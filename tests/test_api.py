import time
from fastapi.testclient import TestClient
from agentlab.api import create_app


def settled(client, task_id):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        task = client.get('/api/tasks/' + task_id).json()
        if task['status'] not in {'queued', 'running'}:
            return task
        time.sleep(.02)
    raise AssertionError("task did not settle")


def test_full_human_flow_history_export_sse(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/').status_code == 200
        res = client.post('/api/tasks', json={'question': '研究任务的检查点有什么作用？'})
        assert res.status_code == 201
        task_id = res.json()['id']
        paused = settled(client, task_id)
        assert paused['status'] == 'paused'
        assert paused['result']['interrupts'][0]['kind'] == 'plan'
        assert client.post(f'/api/tasks/{task_id}/resume',json={'direction':'只谈 SQLite','plan':['检索SQLite','审查结论']}).status_code == 200
        done = settled(client, task_id)
        assert done['status'] == 'completed'
        assert done['result']['direction'] == '只谈 SQLite'
        assert len(client.get('/api/tasks').json()) == 1
        exported = client.get(f'/api/tasks/{task_id}/export?format=json').json()
        assert exported['execution_mode'] == 'mock' and exported['metrics']['model_calls'] == 0
        assert client.get(f'/api/tasks/{task_id}/export').status_code == 200
        stream = client.get(f'/api/tasks/{task_id}/events')
        assert 'event: progress' in stream.text and 'event: state' in stream.text
        follow = client.post('/api/tasks',json={'question':'进一步说明恢复条件','parent_id':task_id,'review_plan':False})
        assert follow.status_code == 201
        assert settled(client, follow.json()['id'])['status'] == 'completed'


def test_sync_benchmark_contract_and_validation(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        for mode in ['single','multi']:
            response = client.post('/api/benchmark/run',json={'task_id':'example-1','question':'怎样判断证据可靠性？','mode':mode,
                                        'execution_mode':'mock','research_instructions':'要求注明局限','temperature':0})
            assert response.status_code == 200, response.text
            result=response.json()
            assert result['schema_version']=='1.0' and result['task_id']=='example-1' and result['metrics']['success']
            assert result['metrics']['input_tokens'] is None and result['metrics']['cost_usd'] is None
        assert client.post('/api/tasks',json={'question':'ab'}).status_code == 422
        assert client.post('/api/tasks',json={'question':'有效研究问题','max_calls':0}).status_code == 422
        assert client.get('/api/tasks/no-such-task').status_code == 404
        assert client.post('/api/tasks',json={'question':'有效研究问题'},headers={'Origin':'https://untrusted.example'}).status_code == 403
        assert client.get('/api/tasks',headers={'Host':'attacker.example','Origin':'http://attacker.example'}).status_code == 400


def test_benchmark_identical_retry_is_idempotent_but_modes_differ(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        payload={'task_id':'retry-id','question':'同样请求不可重复计费','mode':'multi'}
        first=client.post('/api/benchmark/run',json=payload).json()
        again=client.post('/api/benchmark/run',json=payload).json()
        assert first['run_id']==again['run_id'] and len(client.get('/api/tasks').json())==1
        single=client.post('/api/benchmark/run',json={**payload,'mode':'single'}).json()
        assert single['run_id']!=first['run_id'] and len(client.get('/api/tasks').json())==2


def test_live_without_key_pauses_with_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv('QWEN_API_KEY',raising=False)
    with TestClient(create_app(tmp_path)) as client:
        response=client.post('/api/tasks',json={'question':'研究错误恢复流程','execution_mode':'live'})
        task=settled(client,response.json()['id'])
        assert task['status']=='paused'
        assert task['result']['interrupts'][0]['kind']=='error'
        # A03: no credential identity → legacy_binding_required (actionable, no env fallback).
        assert task['result']['interrupts'][0].get('category') in ('legacy_binding_required', 'credential_missing')
