//! Bounded directional query policy; positional execution stays in Tantivy.
use pg_qdrant_protocol::{ProbeError, SynonymPolicy};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use tantivy::Term;
use tantivy::query::{BooleanQuery, Occur, PhraseQuery, Query, TermQuery};
use tantivy::schema::{Field, IndexRecordOption};

#[derive(Debug)]
pub struct Expansion {
    phrases: BTreeSet<Vec<String>>,
    pub evidence: Value,
}

fn words(input: &[String], maximum: usize) -> Result<Vec<String>, ProbeError> {
    if input.is_empty() || input.len() > maximum {
        return Err(ProbeError::invalid("synonym phrase word count is invalid"));
    }
    input
        .iter()
        .map(|word| {
            let normalized = word.to_lowercase();
            if normalized.is_empty()
                || word.len() > 64
                || normalized.len() > 64
                || !word.chars().all(char::is_alphabetic)
                || !normalized.chars().all(char::is_alphabetic)
            {
                return Err(ProbeError::invalid(concat!(
                    "synonyms require single alphabetic Unicode words; ",
                    "identifiers and operators are refused"
                )));
            }
            Ok(normalized)
        })
        .collect()
}

fn budget() -> ProbeError {
    ProbeError::new(
        "source_lexical_budget",
        "synonym expansion exceeds 32 phrases, 16 words per phrase or 256 total terms",
        "The entire query is refused; expansions are never truncated.",
    )
}

pub fn expand(q: &str, policy: &SynonymPolicy, slop: u32) -> Result<Expansion, ProbeError> {
    if q.is_empty()
        || q.len() > 256
        || slop != 0
        || policy.id.is_empty()
        || policy.id.len() > 64
        || !policy
            .id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        || !(1..=1_000_000_000).contains(&policy.revision)
        || !(1..=16).contains(&policy.rules.len())
        || serde_json::to_vec(policy)
            .map_err(|_| ProbeError::invalid("invalid synonym policy"))?
            .len()
            > 8192
    {
        return Err(ProbeError::invalid(
            "synonyms require a bounded named revisioned policy, 1..16 rules and external slop zero",
        ));
    }
    let input = words(
        &q.split_whitespace().map(str::to_owned).collect::<Vec<_>>(),
        8,
    )?;
    let mut rules = BTreeMap::new();
    for rule in &policy.rules {
        let from = words(&rule.from, 4)?;
        if !(1..=4).contains(&rule.to.len()) {
            return Err(ProbeError::invalid(
                "synonym rules require 1..4 directional alternatives",
            ));
        }
        let to = rule
            .to
            .iter()
            .map(|p| words(p, 4))
            .collect::<Result<BTreeSet<_>, _>>()?;
        if rules.insert(from, to).is_some() {
            return Err(ProbeError::invalid(
                "duplicate normalized synonym source phrase",
            ));
        }
    }
    let canonical = json!({"id":policy.id,"revision":policy.revision,"rules":rules.iter()
        .map(|(from,to)| json!({"from":from,"to":to})).collect::<Vec<_>>()});
    let digest = Sha256::digest(serde_json::to_vec(&canonical).expect("bounded JSON policy"))
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let mut phrases = BTreeSet::from([Vec::new()]);
    let (mut offset, mut applied) = (0, 0);
    while offset < input.len() {
        // Match only original input; longest leftmost rule wins, without recursion.
        let matched = rules
            .iter()
            .filter(|(from, _)| input[offset..].starts_with(from))
            .max_by_key(|(from, _)| from.len());
        let (width, alternatives) = if let Some((from, to)) = matched {
            applied += 1;
            let mut options = to.clone();
            options.insert(from.clone());
            (from.len(), options)
        } else {
            (1, BTreeSet::from([vec![input[offset].clone()]]))
        };
        let mut next = BTreeSet::new();
        for prefix in &phrases {
            for suffix in &alternatives {
                let phrase: Vec<_> = prefix.iter().chain(suffix).cloned().collect();
                if phrase.len() > 16 {
                    return Err(budget());
                }
                next.insert(phrase);
                if next.len() > 32 || next.iter().map(Vec::len).sum::<usize>() > 256 {
                    return Err(budget());
                }
            }
        }
        phrases = next;
        offset += width;
    }
    let evidence = json!({"id":policy.id,"revision":policy.revision,"sha256":digest,
        "applied_rules":applied,"expanded_phrases":phrases,"max_phrases":32,"max_total_terms":256,
        "max_words_per_phrase":16,"max_query_words":8,
        "direction":"explicit from-to; original retained; longest-leftmost; single pass; no recursive or reverse expansion",
        "matching":"OR of exact contiguous phrases; simple_lower_v1; no implicit Boolean/fuzzy syntax",
        "lifecycle":"request-local query policy; no persisted dictionary or index rebuild"});
    Ok(Expansion { phrases, evidence })
}

