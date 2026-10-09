//! Query-local mature lexical primitives over a complete durable Edge snapshot.
//! There is no independently maintained persistent source index here.
use pg_qdrant_protocol::{ProbeError, SourceLexical};
use serde_json::{Value, json};
use tantivy::collector::{Count, TopDocs};
use tantivy::query::{BooleanQuery, EmptyQuery, Occur, PhraseQuery, Query, QueryParser, TermQuery};
use tantivy::schema::{
    INDEXED, IndexRecordOption, STORED, Schema, TextFieldIndexing, TextOptions, Value as _,
};
use tantivy::tokenizer::{LowerCaser, SimpleTokenizer, TextAnalyzer};
use tantivy::{Index, ReloadPolicy, TantivyDocument, Term, doc};

const MAX_BYTES: usize = 1024 * 1024;

// Adapt two public upstream APIs; edit-distance construction is library-owned.
struct FuzzyAutomaton(levenshtein_automata::DFA);
impl tantivy_fst::Automaton for FuzzyAutomaton {
    type State = u32;
    fn start(&self) -> u32 {
        self.0.initial_state()
    }
    fn is_match(&self, state: &u32) -> bool {
        matches!(
            self.0.distance(*state),
            levenshtein_automata::Distance::Exact(_)
        )
    }
    fn can_match(&self, state: &u32) -> bool {
        *state != levenshtein_automata::SINK_STATE
    }
    fn accept(&self, state: &u32, byte: u8) -> u32 {
        self.0.transition(*state, byte)
    }
}

fn fuzzy_query(
    searcher: &tantivy::Searcher,
    field: tantivy::schema::Field,
    word: &str,
) -> Result<Box<dyn Query>, ProbeError> {
    let builder = levenshtein_automata::LevenshteinAutomatonBuilder::new(1, true);
    let automaton = FuzzyAutomaton(builder.build_dfa(word));
    let mut terms = std::collections::BTreeSet::new();
    for segment in searcher.segment_readers() {
        let inverted = segment.inverted_index(field).map_err(error)?;
        let mut stream = inverted
            .terms()
            .search(&automaton)
            .into_stream()
            .map_err(error)?;
        while stream.advance() {
            terms.insert(std::str::from_utf8(stream.key()).map_err(error)?.to_owned());
            if terms.len() > 32 {
                return Err(ProbeError::new(
                    "source_lexical_budget",
                    "fuzzy expansion exceeds 32 terms",
                    "No truncated expansion or partial query result is returned.",
                ));
            }
        }
    }
    if terms.is_empty() {
        return Ok(Box::new(EmptyQuery));
    }
    Ok(Box::new(BooleanQuery::new(
        terms
            .into_iter()
            .map(|term| {
                (
                    Occur::Should,
                    Box::new(TermQuery::new(
                        Term::from_field_text(field, &term),
                        IndexRecordOption::WithFreqs,
                    )) as Box<dyn Query>,
                )
            })
            .collect(),
    )))
}

fn error(e: impl std::fmt::Display) -> ProbeError {
    ProbeError::new(
        "source_lexical_error",
        e.to_string(),
        "No partial lexical result is returned.",
    )
}

