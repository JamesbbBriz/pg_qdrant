//! Query-local mature lexical primitives over a complete durable Edge snapshot.
//! There is no independently maintained persistent source index here.
use pg_qdrant_protocol::{ProbeError, SourceLexical};
use serde_json::{Value, json};
use tantivy::collector::{Count, TopDocs};
use tantivy::query::{BooleanQuery, EmptyQuery, Occur, PhraseQuery, Query, TermQuery};
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
    if words.is_empty()
        || words.len() > 8
        || words
            .iter()
            .any(|w| w.len() > 32 || !w.bytes().all(|b| b.is_ascii_alphabetic()))
        || (request.kind == "fuzzy"
            && (words.len() != 1 || words[0].len() < 3 || request.slop != 0))
        || (request.kind == "proximity" && words.len() < 2)
        || !matches!(request.kind.as_str(), "fuzzy" | "proximity")
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
    let query: Box<dyn Query> = if request.kind == "fuzzy" {
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
        output.push(json!({"id":point_id,"score":score}));
    }
    Ok(
        json!({"hits":output,"matched_points":count,"matched_count_exact":true,"top_k":request.top_k,
        "kind":request.kind,"q":request.q,"slop":request.slop,"engine":"tantivy-0.26.2",
        "analyzer":"simple_lower_v1; Unicode simple words, lowercase, positions; ASCII query words only",
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
