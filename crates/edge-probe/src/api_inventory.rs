//! Exhaustive compile sentinels for the public pinned upstream surface.
//!
//! These deliberately have no wildcard arms. Upstream variants cannot silently
//! acquire SQL support; an upgrade that adds one requires an explicit mapping.
//! A matched type/variant is compile evidence, not a runtime or SQL test.

#![allow(dead_code)]

use qdrant_edge::*;

/// Construct the typed advanced queries without relying on REST/client types.
/// This only proves that their published Rust inputs can be expressed.
pub fn construct_advanced_queries() -> OperationResult<Vec<ScoringQuery>> {
    use external::ordered_float::OrderedFloat;
    let vector = VectorInternal::Dense(vec![1.0, 0.0]);
    let negative = VectorInternal::Dense(vec![0.0, 1.0]);
    let pair = ContextPair {
        positive: vector.clone(),
        negative: negative.clone(),
    };
    let recommendation = RecommendQuery::new(vec![vector.clone()], vec![negative]);
    let formula = Formula {
        formula: Expression::Sum(vec![
            Expression::Variable("$score[0]".into()),
            Expression::Constant(1.0),
        ]),
        defaults: Default::default(),
    };
    Ok(vec![
        ScoringQuery::Vector(QueryEnum::RecommendBestScore(NamedQuery {
            query: recommendation.clone(),
            using: Some("dense".into()),
        })),
        ScoringQuery::Vector(QueryEnum::RecommendSumScores(NamedQuery {
            query: recommendation,
            using: Some("dense".into()),
        })),
        ScoringQuery::Vector(QueryEnum::Discover(NamedQuery {
            query: DiscoverQuery::new(vector.clone(), vec![pair.clone()]),
            using: Some("dense".into()),
        })),
        ScoringQuery::Vector(QueryEnum::Context(NamedQuery {
            query: ContextQuery::new(vec![pair]),
            using: Some("dense".into()),
        })),
        ScoringQuery::Vector(QueryEnum::FeedbackNaive(NamedQuery {
            query: FeedbackNaiveQuery {
                target: vector.clone(),
                feedback: vec![FeedbackItem {
                    vector: vector.clone(),
                    score: OrderedFloat(1.0),
                }],
                coefficients: NaiveFeedbackStrategy {
                    a: OrderedFloat(1.0),
                    b: OrderedFloat(1.0),
                    c: OrderedFloat(1.0),
                },
            },
            using: Some("dense".into()),
        })),
        ScoringQuery::Formula(formula.try_into()?),
        ScoringQuery::Sample(Sample::Random),
        ScoringQuery::Mmr(Mmr {
            vector,
            using: "dense".into(),
            lambda: OrderedFloat(0.5),
            candidates_limit: 16,
        }),
    ])
}

pub fn query_mapping(query: &ScoringQuery) -> &'static str {
    match query {
        ScoringQuery::Vector(query) => vector_query_mapping(query),
        ScoringQuery::Fusion(_) => "Q03",
        ScoringQuery::OrderBy(_) | ScoringQuery::Sample(_) => "Q10",
        ScoringQuery::Formula(_) => "Q08",
        ScoringQuery::Mmr(_) => "Q07",
    }
}

pub fn vector_query_mapping(query: &QueryEnum) -> &'static str {
    match query {
        QueryEnum::Nearest(_) => "Q01",
        QueryEnum::RecommendBestScore(_) | QueryEnum::RecommendSumScores(_) => "Q04",
        QueryEnum::Discover(_) | QueryEnum::Context(_) => "Q05",
        QueryEnum::FeedbackNaive(_) => "Q06",
    }
}

pub fn match_mapping(query: &Match) -> &'static str {
    match query {
        Match::Value(_) => "F10/Q12",
        Match::Text(_) | Match::TextAny(_) => "F06",
        Match::Phrase(_) => "F08",
        Match::Prefix(_) => "F11",
        Match::Any(_) | Match::Except(_) => "F07/Q12",
    }
}

pub fn condition_mapping(condition: &Condition) -> &'static str {
    match condition {
        Condition::Field(_)
        | Condition::IsEmpty(_)
        | Condition::IsNull(_)
        | Condition::HasId(_)
        | Condition::HasVector(_)
        | Condition::Slice(_)
        | Condition::Nested(_)
        | Condition::Filter(_) => "Q12",
        // The input trait is not re-exported. No arbitrary callback can cross
        // the extension boundary, and PG must never be called on Edge threads.
        Condition::CustomIdChecker(_) => "outside SQL scope: native callback",
    }
}

pub fn tokenizer_mapping(tokenizer: TokenizerType) -> &'static str {
    match tokenizer {
        TokenizerType::Word | TokenizerType::Whitespace => "F04",
        TokenizerType::Prefix => "F09",
        TokenizerType::Multilingual => "F05",
    }
}