impl Expansion {
    pub fn query(&self, field: Field) -> Box<dyn Query> {
        Box::new(BooleanQuery::new(
            self.phrases
                .iter()
                .map(|phrase| {
                    let terms: Vec<_> = phrase
                        .iter()
                        .map(|word| Term::from_field_text(field, word))
                        .collect();
                    let query: Box<dyn Query> = if terms.len() == 1 {
                        Box::new(TermQuery::new(
                            terms[0].clone(),
                            IndexRecordOption::WithFreqs,
                        ))
                    } else {
                        Box::new(PhraseQuery::new(terms))
                    };
                    (Occur::Should, query)
                })
                .collect(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy(value: Value) -> SynonymPolicy {
        serde_json::from_value(value).unwrap()
    }
    #[test]
    fn directional_longest_multiword_nonrecursive_and_canonical_identity() {
        let p = policy(json!({"id":"transport","revision":1,"rules":[
            {"from":["new","york"],"to":[["nyc"]]},
            {"from":["new"],"to":[["fresh"]]},
            {"from":["nyc"],"to":[["metropolis"]]}]}));
        let result = expand("NEW york hotel", &p, 0).unwrap();
        assert_eq!(
            result.phrases,
            BTreeSet::from([
                vec!["new".into(), "york".into(), "hotel".into()],
                vec!["nyc".into(), "hotel".into()]
            ])
        );
        assert_eq!(
            expand("metropolis", &p, 0).unwrap().phrases,
            BTreeSet::from([vec!["metropolis".into()]])
        );
        let mut reordered = p.clone();
        reordered.rules.reverse();
        assert_eq!(
            result.evidence["sha256"],
            expand("NEW york hotel", &reordered, 0).unwrap().evidence["sha256"]
        );
        reordered.revision = 2;
        assert_ne!(
            result.evidence["sha256"],
            expand("NEW york hotel", &reordered, 0).unwrap().evidence["sha256"]
        );
        for q in ["SKU-123", "car OR *", "cafe\u{301}", ""] {
            assert!(expand(q, &p, 0).is_err());
        }
        assert!(expand("hotel", &p, 1).is_err());
    }
    #[test]
    fn duplicate_and_combinatorial_expansions_refuse_without_truncation() {
        let mut p = policy(
            json!({"id":"budget","revision":1,"rules":[{"from":["a"],"to":[["b"],["c"],["d"],["e"]]}]}),
        );
        assert_eq!(
            expand("a a a", &p, 0).unwrap_err().code,
            "source_lexical_budget"
        );
        p.rules.push(p.rules[0].clone());
        assert_eq!(expand("a", &p, 0).unwrap_err().code, "invalid_parameter");
    }

    #[test]
    fn exact_total_term_boundary_and_normalized_alternative_identity() {
        let p = policy(json!({"id":"boundary","revision":1,"rules":[
            {"from":["a"],"to":[["b"]]}]}));
        let exact = expand("a a a a a x y z", &p, 0).unwrap();
        assert_eq!(exact.phrases.len(), 32);
        assert_eq!(exact.phrases.iter().map(Vec::len).sum::<usize>(), 256);
        let mut duplicate = p.clone();
        duplicate.rules[0].to.push(vec!["B".into()]);
        assert_eq!(
            exact.evidence["sha256"],
            expand("a a a a a x y z", &duplicate, 0).unwrap().evidence["sha256"]
        );
        let wider = policy(json!({"id":"boundary","revision":1,"rules":[
            {"from":["a"],"to":[["b","c"]]}]}));
        assert_eq!(
            expand("a a a a a x y z", &wider, 0).unwrap_err().code,
            "source_lexical_budget"
        );
        let too_long = policy(json!({"id":"boundary","revision":1,"rules":[
            {"from":["a"],"to":[["b","c","d","e"]]}]}));
        assert_eq!(
            expand("a a a a a", &too_long, 0).unwrap_err().code,
            "source_lexical_budget"
        );
    }
}
