//! Native term-dictionary completion over a complete authorized query-local index.
use pg_qdrant_protocol::{ProbeError, SourceLexical};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use tantivy::{Searcher, schema::Field};

fn error(e: impl std::fmt::Display) -> ProbeError {
    ProbeError::new(
        "source_lexical_error",
        e.to_string(),
        "No partial suggestions are returned.",
    )
}

pub fn prefix(request: &SourceLexical) -> Result<String, ProbeError> {
    let normalized = request.q.to_lowercase();
    if request.kind != "suggestions"
        || request.slop != 0
        || request.synonyms.is_some()
        || !(1..=20).contains(&request.top_k)
        || request.q.chars().count() < 2
        || request.q.len() > 32
        || normalized.len() > 32
        || !request.q.chars().all(char::is_alphabetic)
        || !normalized.chars().all(char::is_alphabetic)
    {
        return Err(ProbeError::invalid(
            "suggestions require one alphabetic Unicode prefix of 2+ characters and at most 32 UTF-8 bytes, limit 1..20, no slop or synonyms",
        ));
    }
    Ok(normalized)
}

pub fn collect(
    searcher: &Searcher,
    field: Field,
    prefix: &str,
    limit: usize,
) -> Result<Value, ProbeError> {
    // The caller constructs a fresh append-only snapshot: native doc_freq has no tombstones.
    // Prefixes are nonempty valid UTF-8, whose final byte is always below 0xff.
    // The half-open byte range covers exactly all terms beginning with this prefix.
    let mut upper = prefix.as_bytes().to_vec();
    let last = upper
        .last_mut()
        .ok_or_else(|| error("empty suggestion prefix"))?;
    *last = last
        .checked_add(1)
        .ok_or_else(|| error("invalid prefix bound"))?;
    let mut counts = BTreeMap::<String, u64>::new();
    let mut visited = 0usize;
    for segment in searcher.segment_readers() {
        let inverted = segment.inverted_index(field).map_err(error)?;
        let mut stream = inverted
            .terms()
            .range()
            .ge(prefix.as_bytes())
            .lt(&upper)
            .into_stream()
            .map_err(error)?;
        while stream.advance() {
            visited += 1;
            if visited > 1024 {
                return Err(budget());
            }
            let term = std::str::from_utf8(stream.key()).map_err(error)?;
            // This policy deliberately completes words, never numeric identifiers.
            if term.len() > 64 || !term.chars().all(char::is_alphabetic) {
                continue;
            }
            let count = counts.entry(term.to_owned()).or_default();
            *count += u64::from(stream.value().doc_freq);
            if counts.len() > 128 {
                return Err(budget());
            }
        }
    }
    if counts
        .values()
        .any(|&n| n == 0 || n > u64::from(searcher.num_docs()))
    {
        return Err(error("invalid suggestion source-point frequency"));
    }
    let total = counts.len();
    let mut terms: Vec<_> = counts.into_iter().collect();
    terms.sort_by(|(a, na), (b, nb)| nb.cmp(na).then_with(|| a.cmp(b)));
    let items: Vec<_> = terms
        .into_iter()
        .take(limit)
        .map(|(term, count)| json!({"term":term,"point_count":count}))
        .collect();
    Ok(
        json!({"hits":[],"items":items,"prefix":prefix,"kind":"suggestions",
        "limit":limit,"total_terms":total,"terms_complete":total<=limit,"counts_exact":true,
        "count_unit":"source points containing the analyzed word, once per point",
        "ordering":"point_count descending; normalized UTF-8 term ascending for ties",
        "term_policy":"simple_lower_v1 alphabetic words of at most 64 UTF-8 bytes; no phrase, whole-value identifier, typo or synonym completion",
        "engine":"tantivy-0.26.2","max_terms":128,"max_segment_term_visits":1024,
        "visited_segment_terms":visited,"scope":"complete filtered authorized durable query-local source snapshot",
        "index_lifetime":"query-local RAM; no second persistent derived index","release_supported":false}),
    )
}

fn budget() -> ProbeError {
    ProbeError::new(
        "source_lexical_budget",
        "suggestion prefix exceeds 128 eligible terms or 1024 segment-term visits",
        "The entire request is refused; candidates are never truncated before ranking.",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use tantivy::{
        Index, ReloadPolicy, doc,
        schema::{Schema, TEXT},
    };

    #[test]
    fn ineligible_numeric_terms_still_consume_dictionary_visit_budget() {
        let mut schema = Schema::builder();
        let field = schema.add_text_field("body", TEXT);
        let index = Index::create_in_ram(schema.build());
        let mut writer = index.writer_with_num_threads(1, 15_000_000).unwrap();
        let text = (0..1024)
            .map(|n| format!("ab{n}"))
            .collect::<Vec<_>>()
            .join(" ");
        writer.add_document(doc!(field => text.as_str())).unwrap();
        writer.commit().unwrap();
        let reader: tantivy::IndexReader = index.reader().unwrap();
        let exact = collect(&reader.searcher(), field, "ab", 1).unwrap();
        assert_eq!(exact["visited_segment_terms"], 1024);
        assert_eq!(exact["total_terms"], 0);
        assert_eq!(exact["items"], json!([]));
        writer.add_document(doc!(field => "ab1024")).unwrap();
        writer.commit().unwrap();
        reader.reload().unwrap();
        assert_eq!(
            collect(&reader.searcher(), field, "ab", 1)
                .unwrap_err()
                .code,
            "source_lexical_budget"
        );
    }

    #[test]
    fn segment_frequencies_sum_and_exact_visit_boundary_is_enforced() {
        let mut schema = Schema::builder();
        let field = schema.add_text_field("body", TEXT);
        let index = Index::create_in_ram(schema.build());
        let mut writer = index.writer_with_num_threads(1, 15_000_000).unwrap();
        writer.set_merge_policy(Box::new(tantivy::merge_policy::NoMergePolicy));
        let text = (0..128u8)
            .map(|n| format!("ab{}{}", (b'a' + n / 26) as char, (b'a' + n % 26) as char))
            .collect::<Vec<_>>()
            .join(" ");
        for _ in 0..8 {
            writer.add_document(doc!(field => text.as_str())).unwrap();
            writer.commit().unwrap();
        }
        let reader = index
            .reader_builder()
            .reload_policy(ReloadPolicy::Manual)
            .try_into()
            .unwrap();
        let reader: tantivy::IndexReader = reader;
        let exact = collect(&reader.searcher(), field, "ab", 1).unwrap();
        assert_eq!(exact["visited_segment_terms"], 1024);
        assert_eq!(exact["total_terms"], 128);
        assert_eq!(exact["items"][0]["point_count"], 8);
        writer.add_document(doc!(field => text.as_str())).unwrap();
        writer.commit().unwrap();
        reader.reload().unwrap();
        assert_eq!(
            collect(&reader.searcher(), field, "ab", 1)
                .unwrap_err()
                .code,
            "source_lexical_budget"
        );
    }
}
