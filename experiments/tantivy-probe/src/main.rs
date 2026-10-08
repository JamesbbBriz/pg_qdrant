//! Isolated, synthetic public-API experiment. This is not a product lexical adapter.
use std::collections::BTreeSet;

use serde_json::{Value as JsonValue, json};
use tantivy::collector::{Count, TopDocs};
use tantivy::query::{
    BooleanQuery, FuzzyTermQuery, Occur, PhraseQuery, Query, QueryParser, TermQuery,
};
use tantivy::schema::{
    Field, INDEXED, IndexRecordOption, STORED, STRING, Schema, TextFieldIndexing, TextOptions,
    Value,
};
use tantivy::snippet::SnippetGenerator;
use tantivy::tokenizer::{LowerCaser, SimpleTokenizer, TextAnalyzer};
use tantivy::{
    Index, IndexReader, IndexWriter, ReloadPolicy, Searcher, TantivyDocument, Term, doc,
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

// Public dependency bridge only: edit-distance construction is upstream-owned.
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

// Synthetic dictionary only. This bounds returned expansions, not all FST work,
// and is not a permission-safe shared-corpus vocabulary or a deadline mechanism.
fn bounded_fuzzy_terms(
    searcher: &Searcher,
    field: Field,
    query: &str,
    cap: usize,
) -> Result<BTreeSet<String>> {
    if query.is_empty()
        || query.len() > 256
        || query.chars().count() > 32
        || !(1..=32).contains(&cap)
        || searcher.segment_readers().len() > 16
    {
        return Err("invalid_experiment_budget".into());
    }
    let builder = levenshtein_automata::LevenshteinAutomatonBuilder::new(1, true);
    let automaton = FuzzyAutomaton(builder.build_dfa(query));
    let mut terms = BTreeSet::new();
    for segment in searcher.segment_readers() {
        let inverted = segment.inverted_index(field)?;
        let mut stream = inverted.terms().search(&automaton).into_stream()?;
        while stream.advance() {
            terms.insert(std::str::from_utf8(stream.key())?.to_owned());
            if terms.len() > cap {
                return Err("term_expansion_overflow".into());
            }
        }
    }
    Ok(terms)
}

struct Fixture {
    index: Index,
    reader: IndexReader,
    writer: IndexWriter,
    id: Field,
    body: Field,
    tenant: Field,
    keyword: Field,
    incarnation: Field,
    // Drop the writer/readers before removing their owned files.
    _directory: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Result<Self> {
        let directory = tempfile::tempdir()?;
        let mut schema = Schema::builder();
        let id = schema.add_u64_field("id", INDEXED | STORED);
        let body = schema.add_text_field(
            "body",
            TextOptions::default().set_stored().set_indexing_options(
                TextFieldIndexing::default()
                    .set_tokenizer("simple_control")
                    .set_index_option(IndexRecordOption::WithFreqsAndPositions),
            ),
        );
        let tenant = schema.add_text_field("tenant", STRING | STORED);
        let keyword = schema.add_text_field("keyword", STRING | STORED);
        let incarnation = schema.add_text_field("incarnation", STRING | STORED);
        let index = Index::create_in_dir(directory.path(), schema.build())?;
        index.tokenizers().register(
            "simple_control",
            TextAnalyzer::builder(SimpleTokenizer::default())
                .filter(LowerCaser)
                .build(),
        );
        let writer = index.writer_with_num_threads(1, 15_000_000)?;
        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::Manual)
            .try_into()?;
        let mut fixture = Self {
            _directory: directory,
            index,
            reader,
            writer,
            id,
            body,
            tenant,
            keyword,
            incarnation,
        };
        for (id, tenant, body, keyword) in [
            (1, "a", "transaction recovery", "ERR-0042"),
            (2, "a", "transaction durable recovery", "ERR-0043"),
            (3, "a", "recovery transaction", "ERR-0044"),
            (4, "b", "transaction recovery secret", "ERR-PRIVATE"),
            (5, "a", "diary", "diary"),
            (6, "a", "dairy", "dairy"),
            (7, "a", "中文 数据库 恢复", "数据库"),
            (
                8,
                "a",
                "ＣＡＦÉ café cafe\u{301} <tag> & recovery",
                "UNICODE",
            ),
        ] {
            fixture.add(id, tenant, &[body], keyword, "initial")?;
        }
        fixture.add(9, "a", &["transaction", "recovery"], "ARRAY", "initial")?;
        fixture.writer.commit()?;
        fixture.reader.reload()?;
        Ok(fixture)
    }

    fn add(
        &mut self,
        id: u64,
        tenant: &str,
        bodies: &[&str],
        keyword: &str,
        incarnation: &str,
    ) -> Result<()> {
        let mut document = doc!(self.id => id, self.tenant => tenant, self.keyword => keyword, self.incarnation => incarnation);
        for body in bodies {
            document.add_text(self.body, body);
        }
        self.writer.add_document(document)?;
        Ok(())
    }

    fn ids(&self, searcher: &Searcher, query: &dyn Query) -> Result<BTreeSet<u64>> {
        let (hits, total) =
            searcher.search(query, &(TopDocs::with_limit(128).order_by_score(), Count))?;
        assert!(
            total <= 128,
            "The complete synthetic match set exceeded the fixture limit"
        );
        let ids: BTreeSet<_> = hits
            .into_iter()
            .map(|(_, address)| {
                let document: TantivyDocument = searcher.doc(address).expect("stored fixture");
                document
                    .get_first(self.id)
                    .and_then(|v| v.as_u64())
                    .expect("stored u64 source ID")
            })
            .collect();
        assert_eq!(
            ids.len(),
            total,
            "Duplicate live source IDs are not acceptable"
        );
        Ok(ids)
    }

    fn document(&self, searcher: &Searcher, id: u64) -> Result<TantivyDocument> {
        let query = TermQuery::new(Term::from_field_u64(self.id, id), IndexRecordOption::Basic);
        let (hits, count) =
            searcher.search(&query, &(TopDocs::with_limit(2).order_by_score(), Count))?;
        assert_eq!(count, 1);
        Ok(searcher.doc(hits[0].1)?)
    }

    fn authorized(&self, query: Box<dyn Query>) -> BooleanQuery {
        BooleanQuery::new(vec![
            (
                Occur::Must,
                Box::new(TermQuery::new(
                    Term::from_field_text(self.tenant, "a"),
                    IndexRecordOption::Basic,
                )),
            ),
            (Occur::Must, query),
        ])
    }
}

