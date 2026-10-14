import httpx
import json
import pytest
from agentlab.engine import Engine
from agentlab.models import TaskRequest
from agentlab.providers import ModelClient, SearchClient, BudgetExceeded
from agentlab.storage import Store


def configured_state(tmp_path, monkeypatch, max_calls=8):
    monkeypatch.setenv('QWEN_API_KEY','local-test-placeholder')
    monkeypatch.setenv('QWEN_INPUT_PRICE_PER_MILLION','1')
    monkeypatch.setenv('QWEN_OUTPUT_PRICE_PER_MILLION','2')
    store=Store(tmp_path)
    req=TaskRequest(question='仅测试协议，不调用真实模型',execution_mode='live',review_plan=False,max_calls=max_calls).model_dump()
    store.create('provider',req)
    state=Engine.initial('provider',req)
    engine=Engine(store)
    engine.running_id='provider'
    return store,state,engine


def fake_client(monkeypatch, handler):
    real=httpx.Client
    def factory(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(handler)
        return real(*args,**kwargs)
    monkeypatch.setattr('agentlab.providers.httpx.Client',factory)


@pytest.mark.parametrize('usage,complete,cost',[
    ({'prompt_tokens':100,'completion_tokens':50},True,.0002),
    ({'total_tokens':150},False,None),
    ({},False,None),
    ({'prompt_tokens':100},False,None),
])
def test_usage_cost_never_invents_unknown_tokens(tmp_path,monkeypatch,usage,complete,cost):
    _,state,engine=configured_state(tmp_path,monkeypatch)
    def handler(req):
        return httpx.Response(200,json={'model':'test-model','system_fingerprint':'local-fixture',
           'choices':[{'message':{'content':'{"plan":["a"],"queries":["b"]}'}}],'usage':usage})
    fake_client(monkeypatch,handler)
    out=ModelClient(state,engine.event,engine.guard).call('planner',{},'{}')
    assert out['plan']==['a'] and state['usage_complete'] is complete
    if cost is None:assert state['cost_usd'] is None
    else:assert state['cost_usd']==pytest.approx(cost)
    assert state['response_models'][0]['system_fingerprint']=='local-fixture'


def test_model_retry_is_charged_and_stops_at_budget(tmp_path,monkeypatch):
    store,state,engine=configured_state(tmp_path,monkeypatch,max_calls=1)
    calls=[]
    def handler(req):calls.append(req);return httpx.Response(503,json={'error':'temporary'})
    fake_client(monkeypatch,handler)
    with pytest.raises(BudgetExceeded):
        ModelClient(state,engine.event,engine.guard).call('planner',{},'{}')
    assert len(calls)==1 and state['model_calls']==1 and store.attempt_counts('provider')['model']==1


def test_missing_usage_after_retry_keeps_total_cost_unknown(tmp_path,monkeypatch):
    _,state,engine=configured_state(tmp_path,monkeypatch)
    responses=iter([httpx.Response(503,json={}),httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':3}})])
    fake_client(monkeypatch,lambda req:next(responses))
    ModelClient(state,engine.event,engine.guard).call('planner',{},'{}')
    assert state['model_calls']==2 and state['usage_complete'] is False and state['cost_usd'] is None


def test_search_retry_is_charged_and_respects_frozen_engine(tmp_path,monkeypatch):
    store,state,engine=configured_state(tmp_path,monkeypatch,max_calls=1)
    search=SearchClient('live',engine.event)
    search.engine='wikipedia'
    def charge():
        remaining=engine.guard(state,tool=True)
        state['calls']+=1;state['search_calls']+=1
        return remaining
    search.before_attempt=charge
    monkeypatch.setenv('AGENTLAB_SEARCH','tavily')
    calls=[]
    def handler(req):calls.append(str(req.url));return httpx.Response(503)
    fake_client(monkeypatch,handler)
    with pytest.raises(BudgetExceeded):search.search('LangGraph')
    assert len(calls)==1 and 'wikipedia.org' in calls[0]


def test_uncheckpointed_attempt_survives_restart_budget(tmp_path,monkeypatch):
    store,state,engine=configured_state(tmp_path,monkeypatch,max_calls=1)
    store.attempt('provider','model')  # durable request reservation from a killed process
    with pytest.raises(BudgetExceeded):engine.guard(state,model=True)
    assert state['calls']==1 and state['model_calls']==1 and state['usage_complete'] is False


@pytest.mark.parametrize('mode',['single','multi'])
def test_native_tool_calls_execute_search_and_return_evidence_to_model(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('AGENTLAB_TOOL_MODE','native')
    store,state,engine=configured_state(tmp_path,monkeypatch,max_calls=12)
    req=TaskRequest(question='native search protocol research',execution_mode='live',review_plan=False,mode=mode).model_dump()
    store=Store(tmp_path/'native');store.create('native',req)
    traffic=[]
    def handler(request):
        if 'wikipedia.org' in request.url.host:
            traffic.append('search-http')
            return httpx.Response(200,json={'query':{'search':[{'title':'Research','snippet':'Retrieved evidence from a local HTTP transport fixture.'}]}})
        body=json.loads(request.content)
        system=body['messages'][0]['content']
        usage={'prompt_tokens':10,'completion_tokens':5}
        if '当前角色：planner' in system:
            message={'content':json.dumps({'plan':['search','analyze'],'queries':['research']})}
        elif '当前角色：analyst' in system:
            message={'content':json.dumps({'report':'# Analysis\nEvidence comes from the retrieved tool response [1].'})}
        elif '当前角色：reviewer' in system:
            message={'content':json.dumps({'decision':'finish','reasons':['mapped'],'queries':[]})}
        elif not any(m['role']=='tool' for m in body['messages']):
            assert body['tools'][0]['function']['name']=='search_web'
            traffic.append('native-tool-request')
            message={'content':None,'tool_calls':[{'id':'tool-call-1','type':'function','function':{'name':'search_web','arguments':'{"query":"research"}'}}]}
        else:
            tool=[m for m in body['messages'] if m['role']=='tool'][-1]
            evidence=json.loads(tool['content'])
            assert tool['tool_call_id']=='tool-call-1' and evidence['sources'][0]['id']=='1'
            assert evidence['untrusted_external_data'] is True
            traffic.append('evidence-returned-to-model')
            message={'content':json.dumps({'done':True} if '当前角色：searcher' in system else {'report':'# Single analysis\nThe real tool adapter returned this fixture evidence [1].','queries':[]})}
        return httpx.Response(200,json={'choices':[{'message':message}],'usage':usage,'model':'local-protocol-fixture'})
    fake_client(monkeypatch,handler)
    result=Engine(store).run('native')
    assert store.get('native')['status']=='completed'
    assert traffic==['native-tool-request','search-http','evidence-returned-to-model']
    assert result['search_calls']==1 and result['citation_check']['all_mapped']
    assert result['model_calls']==(2 if mode=='single' else 5)
    assert any(e['status']=='tool_result' for e in store.events('native'))


def test_native_rejects_unknown_tool_and_preserves_budget(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTLAB_TOOL_MODE','native')
    _,state,engine=configured_state(tmp_path,monkeypatch)
    def handler(req):
        return httpx.Response(200,json={'choices':[{'message':{'content':None,'tool_calls':[{'id':'x','function':{'name':'run_shell','arguments':'{"query":"x"}'}}]}}],
                                       'usage':{'prompt_tokens':10,'completion_tokens':2}})
    fake_client(monkeypatch,handler)
    from agentlab.providers import ProviderError
    with pytest.raises(ProviderError,match='不受支持的工具'):
        ModelClient(state,engine.event,engine.guard).call('single',{},'{}')
    assert state['model_calls']==1 and state['search_calls']==0
