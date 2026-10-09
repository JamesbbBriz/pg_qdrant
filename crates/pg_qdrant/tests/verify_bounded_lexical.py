"""Actual mature lexical queries over durable, authorized Edge facts."""
import json
import hashlib
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    sql('CREATE TABLE bounded_lexical(id text PRIMARY KEY,body text NOT NULL,category text)')
    sql("INSERT INTO bounded_lexical VALUES('1','transaction recovery','Allow'),('2','transaction durable recovery','Allow'),"
        "('3','recovery transaction','Denied'),('4','dairy diary','Allow'),('ERR-0042','identifier only','Allow')")
    sql("SELECT qdrant.create_index('bounded_lexical','bounded_lexical','id','{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"kind\":\"keyword\",\"field\":\"category\"}}}')")
    def statement(q='transactoin',kind='fuzzy',slop=0,options=None,index='bounded_lexical',k=100):
        return 'SELECT qdrant.search_lexical('+literal(index)+','+literal(q)+','+literal(kind)+','+str(k)+','+str(slop)+','+literal(json.dumps(options or {}))+')'
    def query(**kw):return json.loads(sql(statement(**kw)))
    def keys(result):return {h['source_key']['value'] for h in result['hits']}
    def validate_snippet(hit,original):
        snippet=hit['snippet']
        assert snippet['source_field']=='body' and snippet['source_fingerprint']==hashlib.sha256(original.encode()).hexdigest(),snippet
        if snippet['status']!='ready':
            assert snippet['status'] in ['no_positive_term_match','fragment_budget_exceeded'],snippet
            assert 'text' not in snippet and 'highlights' not in snippet,snippet
            return []
        text=snippet['text'];encoded=text.encode();source=original.encode()
        assert encoded in source and len(encoded)<=512 and len(snippet['highlights'])<=32,snippet
        assert snippet['offset_unit']=='utf8_bytes' and snippet['offset_scope']=='fragment',snippet
        if snippet['source_occurrence_ambiguous']:
            assert snippet['source_byte_start'] is None and source.find(encoded)!=source.rfind(encoded),snippet
        else:
            start=snippet['source_byte_start']
            assert source[start:start+len(encoded)]==encoded and source.find(encoded)==source.rfind(encoded),snippet
        terms=[]
        for r in snippet['highlights']:
            assert 0<=r['start']<r['end']<=len(encoded),snippet
            terms.append(encoded[r['start']:r['end']].decode())
        assert terms and 'html' not in snippet,snippet
        return terms
    def replay():
        ready('bounded_lexical')
        bodies=json.loads(sql('SELECT jsonb_object_agg(id,body) FROM bounded_lexical'))
        for q,kind,slop,expected in [('transactoin','fuzzy',0,{'1','2','3'}),('transaction recovery','proximity',0,{'1'}),
            ('transaction recovery','proximity',1,{'1','2'}),('transaction recovery','proximity',2,{'1','2','3'})]:
            result=query(q=q,kind=kind,slop=slop)
            assert keys(result)==expected and result['matched_points']==len(expected),result
            assert result['matched_count_exact'] and result['engine']=='tantivy-0.26.2' and not result['release_supported'],result
            assert all('id' not in h and 'payload' not in h for h in result['hits']),result
            for hit in result['hits']:
                terms=validate_snippet(hit,bodies[hit['source_key']['value']])
                assert terms and all(term.lower() in ['transaction','recovery'] for term in terms),hit
        result=query(options={'filter':{'field':'category','eq':'Allow'}})
        assert keys(result)=={'1','2'} and result['filtered_points']==4,result
        assert query(q='unmatched')['matched_points']==0
        for q,expected in [('body:transaction AND NOT durable',{'1','3'}),
            ('(transaction OR diary) AND NOT durable',{'1','3','4'}),
            ('transaction recovery',{'1','2','3'}),('body:"transaction recovery"',{'1'}),
            ('"transaction recovery"~1',{'1','2'}),('NOT durable',{'1','3','4','ERR-0042'})]:
            syntax=query(q=q,kind='syntax')
            assert keys(syntax)==expected and syntax['matched_points']==len(expected),syntax
            assert 'default AND' in syntax['syntax_policy'],syntax
            for hit in syntax['hits']:
                terms=validate_snippet(hit,bodies[hit['source_key']['value']])
                assert 'durable' not in [t.lower() for t in terms],hit
        boosted=query(q='diary^10 OR transaction',kind='syntax')
        assert boosted['hits'][0]['source_key']['value']=='4',boosted
        filtered=query(q='NOT durable',kind='syntax',options={'filter':{'field':'category','eq':'Allow'}})
        assert keys(filtered)=={'1','4','ERR-0042'},filtered
        return result
    replay()
    checks.append('Tantivy fuzzy transposition and native total-movement proximity goldens execute through installed SQL with typed source keys and pre-candidate scalar filters')
    checks.append('strict native syntax executes Boolean AND/OR/NOT, default AND, body scoping, quoted phrases/slop and boosts; complements stay inside the prefiltered authorized snapshot')
    invalid_syntax=['*','body:*','point_id:1','other:word','body:[a TO z]','body:IN [a b]','/tr.*/',
        'word*','"word phrase"*','word^0','word^11','"one two"~9','(word','word AND','"unterminated','"!!!"',
        ' OR '.join('word'+chr(c) for c in range(ord('a'),ord('q')+1)),
        ' OR '.join('"a b c'+chr(c)+'"' for c in range(ord('a'),ord('l')+1))]
    nested='word'
    for _ in range(12):nested='(word OR '+nested+')'
    invalid_syntax.append(nested)
    for q in invalid_syntax:assert '22023' in sql(statement(q=q,kind='syntax'),ok=False),q
    assert '22023' in sql(statement(q='word',kind='syntax',slop=1),ok=False)
    sql("UPDATE bounded_lexical SET body='数据库 恢复 foo bar' WHERE id='ERR-0042'");ready('bounded_lexical')
    assert keys(query(q='恢复',kind='syntax'))=={'ERR-0042'}
    assert keys(query(q=r'body:foo\:bar',kind='syntax'))=={'ERR-0042'}
    sql("UPDATE bounded_lexical SET body='identifier only' WHERE id='ERR-0042'");replay()
    checks.append('strict syntax refuses unsupported fields/expansions/range/regex/set/all, malformed syntax, empty analysis and structural/term budgets; literal Unicode and escapes use native analysis')
    original="Préface 🙂 <script> Café CAFÉ cafe\u0301 end"
    sql("UPDATE bounded_lexical SET body="+literal(original)+" WHERE id='ERR-0042'");ready('bounded_lexical')
    accented=query(q='café',kind='syntax')
    assert keys(accented)=={'ERR-0042'},accented
    assert validate_snippet(accented['hits'][0],original)==['Café','CAFÉ'],accented
    assert '<script>' in accented['hits'][0]['snippet']['text'],accented
    original='word '*80
    sql("UPDATE bounded_lexical SET body="+literal(original)+" WHERE id='ERR-0042'");ready('bounded_lexical')
    repeated=query(q='word',kind='syntax')['hits'][0]
    validate_snippet(repeated,original)
    assert repeated['snippet']['source_occurrence_ambiguous'] and repeated['snippet']['source_byte_start'] is None,repeated
    sql("UPDATE bounded_lexical SET body="+literal('a '*80)+" WHERE id='ERR-0042'");ready('bounded_lexical')
    limited=query(q='a',kind='syntax')
    assert limited['matched_points']==1 and limited['hits'][0]['snippet']['status']=='fragment_budget_exceeded',limited
    sql("UPDATE bounded_lexical SET body='identifier only' WHERE id='ERR-0042'");replay()
    checks.append('native positive-term snippets preserve original UTF8 bytes/case/markup without HTML, bind current source fingerprint, omit ambiguous absolute offsets and explicitly bound fragments/ranges')
    for kw in [dict(q='ERR-0042'),dict(q='中文'),dict(q='a'),dict(q='two words'),dict(kind='unknown'),dict(slop=1),
               dict(q='transaction recovery',kind='proximity',slop=9),dict(k=101),dict(k=0),dict(options={'unknown':1})]:
        assert '22023' in sql(statement(**kw),ok=False),kw
    assert '0A000' in sql('BEGIN ISOLATION LEVEL REPEATABLE READ; '+statement(),ok=False)
    assert '55000' in sql("BEGIN; UPDATE bounded_lexical SET body='uncommitted' WHERE id='1'; "+statement(),ok=False)
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(),ok=False)
    sql('CREATE ROLE pgq_lexical_reader; GRANT SELECT ON bounded_lexical TO pgq_lexical_reader')
    assert '42501' in sql('SET ROLE pgq_lexical_reader; '+statement(),ok=False)
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(q='NOT durable',kind='syntax'),ok=False)
    assert '42501' in sql('SET ROLE pgq_lexical_reader; '+statement(q='NOT durable',kind='syntax'),ok=False)
    assert '55000' in sql("BEGIN; UPDATE bounded_lexical SET body='uncommitted' WHERE id='1'; "+statement(q='NOT durable',kind='syntax'),ok=False)
    assert '0A000' in sql('BEGIN ISOLATION LEVEL REPEATABLE READ; '+statement(q='NOT durable',kind='syntax'),ok=False)
    sql('GRANT builder TO pgq_lexical_reader')
    assert keys(json.loads(sql('SET ROLE pgq_lexical_reader; '+statement())))=={'1','2','3'}
    assert keys(json.loads(sql('SET ROLE pgq_lexical_reader; '+statement(q='NOT durable',kind='syntax'))))=={'1','3','4','ERR-0042'}
    sql('REVOKE builder FROM pgq_lexical_reader; REVOKE SELECT ON bounded_lexical FROM pgq_lexical_reader')
    sql('ALTER TABLE bounded_lexical ENABLE ROW LEVEL SECURITY')
    assert '0A000' in sql(statement(),ok=False)
    assert '0A000' in sql(statement(q='NOT durable',kind='syntax'),ok=False)
    sql('ALTER TABLE bounded_lexical DISABLE ROW LEVEL SECURITY')
    # Conservative DDL invalidation requires a fresh trusted registration.
    task=json.loads(sql("SELECT qdrant.drop_index('bounded_lexical')"))
    assert json.loads(sql("SELECT qdrant.await_task('"+task['task_id']+"',60000)"))['succeeded']
    sql("SELECT qdrant.create_index('bounded_lexical','bounded_lexical','id','{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"kind\":\"keyword\",\"field\":\"category\"}}}')")
    replay()
    checks.append('bounded lexical refuses identifiers, unsupported query shapes/options/snapshots, uncommitted source, non-owner/table-only roles and RLS; explicit inherited owner domain succeeds')
    ticket=ticket_from(sql("BEGIN; DELETE FROM bounded_lexical WHERE id='1'; INSERT INTO bounded_lexical VALUES('1','replacement only','Allow'); SELECT qdrant.track_changes('bounded_lexical'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
    assert keys(query())=={'2','3'} and keys(query(q='replacement'))=={'1'}
    assert keys(query(q='transaction',kind='syntax'))=={'2','3'} and keys(query(q='replacement',kind='syntax'))=={'1'}
    sql("UPDATE bounded_lexical SET body='transaction recovery' WHERE id='1'");replay()
    checks.append('ordinary DML, deletion/key reuse and exact durable tickets update query-local lexical facts without a second document write API')
    vocabulary=' '.join('abcdef'+chr(n)+' '+chr(n)+'abcdef' for n in range(ord('a'),ord('z')+1))
    sql("UPDATE bounded_lexical SET body="+literal(vocabulary)+" WHERE id='1'");ready('bounded_lexical')
    assert '54000' in sql(statement(q='abcdef'),ok=False)
    sql("UPDATE bounded_lexical SET body='transaction recovery' WHERE id='1'");replay()
    checks.append('actual high-cardinality fuzzy dictionary refuses more than32 complete upstream automaton expansions rather than truncating matches')
    sql('CREATE TABLE lexical_big(id bigint PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO lexical_big SELECT n,repeat('transaction ',5000) FROM generate_series(1,18)n")
    sql("SELECT qdrant.create_index('lexical_big','lexical_big','id','{\"text\":{\"fields\":[\"body\"]}}')");ready('lexical_big')
    assert '54000' in sql(statement(index='lexical_big'),ok=False)
    assert '54000' in sql(statement(q='NOT absent',kind='syntax',index='lexical_big'),ok=False)
    task=json.loads(sql("SELECT qdrant.drop_index('lexical_big')"))
    assert json.loads(sql("SELECT qdrant.await_task('"+task['task_id']+"',60000)"))['succeeded']
    checks.append('actual 1080000-byte durable source snapshot refuses before building the query-local lexical index')
    if spawn is not None:
        for kind,change in [('fuzzy','source'),('fuzzy','cancel'),('syntax','source'),('syntax','cancel')]:
            owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
            os.kill(pid,signal.SIGSTOP)
            try:
                application='pgq_lexical_'+kind+'_'+change
                waiting=spawn(statement(q='NOT durable' if kind=='syntax' else 'transactoin',kind=kind),application)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_statistics':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_statistics',busy
                if change=='source':sql("UPDATE bounded_lexical SET body='changed source' WHERE id='1'")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained.get('active') and retained['engine_instance']==owner['engine_instance'],retained
            finally:os.kill(pid,signal.SIGCONT)
            if change=='source':
                out,err=waiting.communicate(timeout=10)
                assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE bounded_lexical SET body='transaction recovery' WHERE id='1'")
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            replay()
        checks.append('in-flight fuzzy and syntax source mutation refuses the whole result; cancelled SQL retains native operation ownership until completion')
    return replay
