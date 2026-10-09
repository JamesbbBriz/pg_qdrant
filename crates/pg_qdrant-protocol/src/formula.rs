//! Owned score-only expressions. Payload access requires a separate capture contract.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum ScoreFormula {
    Constant {
        value: f64,
    },
    Score {},
    Add {
        args: Vec<ScoreFormula>,
    },
    Multiply {
        args: Vec<ScoreFormula>,
    },
    Negate {
        arg: Box<ScoreFormula>,
    },
    Abs {
        arg: Box<ScoreFormula>,
    },
    Sqrt {
        arg: Box<ScoreFormula>,
    },
    Divide {
        left: Box<ScoreFormula>,
        right: Box<ScoreFormula>,
        #[serde(default)]
        by_zero_default: Option<f64>,
    },
}

impl ScoreFormula {
    /// Root depth is one; native runtime still checks arithmetic domains.
    pub fn validate(&self) -> Result<usize, String> {
        fn visit(node: &ScoreFormula, depth: usize, count: &mut usize) -> Result<(), String> {
            *count += 1;
            if depth > 8 || *count > 64 {
                return Err("formula exceeds depth 8 or 64 nodes".into());
            }
            let numeric = |value: f64| -> Result<(), String> {
                if value.is_finite() && value.abs() <= 1_000_000.0 {
                    Ok(())
                } else {
                    Err(
                        "formula constant must be finite with absolute value at most 1000000"
                            .into(),
                    )
                }
            };
            match node {
                ScoreFormula::Constant { value } => numeric(*value)?,
                ScoreFormula::Score {} => (),
                ScoreFormula::Add { args } | ScoreFormula::Multiply { args } => {
                    if !(2..=8).contains(&args.len()) {
                        return Err("formula add/multiply requires 2..8 arguments".into());
                    }
                    for arg in args {
                        visit(arg, depth + 1, count)?;
                    }
                }
                ScoreFormula::Negate { arg }
                | ScoreFormula::Abs { arg }
                | ScoreFormula::Sqrt { arg } => {
                    visit(arg, depth + 1, count)?;
                }
                ScoreFormula::Divide {
                    left,
                    right,
                    by_zero_default,
                } => {
                    if let Some(value) = by_zero_default {
                        numeric(*value)?;
                    }
                    visit(left, depth + 1, count)?;
                    visit(right, depth + 1, count)?;
                }
            }
            Ok(())
        }
        let mut count = 0;
        visit(self, 1, &mut count)?;
        Ok(count)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn strict_score_formula_shapes_and_numeric_bounds() {
        for input in [
            json!({"op":"field","name":"revision"}),
            json!({"op":"score","name":"$score[1]"}),
            json!({"op":"constant","value":1,"hidden":true}),
            json!({"op":"divide","left":{"op":"score"},"right":{"op":"score"},"by_zero_default":"0"}),
        ] {
            assert!(serde_json::from_value::<ScoreFormula>(input).is_err());
        }
        for value in [f64::NAN, f64::INFINITY, 1_000_001.0, 1_000_000.01] {
            assert!(ScoreFormula::Constant { value }.validate().is_err());
        }
        assert_eq!(
            ScoreFormula::Constant {
                value: -1_000_000.0
            }
            .validate()
            .unwrap(),
            1
        );
        assert!(
            ScoreFormula::Add {
                args: vec![ScoreFormula::Score {}]
            }
            .validate()
            .is_err()
        );
        assert!(
            ScoreFormula::Multiply {
                args: vec![ScoreFormula::Score {}; 9]
            }
            .validate()
            .is_err()
        );
        let mut expression = ScoreFormula::Score {};
        for _ in 0..7 {
            expression = ScoreFormula::Negate {
                arg: Box::new(expression),
            };
        }
        assert_eq!(expression.validate().unwrap(), 8);
        assert!(
            ScoreFormula::Abs {
                arg: Box::new(expression)
            }
            .validate()
            .is_err()
        );
        let sixty_four = ScoreFormula::Add {
            args: vec![
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
                ScoreFormula::Add {
                    args: vec![ScoreFormula::Score {}; 8],
                },
            ],
        };
        assert_eq!(sixty_four.validate().unwrap(), 64);
        assert!(
            ScoreFormula::Negate {
                arg: Box::new(sixty_four)
            }
            .validate()
            .is_err()
        );
    }
}