pub fn input_mapping(vector: &VectorInternal) -> &'static str {
    match vector {
        VectorInternal::Dense(_) => "V01",
        VectorInternal::Sparse(_) => "V02",
        VectorInternal::MultiDense(_) => "V03",
    }
}

pub fn vectors_mapping(vectors: &VectorStructInternal) -> &'static str {
    match vectors {
        VectorStructInternal::Single(_) | VectorStructInternal::Named(_) => "V01",
        VectorStructInternal::MultiDense(_) => "V03",
    }
}

pub fn persisted_vector_mapping(vector: &VectorPersisted) -> &'static str {
    match vector {
        VectorPersisted::Dense(_) => "V01/L02",
        VectorPersisted::Sparse(_) => "V02/L02",
        VectorPersisted::MultiDense(_) => "V03/L02",
    }
}

pub fn persisted_vectors_mapping(vectors: &VectorStructPersisted) -> &'static str {
    match vectors {
        VectorStructPersisted::Single(_) | VectorStructPersisted::Named(_) => "V01/L02",
        VectorStructPersisted::MultiDense(_) => "V03/L02",
    }
}

pub fn distance_mapping(distance: Distance) -> &'static str {
    match distance {
        Distance::Dot | Distance::Cosine | Distance::Euclid | Distance::Manhattan => "V01/Q01",
    }
}

pub fn multivector_comparator_mapping(comparator: MultiVectorComparator) -> &'static str {
    match comparator {
        MultiVectorComparator::MaxSim => "V03",
    }
}

pub fn modifier_mapping(modifier: Modifier) -> &'static str {
    match modifier {
        Modifier::None | Modifier::Idf => "V02/Q14",
    }
}

pub fn point_id_mapping(id: PointId) -> &'static str {
    match id {
        PointId::NumId(_) | PointId::Uuid(_) => "L02/L05",
    }
}

pub fn value_mapping(value: &ValueVariants) -> &'static str {
    match value {
        ValueVariants::String(_) | ValueVariants::Integer(_) | ValueVariants::Bool(_) => "Q12",
    }
}

pub fn any_value_mapping(value: &AnyVariants) -> &'static str {
    match value {
        AnyVariants::Strings(_) | AnyVariants::Integers(_) => "Q12",
    }
}

pub fn datatype_mapping(datatype: VectorStorageDatatype) -> &'static str {
    match datatype {
        VectorStorageDatatype::Float32
        | VectorStorageDatatype::Float16
        | VectorStorageDatatype::Uint8
        | VectorStorageDatatype::Turbo4 => "V05",
    }
}

pub fn quantization_mapping(config: &QuantizationConfig) -> &'static str {
    match config {
        QuantizationConfig::Scalar(_)
        | QuantizationConfig::Product(_)
        | QuantizationConfig::Binary(_)
        | QuantizationConfig::Turbo(_) => "V04",
    }
}

pub fn binary_encoding_mapping(encoding: BinaryQuantizationEncoding) -> &'static str {
    match encoding {
        BinaryQuantizationEncoding::OneBit
        | BinaryQuantizationEncoding::TwoBits
        | BinaryQuantizationEncoding::OneAndHalfBits => "V04; not native V08 input",
    }
}

pub fn binary_query_encoding_mapping(encoding: BinaryQuantizationQueryEncoding) -> &'static str {
    match encoding {
        BinaryQuantizationQueryEncoding::Default
        | BinaryQuantizationQueryEncoding::Binary
        | BinaryQuantizationQueryEncoding::Scalar4Bits
        | BinaryQuantizationQueryEncoding::Scalar8Bits => "V04",
    }
}

pub fn payload_schema_mapping(schema: &PayloadSchemaParams) -> &'static str {
    match schema {
        PayloadSchemaParams::Keyword(_)
        | PayloadSchemaParams::Integer(_)
        | PayloadSchemaParams::Float(_)
        | PayloadSchemaParams::Geo(_)
        | PayloadSchemaParams::Text(_)
        | PayloadSchemaParams::Bool(_)
        | PayloadSchemaParams::Datetime(_)
        | PayloadSchemaParams::Uuid(_) => "Q12",
    }
}

pub fn payload_type_mapping(schema: PayloadSchemaType) -> &'static str {
    match schema {
        PayloadSchemaType::Keyword
        | PayloadSchemaType::Integer
        | PayloadSchemaType::Float
        | PayloadSchemaType::Geo
        | PayloadSchemaType::Text
        | PayloadSchemaType::Bool
        | PayloadSchemaType::Datetime
        | PayloadSchemaType::Uuid => "Q12",
    }
}

pub fn payload_field_mapping(schema: &PayloadFieldSchema) -> &'static str {
    match schema {
        PayloadFieldSchema::FieldType(_) | PayloadFieldSchema::FieldParams(_) => "Q12",
    }
}

