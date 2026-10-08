//! Compile sentinels for explicitly enumerated public Edge 0.8.0 types.
//!
//! The enum matches deliberately have no wildcard arms: additions to those
//! enums require a mapping decision. This does not enumerate every public
//! method, nested implementation type, struct field, or valid combination.
//! Method references and typed constructors below are compile-only evidence;
//! they do not prove invocation, persistence, query behavior, or SQL support.

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

/// Public facet result kinds; point/document scope and permission checks remain
/// the statistics planner's responsibility (F19/Q11), not this enum mapping.
pub fn facet_value_mapping(value: &FacetValue) -> &'static str {
    match value {
        FacetValue::Keyword(_) | FacetValue::Int(_) | FacetValue::Uuid(_) | FacetValue::Bool(_) => {
            "Q11/F19"
        }
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

pub fn compression_ratio_mapping(ratio: CompressionRatio) -> &'static str {
    match ratio {
        CompressionRatio::X4
        | CompressionRatio::X8
        | CompressionRatio::X16
        | CompressionRatio::X32
        | CompressionRatio::X64 => "V04; product quantization configuration only",
    }
}

pub fn scalar_type_mapping(kind: ScalarType) -> &'static str {
    match kind {
        ScalarType::Int8 => "V04; scalar quantization, not V05 uint8 source storage",
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

pub fn update_mode_mapping(mode: UpdateMode) -> &'static str {
    match mode {
        UpdateMode::Upsert | UpdateMode::InsertOnly | UpdateMode::UpdateOnly => "L02",
    }
}

/// Update-only batch preview outcomes. This does not make its restricted batch
/// API equivalent to EdgeShard::update or provide WAL/PG transaction semantics.
pub fn point_action_mapping(action: &PointAction) -> &'static str {
    match action {
        PointAction::Store(_) | PointAction::Delete | PointAction::Skip | PointAction::Missing => {
            "L02/L04; update-only batch preview"
        }
    }
}

/// Manifest metadata is an engine segment state, not the extension's index
/// generation state or proof of an atomic reader cutover.
pub fn segment_manifest_state_mapping(state: &SegmentManifestState) -> &'static str {
    match state {
        SegmentManifestState::Active
        | SegmentManifestState::UnderConstruction
        | SegmentManifestState::Optimizing {
            holder: _,
            lease_until: _,
        }
        | SegmentManifestState::Retiring => "L04; manifest/read-only lifecycle audit",
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

pub fn range_mapping(range: &RangeInterface) -> &'static str {
    match range {
        RangeInterface::Float(_) | RangeInterface::DateTime(_) => "Q12",
    }
}

/// A projection is not an authorization filter. SQL-owned result policy must
/// independently enforce permissions and remove unrequested fields/vectors.
pub fn payload_projection_mapping(projection: &WithPayloadInterface) -> &'static str {
    match projection {
        WithPayloadInterface::Bool(_) | WithPayloadInterface::Fields(_) => "Q10/L08",
        WithPayloadInterface::Selector(selector) => payload_selector_mapping(selector),
    }
}

pub fn payload_selector_mapping(selector: &PayloadSelector) -> &'static str {
    match selector {
        PayloadSelector::Include(_) | PayloadSelector::Exclude(_) => "Q10/L08",
    }
}

pub fn vector_projection_mapping(projection: &WithVector) -> &'static str {
    match projection {
        WithVector::Bool(_) | WithVector::Selector(_) => "Q10/L08",
    }
}

pub fn order_by_interface_mapping(order_by: &OrderByInterface) -> &'static str {
    match order_by {
        OrderByInterface::Key(_) | OrderByInterface::Struct(_) => "Q10",
    }
}

pub fn direction_mapping(direction: Direction) -> &'static str {
    match direction {
        Direction::Asc | Direction::Desc => "Q10",
    }
}

pub fn start_from_mapping(start: &StartFrom) -> &'static str {
    match start {
        StartFrom::Integer(_) | StartFrom::Float(_) | StartFrom::Datetime(_) => "Q10",
    }
}

pub fn order_value_mapping(value: &OrderValue) -> &'static str {
    match value {
        OrderValue::Int(_) | OrderValue::Float(_) => "Q10",
    }
}

pub fn decay_kind_mapping(kind: DecayKind) -> &'static str {
    match kind {
        DecayKind::Lin | DecayKind::Gauss | DecayKind::Exp => "Q08",
    }
}

pub fn sample_mapping(sample: Sample) -> &'static str {
    match sample {
        Sample::Random => "Q10",
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

/// Typed input construction only (V04), with no quantized index built or query
/// run. Published From implementations avoid naming the unexported wrapper
/// types. `memory: None` is inferred: the underlying Memory enum is not part of
/// the root reexports, so its placement variants are not covered here. Turbo's
/// nested config/bit type is likewise not publicly reexported; only the outer
/// QuantizationConfig::Turbo match above is covered.
#[allow(deprecated)] // The published config literals require always_ram.
pub fn construct_quantization_inputs() -> [QuantizationConfig; 3] {
    let scalar = ScalarQuantizationConfig {
        r#type: ScalarType::Int8,
        quantile: Some(0.99),
        always_ram: None,
        memory: None,
    };
    let product = ProductQuantizationConfig {
        compression: CompressionRatio::X4,
        always_ram: None,
        memory: None,
    };
    let binary = BinaryQuantizationConfig {
        always_ram: None,
        memory: None,
        encoding: Some(BinaryQuantizationEncoding::OneBit),
        query_encoding: Some(BinaryQuantizationQueryEncoding::Scalar8Bits),
    };
    [scalar.into(), product.into(), binary.into()]
}

/// Typed search options only (V04/Q13). ACORN enablement is conditional on the
/// real HNSW/filter path; constructing these flags does not prove it ran.
/// Rescoring uses the retained storage precision, not restored float32 inputs.
pub fn construct_approximate_search_inputs() -> SearchParams {
    SearchParams {
        hnsw_ef: Some(32),
        exact: false,
        quantization: Some(QuantizationSearchParams {
            ignore: false,
            rescore: Some(true),
            oversampling: Some(2.0),
        }),
        indexed_only: false,
        acorn: Some(AcornSearchParams {
            enable: true,
            max_selectivity: Some(external::ordered_float::OrderedFloat(0.4)),
        }),
        idf: None,
    }
}

/// Compile-only public LoadProfile constructors and merge (L04/V05). No shard
/// is opened, no manifest follower is refreshed, and no warm/cold behavior is
/// measured. Mutable components can ignore this profile in the fixed release.
pub fn construct_load_profiles(filter: &Filter, order_key: &JsonPath) -> [LoadProfile; 4] {
    let search = LoadProfile::for_search("dense", Some(filter), false);
    let scroll = LoadProfile::for_scroll(Some(filter), Some(order_key), true);
    let retrieve = LoadProfile::for_retrieve();
    let mut combined = search.clone();
    combined.merge(scroll.clone());
    combined.merge(retrieve.clone());
    [search, scroll, retrieve, combined]
}

/// Method references only, including paths whose runtime scenarios are pending.
/// Inferred return types avoid importing unnameable implementation types. A new
/// method will not cause this list to fail compilation; keep the source audit.
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
}
