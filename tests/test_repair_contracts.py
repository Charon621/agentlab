import sqlite3
import pytest
from fastapi import HTTPException
from agentlab.storage import Store
from agentlab.configuration import ModelRegistry
from agentlab.search_replay import freeze,replay
from agentlab.api import app
from pathlib import Path

def test_default_test_app_never_uses_project_data():
    assert app.state.store.data_dir.resolve()!=Path(__file__).resolve().parents[1]/'data'

def test_conflicted_claim_is_preserved(tmp_path):
    store=Store(tmp_path)
    with store.connect() as db:
        db.execute('CREATE TABLE env_claims(provider TEXT PRIMARY KEY,owner_id TEXT)')
        db.executemany('INSERT INTO env_claims VALUES(?,?)',[('qwen','a'),('kimi','b')])
    reg=ModelRegistry(store)
    with pytest.raises(HTTPException) as e:reg.claim_env('attacker')
    assert e.value.status_code==409
    assert set(reg.env_claim_conflicts())=={'a','b'}
    with pytest.raises(HTTPException):reg.resolve_env_conflict('attacker')
    reg.resolve_env_conflict('a');assert reg.env_owner()=='a'

def test_archive_preserves_events_ledger_and_checkpoint(tmp_path,monkeypatch):
    store=Store(tmp_path);store.create('task',{'question':'test','owner_id':'a'})
    store.event('task','test','ok','evidence');store.attempt('task','model')
    monkeypatch.setattr(store,'_delete_checkpoint',lambda _:pytest.fail('archive must retain checkpoint'))
    assert store.delete('task')['recoverable']
    assert store.list('a')==[] and len(store.list('a',archived=True))==1
    assert len(store.events('task'))==1 and store.attempt_counts('task')['model']==1
    assert store.restore('task') and len(store.list('a'))==1

def test_retrieval_hit_miss_and_hash():
    snap=freeze({'q':[{'title':'t','url':'https://example.org','excerpt':'e','retrieved':False}]})
    assert replay(snap,'q')[0]['cache_hit'] is True
    assert replay(snap,'miss')==[]
    snap['entries']['q'][0]['excerpt']='tampered'
    with pytest.raises(ValueError):replay(snap,'q')

def test_running_grant_cannot_resolve_after_current_config_drift(tmp_path):
    from agentlab.providers import ProviderError
    reg=ModelRegistry(Store(tmp_path))
    values={'name':'fixture','provider':'qwen','model':'fixture','base_url':'https://example.org/v1','temperature':0,'max_tokens':100,'api_key':'fixture-only-key'}
    config=reg.save('owner',dict(values));grant=reg.grant('owner',config['id'])
    snapshot={**reg.freeze('owner',config['id']),'integration_grant_id':grant['id']}
    assert reg.resolve('owner',snapshot)=='fixture-only-key'
    reg.save('owner',{**values,'model':'edited','api_key':None},config['id'])
    with pytest.raises(ProviderError) as e:reg.resolve('owner',snapshot)
    assert e.value.category=='grant_drift'

def test_expired_grant_blocks_both_new_and_existing_task_resolution(tmp_path):
    from agentlab.providers import ProviderError
    reg=ModelRegistry(Store(tmp_path))
    config=reg.save('owner',{'name':'fixture','provider':'qwen','model':'fixture','base_url':'https://example.org/v1','api_key':'fixture-only-key'})
    grant=reg.grant('owner',config['id']);next(iter(reg.grants.values()))['expires']=0
    with pytest.raises(HTTPException) as e:reg.authorize('Bearer '+grant['token'],config['id'])
    assert e.value.status_code==401
    with pytest.raises(ProviderError) as e:reg.resolve('owner',{**config,'integration_grant_id':grant['id']})
    assert e.value.category=='grant_expired'

def test_human_decision_survives_graph_reopen_and_archive(tmp_path):
    from agentlab.models import TaskRequest
    from agentlab.engine import Engine
    store=Store(tmp_path);store.create('human-history',TaskRequest(question='人工计划历史应可恢复').model_dump())
    Engine(store).run('human-history');Engine(store).run('human-history',resume={'plan':['人工确认计划'],'direction':'明确方向'})
    store.delete('human-history');store.restore('human-history')
    state=Engine(store).snapshot('human-history');decision=state['human_decisions'][-1]
    assert decision['plan']==['人工确认计划'] and decision['direction']=='明确方向'
