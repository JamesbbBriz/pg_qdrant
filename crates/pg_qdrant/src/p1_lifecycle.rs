//! Installed transactional source ledger and management APIs.
pgrx::extension_sql_file!(
    "../sql/ledger.sql",
    name = "p1_transaction_capture",
    requires = [qdrant, qdrant_internal]
);
pgrx::extension_sql_file!(
    "../sql/representations.sql",
    name = "p1_representations",
    requires = ["p1_management"]
);
pgrx::extension_sql_file!(
    "../sql/consumer.sql",
    name = "p1_consumer_contract",
    requires = ["p1_representations"]
);
pgrx::extension_sql_file!(
    "../sql/generations.sql",
    name = "p1_native_generations",
    requires = ["p1_consumer_contract", "p3_generation_reservations"]
);
pgrx::extension_sql_file!(
    "../sql/management.sql",
    name = "p1_management",
    requires = ["p1_transaction_capture"]
);
pgrx::extension_sql_file!(
    "../sql/ddl.sql",
    name = "p1_source_ddl",
    requires = ["p1_transaction_capture"]
);