fn set(ids: &[u64]) -> BTreeSet<u64> {
    ids.iter().copied().collect()
}

fn fuzzy_and_identifier() -> Result<JsonValue> {
    let fixture = Fixture::new()?;
    let searcher = fixture.reader.searcher();
    for (distance, transpose, expected) in [
        (0, true, set(&[5])),
        (1, true, set(&[5, 6])),
        (1, false, set(&[5])),
        (2, false, set(&[5, 6])),
    ] {
        let query = FuzzyTermQuery::new(
            Term::from_field_text(fixture.keyword, "diary"),
            distance,
            transpose,
        );
        assert_eq!(fixture.ids(&searcher, &query)?, expected);
    }
    let prefix =
        FuzzyTermQuery::new_prefix(Term::from_field_text(fixture.keyword, "diar"), 0, false);
    let whole = FuzzyTermQuery::new(Term::from_field_text(fixture.keyword, "diar"), 0, false);
    assert_eq!(fixture.ids(&searcher, &prefix)?, set(&[5]));
    assert!(fixture.ids(&searcher, &whole)?.is_empty());
    let invalid = FuzzyTermQuery::new(Term::from_field_text(fixture.keyword, "diary"), 3, true);
    assert!(searcher.search(&invalid, &Count).is_err());
    let protected = fixture.authorized(Box::new(TermQuery::new(
        Term::from_field_text(fixture.keyword, "ERR-0042"),
        IndexRecordOption::Basic,
    )));
    assert_eq!(fixture.ids(&searcher, &protected)?, set(&[1]));
    let wrong_case = TermQuery::new(
        Term::from_field_text(fixture.keyword, "err-0042"),
        IndexRecordOption::Basic,
    );
    assert!(fixture.ids(&searcher, &wrong_case)?.is_empty());
    Ok(
        json!({"case":"fuzzy_and_identifier","passed":true,"edit_distances":[0,1,2],"distance_3":"rejected_at_query_execution","transposition_costs_distinguished":true,"whole_token_and_prefix_distinguished":true,"keyword_equality_case_sensitive":true,"production_identifier_policy":false}),
    )
}