pub fn search(documents: &[(u64, String)], request: &SourceLexical) -> Result<Value, ProbeError> {
    let bytes = documents
        .iter()
        .try_fold(0usize, |n, (_, body)| n.checked_add(body.len()));
    if documents.len() > 1000
        || bytes.is_none_or(|n| n > MAX_BYTES)
        || documents
            .iter()
            .any(|(id, body)| *id == 0 || body.len() > 65536)
        || !(1..=100).contains(&request.top_k)
        || request.q.len() > 256
        || request.slop > 8
    {
        return Err(ProbeError::new(
            "source_lexical_budget",
            "lexical snapshot/input exceeds bounds",
            "At most 1000 points, 1 MiB source text, 100 hits and slop 0..8 are admitted.",
        ));
    }
    let words: Vec<String> = request
        .q
        .split_ascii_whitespace()
        .map(str::to_ascii_lowercase)
        .collect();
    let syntax = if request.kind == "syntax" {
        Some(crate::lexical_syntax::parse(&request.q, request.slop)?)
    } else {
        None
    };
    let synonyms = if request.kind == "synonyms" {
        Some(crate::lexical_synonyms::expand(
            &request.q,
            request
                .synonyms
                .as_ref()
                .ok_or_else(|| ProbeError::invalid("synonym policy required"))?,
            request.slop,
        )?)
    } else {
        if request.synonyms.is_some() {
            return Err(ProbeError::invalid("synonym policy requires synonyms kind"));
        }
        None
    };
    let suggestion_prefix = if request.kind == "suggestions" {
        Some(crate::lexical_suggestions::prefix(request)?)
    } else {
        None
    };
    if syntax.is_none()
        && synonyms.is_none()
        && suggestion_prefix.is_none()
        && (words.is_empty()
            || words.len() > 8
            || words
                .iter()
                .any(|w| w.len() > 32 || !w.bytes().all(|b| b.is_ascii_alphabetic()))
            || (request.kind == "fuzzy"
                && (words.len() != 1 || words[0].len() < 3 || request.slop != 0))
            || (request.kind == "proximity" && words.len() < 2)
            || !matches!(request.kind.as_str(), "fuzzy" | "proximity"))
    {
        return Err(ProbeError::invalid(
            "lexical query requires ASCII words: fuzzy one 3..32-letter word, proximity 2..8 words; identifiers are not fuzzed",
        ));
    }
    let mut schema = Schema::builder();
    let id = schema.add_u64_field("point_id", INDEXED | STORED);
    let body = schema.add_text_field(
        "body",
        TextOptions::default().set_indexing_options(
            TextFieldIndexing::default()
                .set_tokenizer("simple_lower_v1")
                .set_index_option(IndexRecordOption::WithFreqsAndPositions),
        ),
    );
    let index = Index::create_in_ram(schema.build());
    index.tokenizers().register(
        "simple_lower_v1",
        TextAnalyzer::builder(SimpleTokenizer::default())
            .filter(LowerCaser)
            .build(),
    );
    let mut writer = index
        .writer_with_num_threads(1, 15_000_000)
        .map_err(error)?;
    for (point_id, text) in documents {
        writer
            .add_document(doc!(id => *point_id, body => text.as_str()))
            .map_err(error)?;
    }
    writer.commit().map_err(error)?;
    let reader = index
        .reader_builder()
        .reload_policy(ReloadPolicy::Manual)
        .try_into()
        .map_err(error)?;
    let reader: tantivy::IndexReader = reader;
    let searcher = reader.searcher();
    if let Some(prefix) = suggestion_prefix {
        let mut result =
            crate::lexical_suggestions::collect(&searcher, body, &prefix, request.top_k)?;
        result["source_bytes"] = json!(bytes);
        result["max_source_bytes"] = json!(MAX_BYTES);
        result["writer_threads"] = json!(1);
        result["writer_memory_budget"] = json!(15_000_000);
        return Ok(result);
    }
    let mut highlight_query: Option<Box<dyn Query>> = None;
    let query: Box<dyn Query> = if let Some(expansion) = &synonyms {
        expansion.query(body)
    } else if let Some(ast) = syntax {
        let mut parser = QueryParser::for_index(&index, vec![body]);
        parser.set_conjunction_by_default();
        if let Some(positive) = crate::lexical_syntax::highlight_ast(&ast) {
            highlight_query = Some(
                parser
                    .build_query_from_user_input_ast(positive)
                    .map_err(|e| ProbeError::invalid(&format!("invalid positive syntax: {e}")))?,
            );
        } else {
            highlight_query = Some(Box::new(EmptyQuery));
        }
        let query = parser
            .build_query_from_user_input_ast(ast)
            .map_err(|e| ProbeError::invalid(&format!("invalid lexical syntax: {e}")))?;
        let mut terms = 0usize;
        query.query_terms(&mut |_, _| terms += 1);
        if terms == 0 || terms > 32 {
            return Err(ProbeError::invalid(
                "syntax requires 1..32 analyzed query term occurrences",
            ));
        }
        query
    } else if request.kind == "fuzzy" {
        fuzzy_query(&searcher, body, &words[0])?
    } else {
        let mut query = PhraseQuery::new(
            words
                .iter()
                .map(|word| Term::from_field_text(body, word))
                .collect(),
        );
        query.set_slop(request.slop);
        Box::new(query)
    };
    let (hits, count) = searcher
        .search(
            &query,
            &(TopDocs::with_limit(request.top_k).order_by_score(), Count),
        )
        .map_err(error)?;
    let mut snippets = tantivy::snippet::SnippetGenerator::create(
        &searcher,
        highlight_query.as_deref().unwrap_or(query.as_ref()),
        body,
    )
    .map_err(error)?;
    snippets.set_max_num_chars(150);
    let mut output = Vec::with_capacity(hits.len());
    let mut seen = std::collections::HashSet::new();
    for (score, address) in hits {
        let document: TantivyDocument = searcher.doc(address).map_err(error)?;
        let point_id = document
            .get_first(id)
            .and_then(|value| value.as_u64())
            .ok_or_else(|| error("missing lexical identity"))?;
        if !score.is_finite()
            || !seen.insert(point_id)
            || !documents.iter().any(|(id, _)| *id == point_id)
        {
            return Err(error("invalid lexical identity or score"));
        }
        let original = &documents
            .iter()
            .find(|(id, _)| *id == point_id)
            .ok_or_else(|| error("missing lexical source body"))?
            .1;
        output.push(json!({"id":point_id,"score":score,
            "snippet":crate::lexical_snippets::render(&snippets,original)?}));
    }
    Ok(
        json!({"hits":output,"matched_points":count,"matched_count_exact":true,"top_k":request.top_k,
        "kind":request.kind,"q":request.q,"slop":request.slop,"engine":"tantivy-0.26.2",
        "synonym_policy":synonyms.as_ref().map(|s|&s.evidence),
        "analyzer":"simple_lower_v1; Unicode simple words, lowercase, positions; ASCII fuzzy/proximity words; syntax/synonym literals use this analyzer",
        "syntax_policy":if request.kind=="syntax"{Some("strict body-only grammar; default AND; literals/Boolean/quotes/escapes/boosts; 32 nodes/16 literals/depth8/32 analyzed terms; no expansion/range/regex/set/all")}else{None},
        "score_semantics":"native Tantivy query score; no cross-engine normalization",
        "fuzzy_distance":if request.kind=="fuzzy"{Some(1)}else{None},"transpositions":request.kind=="fuzzy","max_fuzzy_expansions":32,
        "phrase_semantics":"native total movement budget; adjacent transposition costs two",
        "source_bytes":bytes,"max_source_bytes":MAX_BYTES,"writer_threads":1,"writer_memory_budget":15_000_000,
        "index_lifetime":"query-local RAM; no second persistent derived index"}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_suggestions_count_source_points_and_rank_complete_prefix_terms() {
        let documents = vec![
            (1, "transaction transaction transfer".into()),
            (2, "transfer".into()),
            (3, "transit".into()),
            (4, "数据库 数据集".into()),
            (5, "transfer123".into()),
        ];
        let mut request = SourceLexical {
            q: "TRAN".into(),
            kind: "suggestions".into(),
            slop: 0,
            top_k: 2,
            synonyms: None,
        };
        let result = search(&documents, &request).unwrap();
        assert_eq!(result["total_terms"], 3);
        assert_eq!(result["terms_complete"], false);
        assert_eq!(
            result["items"],
            json!([
            {"term":"transfer","point_count":2},
            {"term":"transaction","point_count":1}])
        );
        request.q = "数据".into();
        assert_eq!(
            search(&documents, &request).unwrap()["items"],
            json!([
            {"term":"数据库","point_count":1},{"term":"数据集","point_count":1}])
        );
        request.q = "zz".into();
        assert_eq!(search(&documents, &request).unwrap()["total_terms"], 0);
        for q in ["", "a", "SKU-123", "a b", "car*", "cafe\u{301}"] {
            request.q = q.into();
            assert_eq!(
                search(&documents, &request).unwrap_err().code,
                "invalid_parameter"
            );
        }
        request.q = "trans".into();
        request.top_k = 21;
        assert!(search(&documents, &request).is_err());
        request.top_k = 1;
        request.slop = 1;
        assert!(search(&documents, &request).is_err());
    }

    #[test]
    fn native_suggestion_exact_candidate_limit_refuses_overflow_before_ranking() {
        let term = |n: u8| format!("ab{}{}", (b'a' + n / 26) as char, (b'a' + n % 26) as char);
        let text = (0..128).map(term).collect::<Vec<_>>().join(" ");
        let request = SourceLexical {
            q: "ab".into(),
            kind: "suggestions".into(),
            slop: 0,
            top_k: 1,
            synonyms: None,
        };
        let mut documents = vec![(1, text)];
        let exact = search(&documents, &request).unwrap();
        assert_eq!(exact["total_terms"], 128);
        assert_eq!(exact["items"].as_array().unwrap().len(), 1);
        documents.push((2, term(128)));
        assert_eq!(
            search(&documents, &request).unwrap_err().code,
            "source_lexical_budget"
        );
    }

    #[test]
    fn directional_synonyms_execute_exact_native_phrases_and_original_highlights() {
        let documents = vec![
            (1, "new york hotel".into()),
            (2, "nyc hotel".into()),
            (3, "nyc cheap hotel".into()),
            (4, "metropolis hotel".into()),
            (5, "数据库 恢复".into()),
        ];
        let policy = serde_json::from_value(json!({"id":"cities","revision":1,"rules":[
            {"from":["new","york"],"to":[["nyc"]]},
            {"from":["nyc"],"to":[["metropolis"]]},
            {"from":["database"],"to":[["数据库"]]}]}))
        .unwrap();
        let mut request = SourceLexical {
            q: "new york hotel".into(),
            kind: "synonyms".into(),
            slop: 0,
            top_k: 100,
            synonyms: Some(policy),
        };
        let result = search(&documents, &request).unwrap();
        let ids: std::collections::BTreeSet<_> = result["hits"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h["id"].as_u64().unwrap())
            .collect();
        assert_eq!(ids, std::collections::BTreeSet::from([1, 2]));
        let hit = result["hits"]
            .as_array()
            .unwrap()
            .iter()
            .find(|h| h["id"] == 2)
            .unwrap();
        assert_eq!(hit["snippet"]["status"], "ready");
        assert!(hit["snippet"]["text"].as_str().unwrap().contains("nyc"));
        assert_eq!(result["synonym_policy"]["applied_rules"], 1);
        request.q = "database 恢复".into();
        assert_eq!(search(&documents, &request).unwrap()["hits"][0]["id"], 5);
        request.kind = "syntax".into();
        assert!(search(&documents, &request).is_err());
    }

    #[test]
    fn snippets_use_expanded_native_terms_and_conservative_positive_syntax() {
        let documents = vec![
            (1, "transaction durable recovery".into()),
            (2, "diary recovery".into()),
        ];
        let request = |q: &str, kind: &str| SourceLexical {
            q: q.into(),
            kind: kind.into(),
            slop: 0,
            top_k: 10,
            synonyms: None,
        };
        let fuzzy = search(&documents, &request("transactoin", "fuzzy")).unwrap();
        let snippet = &fuzzy["hits"][0]["snippet"];
        let text = snippet["text"].as_str().unwrap();
        let r = &snippet["highlights"][0];
        assert_eq!(
            &text[r["start"].as_u64().unwrap() as usize..r["end"].as_u64().unwrap() as usize],
            "transaction"
        );
        for q in [
            "transaction OR NOT durable",
            "(transaction OR NOT durable)^2",
        ] {
            let result = search(&documents, &request(q, "syntax")).unwrap();
            assert_eq!(result["matched_points"], 2);
            for hit in result["hits"].as_array().unwrap() {
                let snippet = &hit["snippet"];
                if hit["id"] == 2 {
                    assert_eq!(snippet["status"], "no_positive_term_match");
                    continue;
                }
                assert_eq!(snippet["status"], "ready");
                let text = snippet["text"].as_str().unwrap();
                for r in snippet["highlights"].as_array().unwrap() {
                    assert_eq!(
                        &text[r["start"].as_u64().unwrap() as usize
                            ..r["end"].as_u64().unwrap() as usize],
                        "transaction"
                    );
                }
            }
        }
        let negative = search(&documents, &request("NOT durable", "syntax")).unwrap();
        assert_eq!(
            negative["hits"][0]["snippet"]["status"],
            "no_positive_term_match"
        );
    }

    #[test]
    fn strict_syntax_executes_boolean_phrase_boost_and_literal_analysis() {
        let documents = vec![
            (1, "transaction recovery".into()),
            (2, "transaction durable recovery".into()),
            (3, "recovery transaction".into()),
            (4, "dairy diary".into()),
            (5, "数据库 恢复".into()),
            (6, "foo bar".into()),
        ];
        let request = |q: &str| SourceLexical {
            q: q.into(),
            kind: "syntax".into(),
            slop: 0,
            top_k: 100,
            synonyms: None,
        };
        for (q, expected) in [
            ("body:transaction AND NOT durable", vec![1, 3]),
            ("(transaction OR diary) AND NOT durable", vec![1, 3, 4]),
            ("transaction recovery", vec![1, 2, 3]),
            ("body:\"transaction recovery\"", vec![1]),
            ("\"transaction recovery\"~1", vec![1, 2]),
            ("恢复", vec![5]),
            (r"body:foo\:bar", vec![6]),
        ] {
            let result = search(&documents, &request(q)).unwrap();
            let ids: std::collections::BTreeSet<u64> = result["hits"]
                .as_array()
                .unwrap()
                .iter()
                .map(|h| h["id"].as_u64().unwrap())
                .collect();
            assert_eq!(ids, expected.into_iter().collect(), "{q}");
            assert_eq!(result["matched_points"], ids.len());
        }
        let boosted = search(&documents, &request("diary^10 OR transaction")).unwrap();
        assert_eq!(boosted["hits"][0]["id"], 4);
        for q in ["point_id:1", "*", "word*", "\"!!!\"", "body:[a TO z]"] {
            assert_eq!(
                search(&documents, &request(q)).unwrap_err().code,
                "invalid_parameter"
            );
        }
        let excess = (b'a'..=b'l')
            .map(|c| format!("\"a b c{}\"", char::from(c)))
            .collect::<Vec<_>>()
            .join(" OR ");
        assert_eq!(
            search(&documents, &request(&excess)).unwrap_err().code,
            "invalid_parameter"
        );
    }

    #[test]
    fn mature_fuzzy_and_positional_semantics_and_budgets() {
        let documents = vec![
            (1, "transaction recovery".into()),
            (2, "transaction durable recovery".into()),
            (3, "recovery transaction".into()),
            (4, "dairy diary".into()),
            (5, "ERR-0042".into()),
        ];
        let request = |q: &str, kind: &str, slop| SourceLexical {
            q: q.into(),
            kind: kind.into(),
            slop,
            top_k: 100,
            synonyms: None,
        };
        for (q, kind, slop, expected) in [
            ("transactoin", "fuzzy", 0, 3),
            ("transaction recovery", "proximity", 0, 1),
            ("transaction recovery", "proximity", 1, 2),
            ("transaction recovery", "proximity", 2, 3),
        ] {
            let result = search(&documents, &request(q, kind, slop)).unwrap();
            assert_eq!(result["matched_points"], expected);
            assert_eq!(result["hits"].as_array().unwrap().len(), expected as usize);
        }
        assert_eq!(
            search(&[], &request("recovery", "fuzzy", 0)).unwrap()["matched_points"],
            0
        );
        for q in ["ERR-0042", "a", "中文", "transaction recovery"] {
            assert!(search(&documents, &request(q, "fuzzy", 0)).is_err());
        }
        assert!(search(&documents, &request("transaction recovery", "proximity", 9)).is_err());
        let large = vec![(1, "a".repeat(65536)); 17];
        assert_eq!(
            search(&large, &request("recovery", "fuzzy", 0))
                .unwrap_err()
                .code,
            "source_lexical_budget"
        );
        let vocabulary: String = ('a'..='z')
            .flat_map(|letter| [format!("abcdef{letter} "), format!("{letter}abcdef ")])
            .collect();
        assert_eq!(
            search(&[(1, vocabulary)], &request("abcdef", "fuzzy", 0))
                .unwrap_err()
                .code,
            "source_lexical_budget"
        );
    }
}