pub fn update_mapping(operation: &UpdateOperation) -> &'static str {
    match operation {
        UpdateOperation::PointOperation(op) => point_operation_mapping(op),
        UpdateOperation::VectorOperation(_)
        | UpdateOperation::PayloadOperation(_)
        | UpdateOperation::FieldIndexOperation(_)
        | UpdateOperation::VectorNameOperation(_) => "L02",
    }
}

pub fn point_operation_mapping(operation: &PointOperations) -> &'static str {
    match operation {
        PointOperations::UpsertPoints(_)
        | PointOperations::UpsertPointsConditional(_)
        | PointOperations::DeletePoints { .. }
        | PointOperations::DeletePointsByFilter(_)
        | PointOperations::SyncPoints(_) => "L02",
        // Native byte storage transport does not introduce a binary/bit vector
        // similarity input. Its nested types are not public root re-exports.
        PointOperations::UpsertPointsRaw(_) | PointOperations::SyncPointsRaw(_) => {
            "L02/V05 audit; raw storage API excluded from SQL v0.1"
        }
    }
}

pub fn insertion_mapping(operation: &PointInsertOperations) -> &'static str {
    match operation {
        PointInsertOperations::PointsBatch(_) | PointInsertOperations::PointsList(_) => "L02",
    }
}

pub fn vector_operation_mapping(operation: &VectorOperations) -> &'static str {
    match operation {
        VectorOperations::UpdateVectors(_)
        | VectorOperations::DeleteVectors(_, _)
        | VectorOperations::DeleteVectorsByFilter(_, _) => "L02",
    }
}

pub fn payload_operation_mapping(operation: &PayloadOps) -> &'static str {
    match operation {
        PayloadOps::SetPayload(_)
        | PayloadOps::DeletePayload(_)
        | PayloadOps::ClearPayload { .. }
        | PayloadOps::ClearPayloadByFilter(_)
        | PayloadOps::OverwritePayload(_) => "L02",
    }
}

pub fn field_index_operation_mapping(operation: &FieldIndexOperations) -> &'static str {
    match operation {
        FieldIndexOperations::CreateIndex(_) | FieldIndexOperations::DeleteIndex(_) => "L02/Q12",
    }
}

pub fn vector_name_operation_mapping(operation: &VectorNameOperations) -> &'static str {
    match operation {
        VectorNameOperations::CreateVectorName(_) | VectorNameOperations::DeleteVectorName(_) => {
            "L02/Q14"
        }
    }
}

pub fn vector_name_config_mapping(config: &VectorNameConfig) -> &'static str {
    match config {
        VectorNameConfig::Dense(_) | VectorNameConfig::Sparse(_) => "Q14/L02",
    }
}

pub fn fusion_mapping(fusion: &Fusion) -> &'static str {
    match fusion {
        Fusion::Rrf { .. } | Fusion::Dbsf => "Q03",
    }
}

pub fn idf_mapping(params: &IdfParams) -> &'static str {
    match params {
        IdfParams::Scope(IdfScope::Global) | IdfParams::Corpus(_) => "Q14",
    }
}

pub fn expression_mapping(expression: &Expression) -> &'static str {
    match expression {
        Expression::Constant(_)
        | Expression::Variable(_)
        | Expression::Condition(_)
        | Expression::GeoDistance { .. }
        | Expression::Datetime(_)
        | Expression::DatetimeKey(_)
        | Expression::Mult(_)
        | Expression::Sum(_)
        | Expression::Neg(_)
        | Expression::Div { .. }
        | Expression::Sqrt(_)
        | Expression::Pow { .. }
        | Expression::Exp(_)
        | Expression::Log10(_)
        | Expression::Ln(_)
        | Expression::Abs(_)
        | Expression::Decay { .. } => "Q08",
    }
}

/// Reference public methods even where runtime scenarios are still pending.
/// Inferred return types avoid importing unnameable implementation types.
fn lifecycle_and_read_compile_surface() {
    let _ = EdgeShard::new;
    let _ = EdgeShard::load;
    let _ = EdgeShard::config;
    let _ = EdgeShard::flush;
    let _ = EdgeShard::update;
    let _ = EdgeShard::optimize;
    let _ = EdgeShard::set_hnsw_config;
    let _ = EdgeShard::set_vector_hnsw_config;
    let _ = EdgeShard::set_optimizers_config;
    let _ = EdgeShard::query;
    let _ = EdgeShard::retrieve;
    let _ = EdgeShard::scroll;
    let _ = EdgeShard::count;
    let _ = EdgeShard::facet;
    let _ = EdgeShard::info;
    let _ = <EdgeShard as EdgeShardRead>::search_matrix;
    let _ = <EdgeShard as EdgeShardRead>::query_groups;
    let _ = EdgeShard::unpack_snapshot;
    let _ = EdgeShard::snapshot_manifest;
    let _ = EdgeShard::recover_partial_snapshot;
    let _ = ReadOnlyEdgeShard::open_mmap;
    let _ = UpdateOnlyEdgeShard::open_mmap;
    let _: Option<LoadProfile> = None;
}