fn expansion_limit_boundary() -> Result<JsonValue> {
    let mut fixture = Fixture::new()?;
    let alphabet = "abcdefghijklmnopqrstuvwxyz0123456789ABCD";
    assert_eq!(alphabet.chars().count(), 40);
    for (offset, character) in alphabet.chars().enumerate() {
        fixture.add(
            100 + offset as u64,
            if offset < 20 { "a" } else { "b" },
            &["fixture"],
            &format!("cat{character}"),
            "initial",
        )?;
    }
    fixture.writer.commit()?;
    fixture.reader.reload()?;
    let searcher = fixture.reader.searcher();
    let query = FuzzyTermQuery::new(Term::from_field_text(fixture.keyword, "cat"), 1, false);
    let (hits, total) =
        searcher.search(&query, &(TopDocs::with_limit(1).order_by_score(), Count))?;
    assert_eq!(hits.len(), 1);
    assert_eq!(total, 40);
    assert_eq!(hits[0].0, 1.0);
    assert_eq!(
        bounded_fuzzy_terms(&searcher, fixture.keyword, "cat", 32)
            .unwrap_err()
            .to_string(),
        "term_expansion_overflow"
    );
    assert_eq!(
        bounded_fuzzy_terms(&searcher, fixture.keyword, "diary", 1)
            .unwrap_err()
            .to_string(),
        "term_expansion_overflow"
    );
    let expansions = bounded_fuzzy_terms(&searcher, fixture.keyword, "diary", 32)?;
    assert_eq!(
        expansions,
        BTreeSet::from(["diary".to_string(), "dairy".to_string()])
    );
    let expanded = BooleanQuery::new(
        expansions
            .iter()
            .map(|term| {
                (
                    Occur::Should,
                    Box::new(TermQuery::new(
                        Term::from_field_text(fixture.keyword, term),
                        IndexRecordOption::Basic,
                    )) as Box<dyn Query>,
                )
            })
            .collect(),
    );
    assert_eq!(fixture.ids(&searcher, &expanded)?, set(&[5, 6]));
    assert!(bounded_fuzzy_terms(&searcher, fixture.keyword, "diary", 33).is_err());
    let filtered = fixture.authorized(Box::new(query));
    assert_eq!(fixture.ids(&searcher, &filtered)?.len(), 20);
    Ok(
        json!({"case":"fuzzy_budget_boundary","passed":true,"distinct_matching_fixture_terms":40,"top_docs_limit":1,"matched_documents":total,"tenant_a_matches":20,"native_fuzzy_score":1.0,"native_expansion_cap":null,"public_dictionary_bridge":{"maximum_returned_terms":32,"overflow":"error_without_partial_results","diary_expansions":expansions,"match_set_equivalence":true,"ranking_equivalence":false},"enumeration_work_measured":false,"authorization_security_proven":false,"production_fuzzy_budget_satisfied":false}),
    )
}

