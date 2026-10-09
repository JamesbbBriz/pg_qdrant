"""Real directional phrase expansion over the same authorized durable snapshot."""
import copy
import hashlib
import json
import os
import signal
import time


def run(sql, ready, ticket_from, checks, spawn=None):
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    policy = {'id': 'transport', 'revision': 1, 'rules': [
        {'from': ['car'], 'to': [['automobile'], ['motor', 'vehicle']]},
        {'from': ['automobile'], 'to': [['limousine']]},
        {'from': ['database'], 'to': [['数据库']]}]}
    sql('CREATE TABLE synonym_docs(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    bodies = {1: 'car insurance', 2: 'automobile insurance', 3: 'motor vehicle insurance',
              4: 'insurance automobile', 5: 'automobile cheap insurance', 6: '数据库 恢复',
              7: 'car insurance', 8: 'limousine insurance'}
    sql('INSERT INTO synonym_docs VALUES ' + ','.join(
        '('+str(key)+','+literal(body)+','+literal('Denied' if key == 7 else 'Allow')+')'
        for key, body in bodies.items()))
    sql("SELECT qdrant.create_index('synonym_docs','synonym_docs','id',"
        "'{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"kind\":\"keyword\",\"field\":\"category\"}}}')")
    def statement(q='car insurance', p=None, options=None, k=100):
        return 'SELECT qdrant.search_synonyms(\'synonym_docs\','+literal(q)+','+literal(
            json.dumps(policy if p is None else p, ensure_ascii=False))+','+str(k)+','+literal(json.dumps(options or {}))+')'
    def query(**kw):
        return json.loads(sql(statement(**kw)))
    def keys(result):
        return {int(h['source_key']['value']) for h in result['hits']}
    def replay():
        ready('synonym_docs')
        result = query()
        assert keys(result) == {1, 2, 3, 7} and result['matched_points'] == 4, result
        assert result['matched_count_exact'] and not result['release_supported'], result
        evidence = result['synonym_policy']
        assert evidence['id'] == policy['id'] and evidence['revision'] == 1, evidence
        assert len(evidence['sha256']) == 64 and evidence['applied_rules'] == 1, evidence
        assert len(evidence['expanded_phrases']) == 3 and evidence['max_phrases'] == 32, evidence
        for hit in result['hits']:
            key = int(hit['source_key']['value'])
            snippet = hit['snippet']
            assert snippet['source_fingerprint'] == hashlib.sha256(bodies[key].encode()).hexdigest(), snippet
            assert snippet['status'] == 'ready' and snippet['text'] == bodies[key], snippet
            raw = snippet['text'].encode()
            terms = [raw[r['start']:r['end']].decode() for r in snippet['highlights']]
            assert terms and all(term in bodies[key].split() for term in terms), snippet
        assert keys(query(options={'filter': {'field': 'category', 'eq': 'Allow'}})) == {1, 2, 3}
        assert query(k=1)['matched_points'] == 4 and len(query(k=1)['hits']) == 1
        assert keys(query(q='automobile insurance')) == {2, 8}  # No reverse car rule.
        assert keys(query(q='database 恢复')) == {6}
        return evidence['sha256']
    digest = replay()
    reordered = copy.deepcopy(policy); reordered['rules'].reverse()
    assert query(p=reordered)['synonym_policy']['sha256'] == digest
    revised = copy.deepcopy(policy); revised['revision'] = 2
    assert query(p=revised)['synonym_policy']['sha256'] != digest
    longest = {'id': 'longest', 'revision': 1, 'rules': [
        {'from': ['motor', 'vehicle'], 'to': [['car']]},
        {'from': ['motor'], 'to': [['engine']]}]}
    assert keys(query(q='motor vehicle insurance', p=longest)) == {1, 3, 7}
    checks.append('directional versioned synonyms execute complete native contiguous phrase alternatives, retain originals, prefer longest matches without recursion/reverse expansion, and include Unicode literal words')
    checks.append('synonym content digest is canonical across rule order, revisions are explicit, real-source highlights and filtered full-domain counts precede top-k')
    owner_before_invalid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    invalid = [[], {}, {'id': 'x', 'revision': 1, 'rules': []},
               {'id': 'x', 'revision': 1, 'rules': [[['car'], [['automobile']]]]},
               {'id': 'x', 'revision': 1, 'rules': [None]},
               {'id': 'x', 'revision': 1, 'rules': [
                   {'from': ['car'], 'to': [['automobile']], 'unknown': True}]}]
    for field, value in [('id', '../private'), ('revision', 0), ('extra', True)]:
        wrong = copy.deepcopy(policy); wrong[field] = value; invalid.append(wrong)
    for from_words, to in [([], [['word']]), (['a'], []), (['SKU-123'], [['word']]),
                           (['a b'], [['word']]), (['a'], [[]]), (['a'], [['*']])]:
        invalid.append({'id': 'bad', 'revision': 1, 'rules': [{'from': from_words, 'to': to}]})
    duplicate = copy.deepcopy(policy); duplicate['rules'].append({'from': ['CAR'], 'to': [['vehicle']]}); invalid.append(duplicate)
    for p in invalid:
        assert '22023' in sql(statement(p=p), ok=False), p
    for q in ['', 'SKU-123', 'car *', 'car "insurance"', 'cafe\u0301']:
        assert '22023' in sql(statement(q=q), ok=False), q
    excess = {'id': 'bounded', 'revision': 1, 'rules': [{'from': ['a'], 'to': [['b'], ['c'], ['d'], ['e']]}]}
    assert '54000' in sql(statement(q='a a a', p=excess), ok=False)
    boundary = {'id': 'boundary', 'revision': 1, 'rules': [{'from': ['a'], 'to': [['b']]}]}
    exact = query(q='a a a a a x y z', p=boundary)
    assert exact['matched_points'] == 0 and not exact['hits'], exact
    assert len(exact['synonym_policy']['expanded_phrases']) == 32, exact
    assert sum(map(len, exact['synonym_policy']['expanded_phrases'])) == 256, exact
    boundary['rules'][0]['to'] = [['b', 'c']]
    assert '54000' in sql(statement(q='a a a a a x y z', p=boundary), ok=False)
    assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_instance'] == owner_before_invalid['engine_instance']
    replay()
    checks.append('malformed/duplicate synonym policies and identifier/operator inputs refuse; combinatorial expansion overflow rejects the whole query without truncation')
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(), ok=False)
    sql('CREATE ROLE pgq_synonym_reader; GRANT SELECT ON synonym_docs TO pgq_synonym_reader')
    assert '42501' in sql('SET ROLE pgq_synonym_reader; '+statement(), ok=False)
    sql('GRANT builder TO pgq_synonym_reader')
    assert keys(json.loads(sql('SET ROLE pgq_synonym_reader; '+statement()))) == {1, 2, 3, 7}
    sql('REVOKE builder FROM pgq_synonym_reader')
    assert '0A000' in sql('BEGIN ISOLATION LEVEL REPEATABLE READ; '+statement(), ok=False)
    assert '55000' in sql("BEGIN; UPDATE synonym_docs SET body='uncommitted' WHERE id=1; "+statement(), ok=False)
    checks.append('synonym queries enforce registered/inherited owner and source SELECT, refuse table-only/writer roles, uncommitted changes and unsupported snapshots')
    ticket = ticket_from(sql("BEGIN; DELETE FROM synonym_docs WHERE id=2; INSERT INTO synonym_docs VALUES(2,'bus insurance','Allow'); SELECT qdrant.track_changes('synonym_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
    assert keys(query()) == {1, 3, 7}
    sql("UPDATE synonym_docs SET body='automobile insurance' WHERE id=2"); replay()
    checks.append('ordinary committed DML and primary-key reuse replace synonym candidate facts only after exact durable tickets')
    if spawn is not None:
        for change in ['source', 'cancel']:
            owner = json.loads(sql('SELECT qdrant_internal.p0_ping()')); pid = owner['engine_pid']
            os.kill(pid, signal.SIGSTOP)
            try:
                application = 'pgq_synonyms_' + change
                waiting = spawn(statement(), application)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    busy = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation') == 'source_statistics': break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation') == 'source_statistics', busy
                if change == 'source': sql("UPDATE synonym_docs SET body='changed' WHERE id=1")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'") == 't'
                    out, err = waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err, (out, err)
                    retained = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained.get('active') and retained['engine_instance'] == owner['engine_instance'], retained
            finally: os.kill(pid, signal.SIGCONT)
            if change == 'source':
                out, err = waiting.communicate(timeout=10)
                assert waiting.returncode and '55000' in err, (out, err)
                sql("UPDATE synonym_docs SET body='car insurance' WHERE id=1")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                completed = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'): break
                time.sleep(.02)
            assert not completed.get('active'), completed
            replay()
        checks.append('in-flight synonym source mutation invalidates the entire response; SQL cancellation preserves native ownership until completion')
    sql('ALTER TABLE synonym_docs ENABLE ROW LEVEL SECURITY')
    assert '0A000' in sql(statement(), ok=False)
    sql('ALTER TABLE synonym_docs DISABLE ROW LEVEL SECURITY')
    task = json.loads(sql("SELECT qdrant.drop_index('synonym_docs')"))
    assert json.loads(sql("SELECT qdrant.await_task('"+task['task_id']+"',60000)"))['succeeded']
    sql("SELECT qdrant.create_index('synonym_docs','synonym_docs','id',"
        "'{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"kind\":\"keyword\",\"field\":\"category\"}}}')")
    replay()
    checks.append('RLS synonym sources refuse explicitly; trusted re-registration after DDL rebuilds the same query results')
    return replay
