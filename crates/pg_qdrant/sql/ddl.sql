-- Conservative invalidation is durable even if capture is later re-enabled.
-- Source facts remain writable; an invalidated index cannot serve or ACK.
CREATE FUNCTION qdrant_internal.source_ddl_end() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF to_regclass('qdrant_internal.index_catalog') IS NULL THEN RETURN; END IF;
 UPDATE qdrant_internal.index_catalog i SET capture_state='degraded'
 FROM pg_event_trigger_ddl_commands() d
 LEFT JOIN pg_trigger t ON d.classid='pg_trigger'::regclass AND t.oid=d.objid
 WHERE NOT d.in_extension AND ((d.classid='pg_class'::regclass
   AND d.objid=i.source_oid) OR t.tgrelid=i.source_oid);
END $$;
CREATE EVENT TRIGGER pg_qdrant_source_ddl_end ON ddl_command_end
 WHEN TAG IN ('ALTER TABLE','ALTER TRIGGER')
 EXECUTE FUNCTION qdrant_internal.source_ddl_end();

CREATE FUNCTION qdrant_internal.source_sql_drop() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF to_regclass('qdrant_internal.index_catalog') IS NULL THEN RETURN; END IF;
 UPDATE qdrant_internal.index_catalog i SET capture_state='degraded'
 FROM pg_event_trigger_dropped_objects() d
 WHERE (d.classid='pg_class'::regclass AND d.objid=i.source_oid)
    OR (d.object_type='trigger' AND d.address_names[1]=i.source_schema
        AND d.address_names[2]=i.source_name);
END $$;
CREATE EVENT TRIGGER pg_qdrant_source_sql_drop ON sql_drop
 EXECUTE FUNCTION qdrant_internal.source_sql_drop();
REVOKE ALL ON FUNCTION qdrant_internal.source_ddl_end(),
 qdrant_internal.source_sql_drop() FROM PUBLIC;