fn phrase_and_parser() -> Result<JsonValue> {
    let fixture = Fixture::new()?;
    let searcher = fixture.reader.searcher();
    for (slop, expected) in [
        (0, set(&[1, 4])),
        // Upstream POSITION_GAP is one; slop can cross repeated field values.
        // These are characterization goldens, not the project's array contract.
        (1, set(&[1, 2, 4, 9])),
        (2, set(&[1, 2, 3, 4, 9])),
    ] {
        let mut phrase = PhraseQuery::new(vec![
            Term::from_field_text(fixture.body, "transaction"),
            Term::from_field_text(fixture.body, "recovery"),
        ]);
        phrase.set_slop(slop);
        assert_eq!(fixture.ids(&searcher, &phrase)?, expected);
    }
    let phrase = fixture.authorized(Box::new(PhraseQuery::new(vec![
        Term::from_field_text(fixture.body, "transaction"),
        Term::from_field_text(fixture.body, "recovery"),
    ])));
    assert_eq!(fixture.ids(&searcher, &phrase)?, set(&[1]));
    let mut parser = QueryParser::for_index(&fixture.index, vec![fixture.body]);
    assert_eq!(
        fixture.ids(
            &searcher,
            parser.parse_query("transaction recovery")?.as_ref()
        )?,
        set(&[1, 2, 3, 4, 8, 9])
    );
    parser.set_conjunction_by_default();
    assert_eq!(
        fixture.ids(
            &searcher,
            parser.parse_query("transaction recovery")?.as_ref()
        )?,
        set(&[1, 2, 3, 4, 9])
    );
    assert!(parser.parse_query("unknown_field:value").is_err());
    assert!(parser.parse_query("\"unterminated").is_err());
    Ok(
        json!({"case":"phrase_and_parser","passed":true,"slop_0_ids":[1,4],"slop_1_ids":[1,2,4,9],"slop_2_ids":[1,2,3,4,9],"slop_2_admits_reversal":true,"multi_value_phrase_not_matched_at_slop_0":true,"slop_1_crosses_repeated_field_values":true,"production_array_boundary_satisfied":false,"tenant_filtered_phrase_ids":[1],"parser_default":"OR","explicit_conjunction":"AND","unknown_field_and_malformed_quote":"rejected","project_query_grammar_implemented":false}),
    )
}

fn source_snippet_boundaries() -> Result<JsonValue> {
    let fixture = Fixture::new()?;
    let searcher = fixture.reader.searcher();
    let query = TermQuery::new(
        Term::from_field_text(fixture.body, "recovery"),
        IndexRecordOption::WithFreqs,
    );
    let mut generator = SnippetGenerator::create(&searcher, &query, fixture.body)?;
    generator.set_max_num_chars(240);
    let document = fixture.document(&searcher, 8)?;
    let snippet = generator.snippet_from_doc(&document);
    assert_eq!(snippet.highlighted().len(), 1);
    let range = snippet.highlighted()[0].clone();
    assert!(
        snippet.fragment().is_char_boundary(range.start)
            && snippet.fragment().is_char_boundary(range.end)
    );
    assert_eq!(&snippet.fragment()[range.clone()], "recovery");
    let original = document
        .get_first(fixture.body)
        .and_then(|v| v.as_str())
        .unwrap();
    assert_eq!(snippet.fragment(), original);
    assert!(snippet.to_html().contains("&lt;tag&gt; &amp;"));
    let array_document = fixture.document(&searcher, 9)?;
    let joined = generator.snippet_from_doc(&array_document);
    assert_eq!(joined.fragment(), "transaction recovery");
    let fuzzy = FuzzyTermQuery::new(Term::from_field_text(fixture.body, "diary"), 1, true);
    assert_eq!(fixture.ids(&searcher, &fuzzy)?, set(&[5, 6]));
    let mut visited = 0;
    fuzzy.query_terms(&mut |_, _| visited += 1);
    assert_eq!(visited, 0);
    let fuzzy_generator = SnippetGenerator::create(&searcher, &fuzzy, fixture.body)?;
    let fuzzy_document = fixture.document(&searcher, 6)?;
    let fuzzy_snippet = fuzzy_generator.snippet_from_doc(&fuzzy_document);
    assert!(fuzzy_snippet.is_empty());
    assert!(fuzzy_snippet.highlighted().is_empty());
    Ok(
        json!({"case":"source_snippet_boundaries","passed":true,"literal_fragment":snippet.fragment(),"fragment_utf8_byte_range":[range.start,range.end],"literal_html_escaped":true,"repeated_values_joined":joined.fragment(),"original_array_element_identity":null,"fuzzy_match_ids":[5,6],"fuzzy_visited_terms":visited,"fuzzy_highlights":[],"source_offset_mapping_for_general_fragments":false,"production_fuzzy_highlighting":false,"chinese_segmentation_parity":false}),
    )
}

