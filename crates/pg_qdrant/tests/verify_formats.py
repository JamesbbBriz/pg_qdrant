"""P0 format assertions, invoked only by the disposable PostgreSQL harness."""

import re


def exercise_formats(scalar, jsql, run, expect_error, record):
    signature = "qdrant_internal.p0_vector_formats(real[],bigint[],real[],real[])"
    assert scalar("SELECT provolatile='v' AND proparallel='u' AND NOT prosecdef AND NOT proisstrict "
                  f"FROM pg_proc WHERE oid='{signature}'::regprocedure") == "t"
    base = ["ARRAY[1,-0.0,0.25]::real[]", "ARRAY[0,4294967295]::bigint[]",
            "ARRAY[0.5,-1]::real[]", "ARRAY[[1,2],[3,4]]::real[]"]

    def query(args):
        return "SELECT qdrant_internal.p0_vector_formats(" + ",".join(args) + ")"

    result = jsql(query(base))
    assert result["kind"] == "p0_candidate_vector_formats"
    assert result["shape"] == {"dense": [3], "sparse_nnz": 2, "tokens": [2, 2]}
    assert result["values"]["dense"] == [1, 0, 0.25]
    assert result["values"]["sparse_indices"] == [0, 4294967295]
    assert result["values"]["sparse_weights"] == [0.5, -1]
    assert result["values"]["tokens"] == [[1, 2], [3, 4]]
    assert result["owned_scalar_bytes"] == (3 + 2 + 4 + 2) * 4
    assert 0 < result["wire_bytes"] <= result["limits"]["wire_bytes"] == 262144
    assert result["float32_bitwise_roundtrip"] is True
    assert all(result[field] is False for field in ["engine_executed", "ipc_executed",
               "model_contract_verified", "public_api_frozen", "postgres_input_memory_bounded",
               "release_supported"])
    extremes = base.copy()
    extremes[0] = "ARRAY['-0'::real,'1.40129846e-45'::real,'3.402823466e38'::real]"
    exact = jsql(query(extremes))
    # JSONB numeric storage normalizes negative zero; inspect explicit internal
    # float32 bits rather than the rendered JSONB number's sign.
    assert exact["values"]["dense"][0] == 0
    assert exact["internal_dense_f32_bits_prefix"] == [0x80000000, 0x00000001, 0x7F7FFFFF]
    assert exact["float32_bitwise_roundtrip_scope"] == "owned_json_before_postgresql_jsonb"
    assert exact["values"]["dense"][1] > 0
    assert exact["float32_bitwise_roundtrip"] is True
    # Driver-style binding must preserve the matrix's real runtime dimensions.
    prepared = ("PREPARE pgq_formats(real[],bigint[],real[],real[]) AS "
                "SELECT qdrant_internal.p0_vector_formats($1,$2,$3,$4); "
                "EXECUTE pgq_formats(" + ",".join(base) + "); DEALLOCATE pgq_formats")
    assert jsql(prepared)["values"] == result["values"]
    record("candidate_vector_format_roundtrip", wire_bytes=result["wire_bytes"],
           signed_zero_and_finite_float32_extremes=True, public_api_frozen=False)

    cases = []

    def reject(field, expression, state, label):
        args = base.copy()
        args[field] = expression
        failure = run(query(args), check=False)
        assert failure.returncode != 0 and re.search(rf"\b{state}\b", failure.stderr), failure.stderr
        if label != "postgres_ragged_matrix":
            assert "P0 vector format rejected:" in failure.stderr, failure.stderr
            assert "DETAIL:" in failure.stderr, failure.stderr
        cases.append(label)

    for field, kind in enumerate(["real[]", "bigint[]", "real[]", "real[]"]):
        reject(field, f"NULL::{kind}", "22004", f"null_argument_{field}")
        reject(field, f"ARRAY[NULL]::{kind}", "22004", f"null_element_{field}")
        reject(field, f"ARRAY[]::{kind}", "22023", f"empty_{field}")
    reject(0, "'[0:1]={1,2}'::real[]", "22023", "dense_lower_bound")
    reject(1, "'[2:3]={0,1}'::bigint[]", "22023", "sparse_index_lower_bound")
    reject(2, "'[-1:0]={1,2}'::real[]", "22023", "sparse_weight_lower_bound")
    reject(3, "'[0:1][1:2]={{1,2},{3,4}}'::real[]", "22023", "token_row_lower_bound")
    reject(3, "'[1:2][0:1]={{1,2},{3,4}}'::real[]", "22023", "token_column_lower_bound")
    reject(0, "ARRAY[[1,2]]::real[]", "22023", "dense_not_vector")
    reject(1, "ARRAY[[0,1]]::bigint[]", "22023", "indices_not_vector")
    reject(2, "ARRAY[[1,2]]::real[]", "22023", "weights_not_vector")
    reject(3, "ARRAY[1,2]::real[]", "22023", "tokens_not_matrix")
    reject(3, "ARRAY[[[1,2]]]::real[]", "22023", "tokens_three_dimensions")
    reject(1, "ARRAY[-1,1]::bigint[]", "22023", "negative_index")
    reject(1, "ARRAY[0,4294967296]::bigint[]", "22023", "index_u32_overflow")
    reject(1, "ARRAY[1,1]::bigint[]", "22023", "duplicate_indices")
    reject(1, "ARRAY[2,1]::bigint[]", "22023", "unsorted_indices")
    reject(2, "ARRAY[1]::real[]", "22023", "sparse_length_mismatch")
    for value in ["NaN", "Infinity", "-Infinity"]:
        for field in [0, 2, 3]:
            expression = f"ARRAY['{value}'::real]"
            if field == 3:
                expression = f"ARRAY[['{value}'::real]]"
            reject(field, expression, "22023", f"nonfinite_{field}_{value}")
    # PostgreSQL itself rejects ragged dimensions before the extension is called.
    reject(3, "ARRAY[ARRAY[1,2],ARRAY[3]]::real[]", "2202E", "postgres_ragged_matrix")
    reject(0, "array_fill(0::real,ARRAY[4097])", "53400", "dense_budget")
    reject(1, "ARRAY(SELECT i::bigint FROM generate_series(0,4096) i)", "53400", "sparse_budget")
    reject(3, "array_fill(0::real,ARRAY[129,1])", "53400", "token_row_budget")
    reject(3, "array_fill(0::real,ARRAY[1,1025])", "53400", "token_width_budget")
    reject(3, "array_fill(0::real,ARRAY[128,129])", "53400", "token_total_budget")
    maximum = ["array_fill(0::real,ARRAY[4096])",
               "ARRAY(SELECT i::bigint FROM generate_series(0,4095) i)",
               "array_fill(0::real,ARRAY[4096])", "array_fill(0::real,ARRAY[128,128])"]
    largest = jsql(query(maximum))
    assert largest["shape"] == {"dense": [4096], "sparse_nnz": 4096, "tokens": [128, 128]}
    assert largest["owned_scalar_bytes"] <= largest["limits"]["owned_scalar_bytes"]
    maximum[0] = "array_fill('3.402823466e38'::real,ARRAY[4096])"
    maximum[2] = "array_fill('3.402823466e38'::real,ARRAY[4096])"
    maximum[3] = "array_fill('3.402823466e38'::real,ARRAY[128,128])"
    expect_error(query(maximum), "53400")
    cases.append("wire_byte_budget")
    assert jsql(query(base))["values"] == result["values"]
    record("candidate_vector_format_rejections_and_budgets", cases=cases,
           recovery_after_errors=True, limits=result["limits"])

    run("CREATE ROLE pgq_p0_format_unprivileged")
    expect_error("SET ROLE pgq_p0_format_unprivileged; " + query(base), "42501")
    run("GRANT USAGE ON SCHEMA qdrant_internal TO pgq_p0_format_unprivileged; "
        f"GRANT EXECUTE ON FUNCTION {signature} TO pgq_p0_format_unprivileged")
    expect_error("SET ROLE pgq_p0_format_unprivileged; " + query(base), "42501")
    null_args = ["NULL::real[]", "NULL::bigint[]", "NULL::real[]", "NULL::real[]"]
    expect_error("SET ROLE pgq_p0_format_unprivileged; " + query(null_args), "42501")
    run("DROP OWNED BY pgq_p0_format_unprivileged; DROP ROLE pgq_p0_format_unprivileged")
    record("candidate_vector_format_acl_and_runtime_superuser", called_on_null=True)
