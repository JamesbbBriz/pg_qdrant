//! Bounded original-text fragments from the pinned native snippet generator.
use pg_qdrant_protocol::ProbeError;
use serde_json::{Value, json};
use tantivy::snippet::SnippetGenerator;

pub fn render(generator: &SnippetGenerator, source: &str) -> Result<Value, ProbeError> {
    let snippet = generator.snippet(source);
    if snippet.is_empty() {
        return Ok(json!({"status":"no_positive_term_match"}));
    }
    let fragment = snippet.fragment();
    let ranges = snippet.highlighted();
    if fragment.len() > 512 || ranges.len() > 32 {
        return Ok(json!({"status":"fragment_budget_exceeded"}));
    }
    // Native offsets refer to unmodified UTF-8 source bytes. Reject impossible
    // offsets and avoid inventing a unique source position for repeated text.
    if fragment.is_empty()
        || ranges.iter().any(|r| {
            r.start >= r.end
                || r.end > fragment.len()
                || !fragment.is_char_boundary(r.start)
                || !fragment.is_char_boundary(r.end)
        })
    {
        return Err(ProbeError::new(
            "source_lexical_error",
            "invalid native snippet UTF-8 ranges",
            "No snippet or partial lexical result is returned.",
        ));
    }
    let Some(first) = source.find(fragment) else {
        return Err(ProbeError::new(
            "source_lexical_error",
            "native snippet is not original source text",
            "No snippet or partial lexical result is returned.",
        ));
    };
    let ambiguous = source.rfind(fragment) != Some(first);
    Ok(json!({"status":"ready","text":fragment,
        "highlights":ranges.iter().map(|r|json!({"start":r.start,"end":r.end})).collect::<Vec<_>>(),
        "offset_unit":"utf8_bytes","offset_scope":"fragment","source_byte_start":if ambiguous {None}else{Some(first)},
        "source_occurrence_ambiguous":ambiguous,"max_fragment_bytes":512,"max_highlight_ranges":32,
        "match_scope":"positive lexical term occurrences; not Boolean or phrase-match proof"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tantivy::schema::Schema;
    use tantivy::tokenizer::{LowerCaser, SimpleTokenizer, TextAnalyzer};

    fn generator(term: &str) -> SnippetGenerator {
        let field = Schema::builder().add_text_field("body", tantivy::schema::TEXT);
        SnippetGenerator::new(
            [(term.to_owned(), 1.0)].into_iter().collect(),
            TextAnalyzer::builder(SimpleTokenizer::default())
                .filter(LowerCaser)
                .build(),
            field,
            150,
        )
    }

    #[test]
    fn native_fragments_preserve_unicode_original_bytes_and_explicit_bounds() {
        let original = "Préface 🙂 <script> Café CAFÉ cafe\u{301} end";
        let result = render(&generator("café"), original).unwrap();
        assert_eq!(result["status"], "ready");
        let text = result["text"].as_str().unwrap();
        let start = result["source_byte_start"].as_u64().unwrap() as usize;
        assert_eq!(&original[start..start + text.len()], text);
        let highlighted: Vec<_> = result["highlights"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                &text[r["start"].as_u64().unwrap() as usize..r["end"].as_u64().unwrap() as usize]
            })
            .collect();
        assert_eq!(highlighted, vec!["Café", "CAFÉ"]);
        assert!(text.contains("<script>"));
        let repeat = render(&generator("word"), &"word ".repeat(80)).unwrap();
        assert!(repeat["source_occurrence_ambiguous"].as_bool().unwrap());
        assert!(repeat["source_byte_start"].is_null());
        let long = "x".repeat(600);
        assert_eq!(
            render(&generator(&long), &long).unwrap()["status"],
            "fragment_budget_exceeded"
        );
        assert_eq!(
            render(&generator("absent"), original).unwrap()["status"],
            "no_positive_term_match"
        );
    }
}
