//! Strict upstream grammar with bounded product-owned AST admission.
use pg_qdrant_protocol::ProbeError;
use tantivy::query_grammar::{Occur, UserInputAst, UserInputLeaf};

pub fn parse(q: &str, slop: u32) -> Result<UserInputAst, ProbeError> {
    if q.is_empty() || q.len() > 256 || slop != 0 {
        return Err(ProbeError::invalid(
            "syntax requires 1..256 query bytes and external slop zero",
        ));
    }
    let mut ast = tantivy::query_grammar::parse_query(q)
        .map_err(|e| ProbeError::invalid(&format!("invalid lexical syntax: {e:?}")))?;
    let (mut nodes, mut leaves) = (0usize, 0usize);
    validate(&ast, 0, &mut nodes, &mut leaves)?;
    if leaves == 0 {
        return Err(ProbeError::invalid("syntax requires a nonempty literal"));
    }
    complete_negations(&mut ast);
    Ok(ast)
}

// Native BooleanQuery needs a positive universe for a negative-only subgroup.
// The universe is this query's already filtered, complete authorized RAM index.
// Admit user syntax before adding this internal primitive; user '*' stays refused.
fn complete_negations(ast: &mut UserInputAst) {
    match ast {
        UserInputAst::Clause(children) => {
            for (_, child) in children.iter_mut() {
                complete_negations(child);
            }
            if children
                .iter()
                .all(|(occur, _)| *occur == Some(Occur::MustNot))
            {
                children.push((
                    Some(Occur::Must),
                    UserInputAst::Leaf(Box::new(UserInputLeaf::All)),
                ));
            }
        }
        UserInputAst::Boost(child, _) => complete_negations(child),
        UserInputAst::Leaf(_) => {}
    }
}

fn validate(
    ast: &UserInputAst,
    depth: usize,
    nodes: &mut usize,
    leaves: &mut usize,
) -> Result<(), ProbeError> {
    *nodes += 1;
    if depth > 8 || *nodes > 32 {
        return Err(ProbeError::invalid(
            "syntax exceeds depth eight or 32 AST nodes",
        ));
    }
    match ast {
        UserInputAst::Clause(children) => {
            if children.is_empty() {
                return Err(ProbeError::invalid("empty syntax clause"));
            }
            for (_, child) in children {
                validate(child, depth + 1, nodes, leaves)?;
            }
        }
        UserInputAst::Boost(child, boost) => {
            if !boost.0.is_finite() || !(0.1..=10.0).contains(&boost.0) {
                return Err(ProbeError::invalid(
                    "syntax boosts must be finite within 0.1..10",
                ));
            }
            validate(child, depth + 1, nodes, leaves)?;
        }
        UserInputAst::Leaf(leaf) => {
            *leaves += 1;
            let UserInputLeaf::Literal(literal) = leaf.as_ref() else {
                return Err(ProbeError::invalid(
                    "syntax admits literals, Boolean clauses and boosts only",
                ));
            };
            if *leaves > 16
                || literal.prefix
                || literal.slop > 8
                || literal.phrase.contains(['*', '?'])
                || literal.field_name.as_deref().is_some_and(|f| f != "body")
                || literal.phrase.is_empty()
                || literal.phrase.len() > 128
                || literal.phrase.split_whitespace().count() > 8
            {
                return Err(ProbeError::invalid(
                    "syntax requires body-only nonempty literals, at most 16 leaves, eight phrase words and slop 0..8; expansions are refused",
                ));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_grammar_refuses_expansion_fields_and_unbounded_ast() {
        for q in [
            "body:transaction AND NOT durable",
            "(transaction OR diary)^2",
            "\"transaction recovery\"~2",
            "恢复",
            r"body:foo\:bar",
        ] {
            parse(q, 0).unwrap();
        }
        for q in [
            "",
            "*",
            "body:*",
            "point_id:1",
            "other:word",
            "body:[a TO z]",
            "body:IN [a b]",
            "/tr.*/",
            "word*",
            "\"word phrase\"*",
            "word^0",
            "word^11",
            "\"one two\"~9",
            "(word",
            "word AND",
            "\"unterminated",
        ] {
            assert!(parse(q, 0).is_err(), "admitted {q}");
        }
        assert!(parse("word", 1).is_err());
        let distinct = (b'a'..=b'q')
            .map(|c| format!("word{}", char::from(c)))
            .collect::<Vec<_>>()
            .join(" OR ");
        assert!(parse(&distinct, 0).is_err());
        let mut nested = "word".to_owned();
        for _ in 0..12 {
            nested = format!("(word OR {nested})");
        }
        assert!(parse(&nested, 0).is_err());
    }
}
