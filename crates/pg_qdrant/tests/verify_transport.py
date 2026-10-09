"""A live helper transport failure cannot dispatch through a cleared channel."""
import json
import os
from pathlib import Path
import signal


def run(sql, ready, ticket_from, checks, crash_matrix=None):
    prior_epochs={}
    for name in ['transport_a', 'transport_b']:
        sql('CREATE TABLE '+name+'(id bigint PRIMARY KEY,body text NOT NULL)')
        sql("INSERT INTO "+name+" VALUES(1,'transport initial')")
        sql("SELECT qdrant.create_index('"+name+"','"+name+"','id','{\"text\":{\"fields\":[\"body\"]}}')")
        state=ready(name)
        index_id=sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='"+name+"'")
        prior_epochs[name]=(index_id,state)
    before=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    pid=before['engine_pid']
    data=Path(os.environ['PG_QDRANT_DISPOSABLE_DATA'])
    owner=next(data.rglob('db-*.engine-owner'))
    marker=owner.with_suffix('.fault')
    root=owner.with_suffix('.indexes')
    os.kill(pid,signal.SIGSTOP)
    try:
        marker.write_text('transport_error')
        tickets=[]
        for name in ['transport_a', 'transport_b']:
            output=sql("BEGIN; UPDATE "+name+" SET body='transport current'; SELECT qdrant.track_changes('"+name+"'); COMMIT")
            tickets.append(ticket_from(output))
        pending=[json.loads(sql("SELECT qdrant.await_changes('"+t+"',0)")) for t in tickets]
        assert all(p['committed'] and not p['durable'] and p['pending_events']>0 for p in pending),pending
    finally:
        os.kill(pid,signal.SIGCONT)
    results=[json.loads(sql("SELECT qdrant.await_changes('"+t+"',60000)")) for t in tickets]
    after=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    assert after['worker_pid']==before['worker_pid'],(before,after,results)
    assert after['engine_pid']!=pid and after['engine_instance']!=before['engine_instance'],(before,after)
    assert not marker.exists(),'transport marker was not consumed'
    assert all(r['durable'] and r['pending_events']==0 for r in results),results
    observed={'cut':'live_transport_error_pending_indexes','before':before,'after':after,
        'ticket_pending':pending,'ticket_durable':results,'final_source_keys':['1']}
    cleanup=[]
    for name in ['transport_a','transport_b']:
        current=ready(name)
        assert sql("SELECT source_key FROM qdrant.search('"+name+"','current')")=='1'
        assert sql("SELECT count(*) FROM qdrant.search('"+name+"','initial')")=='0'
        task=json.loads(sql("SELECT qdrant.drop_index('"+name+"')"))
        result=json.loads(sql("SELECT qdrant.await_task('"+task['task_id']+"',30000)"))
        index_id,prior=prior_epochs[name]
        prior_path=root/(index_id+'-'+prior['generation']+'-'+prior['storage_epoch'])
        current_path=root/(index_id+'-'+current['generation']+'-'+current['storage_epoch'])
        assert result['completed'] and not result['succeeded'] and not result['physical_cleanup_completed'],result
        assert result['state']=='failed' and result['error_code']=='source_owner_changed' and 'owner_changed' in result['error'],result
        assert result['pending_epochs']==1 and prior_path.is_dir() and not current_path.exists(),result
        cleanup.append({'index_name':name,'task':result,'prior_directory_preserved':prior_path.is_dir(),
                        'current_directory_cleaned':not current_path.exists()})
    observed['cleanup']=cleanup
    if crash_matrix is not None:
        crash_matrix.append(observed)
    checks.append('live native transport failure with two pending source indexes preserves the PostgreSQL supervisor, rotates native owner and replays exact durable tickets before current-only search')
    return observed