fn source_replacement_and_reader_boundary() -> Result<JsonValue> {
    let mut fixture = Fixture::new()?;
    let previous_searcher = fixture.reader.searcher();
    for _ in 0..2 {
        fixture
            .writer
            .delete_term(Term::from_field_u64(fixture.id, 1));
        fixture.add(
            1,
            "a",
            &["replacement durable text"],
            "ERR-0042",
            "replacement",
        )?;
    }
    fixture.writer.commit()?;
    let before_reload = fixture.reader.searcher();
    for searcher in [&previous_searcher, &before_reload] {
        assert_eq!(
            fixture
                .document(searcher, 1)?
                .get_first(fixture.incarnation)
                .and_then(|v| v.as_str()),
            Some("initial")
        );
    }
    fixture.reader.reload()?;
    let after_reload = fixture.reader.searcher();
    let replacement = fixture.document(&after_reload, 1)?;
    assert_eq!(
        replacement
            .get_first(fixture.incarnation)
            .and_then(|v| v.as_str()),
        Some("replacement")
    );
    let body = replacement
        .get_first(fixture.body)
        .and_then(|v| v.as_str())
        .unwrap();
    assert_eq!(body, "replacement durable text");
    fixture
        .writer
        .delete_term(Term::from_field_u64(fixture.id, 1));
    fixture.add(1, "a", &["uncommitted"], "ERR-0042", "not_committed")?;
    fixture.writer.rollback()?;
    fixture.reader.reload()?;
    assert_eq!(
        fixture
            .document(&fixture.reader.searcher(), 1)?
            .get_first(fixture.incarnation)
            .and_then(|v| v.as_str()),
        Some("replacement")
    );
    assert_eq!(
        fixture
            .document(&previous_searcher, 1)?
            .get_first(fixture.incarnation)
            .and_then(|v| v.as_str()),
        Some("initial")
    );
    Ok(
        json!({"case":"replacement_and_reader_boundary","passed":true,"repeated_delete_add_live_source_ids":1,"commit_does_not_reload_manual_reader":true,"old_searcher_pins_previous_view":true,"writer_rollback_retains_committed_replacement":true,"crash_recovery_verified":false,"postgres_or_dual_engine_transaction":false}),
    )
}

fn main() -> Result<()> {
    let cases = vec![
        fuzzy_and_identifier()?,
        expansion_limit_boundary()?,
        phrase_and_parser()?,
        source_snippet_boundaries()?,
        source_replacement_and_reader_boundary()?,
    ];
    println!(
        "{}",
        json!({"schema_version":1,"kind":"isolated_tantivy_public_api_probe","tantivy_version":"0.26.2","features":["mmap","lz4-compression"],"default_features":false,"fixture_kind":"small_synthetic_semantic_controls","writer_threads":1,"writer_memory_budget_bytes":15_000_000,"cases":cases,"status":"passed","core_dependency_adopted":false,"combined_edge_pg_build_verified":false,"held_out_relevance_evaluation":false,"production_authorization":false,"f13_f20_completed":false})
    );
    Ok(())
}
