"""Real HTTP adapters with injected local transports; never call paid providers."""
import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from agentlab.api import create_app
from agentlab.configuration import PRESETS
from agentlab.engine import Engine
from agentlab.models import TaskRequest
from agentlab.network import PublicNetworkBackend, public_addresses, safe_address, validate_model_url
from agentlab.providers import ModelClient, ProviderError, model_request, model_response
from agentlab.storage import Store
import ipaddress

KEY = "fixture-secret-never-persist-this-62948"


def config(provider="qwen", **updates):
    base, model, protocol = PRESETS[provider]
    return {"name": "Test " + provider, "provider": provider, "protocol": protocol,
            "model": model or "fixture-model", "base_url": base or "https://fixture.example/v1",
            "api_key": KEY, "temperature": 0.3, "max_tokens": 99, **updates}


def fixture(request):
    body = json.loads(request.content)
    if request.url.path.endswith("/messages"):
        assert request.headers["x-api-key"] == KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "system" in body and all(m["role"] != "system" for m in body["messages"])
        return httpx.Response(200, json={"model": body["model"], "content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 3, "output_tokens": 1}})
    assert request.headers["authorization"] == "Bearer " + KEY
    return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


def settle(client, task_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = client.get("/api/tasks/" + task_id).json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(.01)
    raise AssertionError("task did not settle")


@pytest.mark.parametrize("provider", list(PRESETS))
def test_every_provider_real_request_crud_default_test_without_leaks(tmp_path, provider):
    transport = httpx.MockTransport(fixture)
    with TestClient(create_app(tmp_path, model_transport=transport)) as client:
        created = client.post("/api/models", json=config(provider))
        assert created.status_code == 201, created.text
        model = created.json()
        assert model["configured"] and "api_key" not in model and KEY not in created.text
        assert "HttpOnly" in created.headers["set-cookie"] and "SameSite=strict" in created.headers["set-cookie"]
        assert client.get("/api/models").json()["default_model_config_id"] == model["id"]
        result = client.post(f"/api/models/{model['id']}/test").json()
        assert result["ok"] and result["category"] == "connected"
        assert client.get("/api/models").json()["models"][0]["last_test"]["config_version"] == 1
        edited = client.put(f"/api/models/{model['id']}", json=config(provider, api_key=None, model="version-two")).json()
        assert edited["config_version"] == 2 and edited["credential_id"] == model["credential_id"]
        assert "last_test" not in edited
        assert client.delete(f"/api/models/{model['id']}/credential").json() == {"configured": False}
        assert client.post(f"/api/models/{model['id']}/test").json()["category"] == "credential_missing"
        assert client.delete(f"/api/models/{model['id']}").status_code == 200
        assert client.get("/api/models").json()["models"] == []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("code,category", [(401, "authentication"), (403, "permission"), (404, "model_not_found"), (429, "rate_limit"), (503, "http_error"), (302, "redirect_blocked")])
def test_connection_errors_never_echo_secret(tmp_path, code, category):
    transport = httpx.MockTransport(lambda request: httpx.Response(code, json={"error": KEY}, headers={"Location": "https://127.0.0.1/" + KEY}))
    with TestClient(create_app(tmp_path, model_transport=transport)) as client:
        model = client.post("/api/models", json=config()).json()
        response = client.post(f"/api/models/{model['id']}/test")
        assert response.json()["category"] == category and KEY not in response.text


@pytest.mark.parametrize("exception,category", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network"), (httpx.RemoteProtocolError, "network")])
def test_connection_network_categories_do_not_echo_exception(tmp_path, exception, category):
    def failure(request):
        raise exception(KEY)
    with TestClient(create_app(tmp_path, model_transport=httpx.MockTransport(failure))) as client:
        model = client.post("/api/models", json=config()).json()
        response = client.post(f"/api/models/{model['id']}/test")
        assert response.json()["category"] == category and KEY not in response.text


def test_session_task_model_grant_and_idempotency_isolation(tmp_path):
    app = create_app(tmp_path, model_transport=httpx.MockTransport(fixture))
    with TestClient(app) as one, TestClient(app) as two:
        model = one.post("/api/models", json=config()).json()
        assert two.get("/api/models").json()["models"] == []
        assert two.post(f"/api/models/{model['id']}/test").status_code == 404
        grant = one.post("/api/integration/grants", json={"model_config_id": model["id"]}).json()
        headers = {"Authorization": "Bearer " + grant["token"]}
        assert two.get("/api/integration/model", params={"model_config_id": model["id"]}, headers=headers).status_code == 200
        assert two.get("/api/integration/model", params={"model_config_id": "other"}, headers=headers).status_code == 401
        payload = {"task_id": "same-external-id", "question": "研究会话安全隔离"}
        result = one.post("/api/benchmark/run", json=payload).json()
        other = two.post("/api/benchmark/run", json=payload).json()
        assert result["run_id"] != other["run_id"]
        task_id = result["run_id"]
        for path in ["", "/events", "/export", "/export?format=json"]:
            assert two.get("/api/tasks/" + task_id + path).status_code == 404
        for path in ["resume", "stop"]:
            assert two.post("/api/tasks/" + task_id + "/" + path, json={}).status_code == 404
        assert two.post("/api/tasks", json={"question": "读取其他会话父任务", "parent_id": task_id}).status_code == 404
        assert grant["token"] not in one.get("/api/integration/grants").text
        assert two.delete("/api/integration/grants/" + grant["id"]).status_code == 404
        assert one.delete("/api/integration/grants/" + grant["id"]).status_code == 200
        assert two.get("/api/integration/model", params={"model_config_id": model["id"]}, headers=headers).status_code == 401


def test_frozen_snapshot_key_deletion_pause_and_original_credential_restore(tmp_path):
    requests = []
    def planner(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": json.dumps({"plan": ["研究原始配置"], "queries": ["Research"]})}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    app = create_app(tmp_path, model_transport=httpx.MockTransport(planner))
    with TestClient(app) as client:
        original = client.post("/api/models", json=config(model="original-model")).json()
        task_id = client.post("/api/tasks", json={"question": "必须使用原配置恢复", "execution_mode": "live", "model_config_id": original["id"]}).json()["id"]
        task = settle(client, task_id)
        assert task["status"] == "paused" and task["result"]["provider_config"]["config_version"] == 1
        current = client.put("/api/models/" + original["id"], json=config(model="new-model", api_key="changed-key-value")).json()
        assert current["credential_id"] != original["credential_id"]
        assert app.state.models.resolve(task["request"]["owner_id"], task["result"]["provider_config"]) == KEY
        client.delete(f"/api/models/{original['id']}/credential")
        client.post("/api/tasks/" + task_id + "/resume", json={"direction": ""})
        missing = settle(client, task_id)
        assert missing["status"] == "paused" and missing["result"]["interrupts"][0]["category"] == "credential_missing"
        with pytest.raises(ProviderError, match="credential_missing"):
            app.state.models.resolve(task["request"]["owner_id"], task["result"]["provider_config"])
        restored = client.post(f"/api/models/{original['id']}/credentials", json={"credential_id": original["credential_id"], "api_key": KEY})
        assert restored.status_code == 200
        assert app.state.models.resolve(task["request"]["owner_id"], task["result"]["provider_config"]) == KEY
        assert requests[0]["model"] == "original-model" and requests[0]["max_tokens"] == 99
        assert KEY not in client.get("/api/tasks/" + task_id + "/export?format=json").text


def test_cached_usage_unknown_cost_is_sticky_across_uncached_calls(tmp_path):
    responses = iter([
        {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 10}},
        {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 3, "output_tokens": 1}},
    ])
    state = Engine.initial("cost", TaskRequest(question="费用未知不可被后次请求覆盖", execution_mode="live", provider="anthropic").model_dump())
    state["provider_config"].update(input_price=1, output_price=2, model="fixture")
    client = ModelClient(state, lambda *args: None, lambda *args, **kwargs: 10,
                         credential_resolver=lambda *args: KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses))))
    client.call("planner", {}, "{}")
    assert state["cost_usd"] is None and state["usage_complete"] and not state["cost_complete"]
    client.call("planner", {}, "{}")
    assert state["input_tokens"] == 17 and state["output_tokens"] == 3 and state["cost_usd"] is None


def test_env_claim_belongs_to_one_persistent_local_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", KEY)
    monkeypatch.setenv("KIMI_API_KEY", KEY)
    app = create_app(tmp_path, model_transport=httpx.MockTransport(fixture))
    with TestClient(app) as one, TestClient(app) as two:
        model = one.post("/api/models/import-env/qwen").json()
        assert model["configured"]
        assert two.post("/api/models/import-env/qwen").status_code == 403
        assert two.post("/api/models/import-env/kimi").status_code == 403
        assert two.get("/api/models").json()["models"] == []


def test_grant_and_expected_version_reject_model_drift(tmp_path):
    app = create_app(tmp_path, model_transport=httpx.MockTransport(fixture))
    with TestClient(app) as client:
        model = client.post("/api/models", json=config()).json()
        grant = client.post("/api/integration/grants", json={"model_config_id": model["id"]}).json()
        client.put("/api/models/" + model["id"], json=config(model="new-version", api_key=None))
        payload = {"task_id": "drift", "question": "模型版本变化不可静默执行", "execution_mode": "live", "model_config_id": model["id"], "expected_config_version": 1}
        assert client.post("/api/benchmark/run", json=payload).status_code == 409
        assert client.post("/api/benchmark/run", json=payload, headers={"Authorization": "Bearer " + grant["token"]}).status_code == 409
        assert client.get("/api/tasks").json() == []


def test_restart_keeps_identity_definitions_and_tasks_but_erases_keys_grants(tmp_path):
    app = create_app(tmp_path, model_transport=httpx.MockTransport(fixture))
    with TestClient(app) as client:
        model = client.post("/api/models", json=config()).json()
        grant = client.post("/api/integration/grants", json={"model_config_id": model["id"]}).json()
        cookie = client.cookies.get("agentlab_session")
        task_id = client.post("/api/benchmark/run", json={"task_id": "restart", "question": "重启后保留会话历史"}).json()["run_id"]
    with TestClient(create_app(tmp_path, model_transport=httpx.MockTransport(fixture))) as restored:
        restored.cookies.set("agentlab_session", cookie)
        assert restored.get("/api/tasks/" + task_id).status_code == 200
        assert not restored.get("/api/models").json()["models"][0]["configured"]
        assert restored.get("/api/integration/grants").json()["grants"] == []
        assert restored.get("/api/integration/model", params={"model_config_id": model["id"]}, headers={"Authorization": "Bearer " + grant["token"]}).status_code == 401
        update = restored.put("/api/models/" + model["id"], json=config()).json()
        assert update["credential_id"] == model["credential_id"] and update["configured"]


def test_invalid_body_does_not_echo_key_and_private_fixture_is_explicit(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/models", json=config(max_tokens=0))
        assert response.status_code == 422 and KEY not in response.text
        assert client.post("/api/models", json=config(base_url="http://localhost:9999/v1")).status_code == 422
        assert client.post("/api/models", json=config(base_url="https://127.0.0.1/v1")).status_code == 422
    with pytest.raises(ValueError):
        create_app(tmp_path, allow_private_fixture=True)
    with TestClient(create_app(tmp_path, model_transport=httpx.MockTransport(fixture), allow_private_fixture=True)) as client:
        created = client.post("/api/models", json=config(base_url="http://localhost:9999/v1"))
        assert created.status_code == 201
        assert client.post("/api/models/" + created.json()["id"] + "/test").json()["ok"]


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fec0::1", "fc00::1", "224.0.0.1"])
def test_ssrf_rejects_private_reserved_site_local_and_multicast(ip):
    assert not safe_address(ipaddress.ip_address(ip))


def test_dns_checks_every_answer_and_pins_literal_ip(monkeypatch):
    answers = lambda ip: (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
    import socket
    monkeypatch.setattr("agentlab.network.socket.getaddrinfo", lambda *a, **k: [answers("8.8.8.8"), answers("127.0.0.1")])
    with pytest.raises(ProviderError, match="非公网"):
        public_addresses("fixture.example", 443)
    monkeypatch.setattr("agentlab.network.socket.getaddrinfo", lambda *a, **k: [answers("8.8.8.8")])
    connected = []
    monkeypatch.setattr("httpcore.SyncBackend.connect_tcp", lambda self, host, *a, **k: connected.append(host))
    PublicNetworkBackend().connect_tcp("fixture.example", 443)
    assert connected == ["8.8.8.8"]


def test_anthropic_native_tools_and_cached_usage_are_adapted():
    cfg = {"provider": "anthropic", "protocol": "anthropic", "model": "fixture", "supports_tools": True}
    response = {"content": [{"type": "tool_use", "id": "call-1", "name": "search_web", "input": {"query": "research"}}],
                "usage": {"input_tokens": 4, "output_tokens": 2, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 3}}
    message, in_tokens, out_tokens = model_response(cfg, response)
    assert (in_tokens, out_tokens) == (17, 2)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "question"},
                {"role": "assistant", **message}, {"role": "tool", "tool_call_id": "call-1", "content": "real evidence"}]
    path, body = model_request(cfg, messages, "auto")
    assert path == "/messages" and body["system"] == "system"
    assert body["messages"][-2]["content"][0]["type"] == "tool_use"
    assert body["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "call-1", "content": "real evidence"}


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_native_two_tools_roundtrip_and_actual_evidence_return(monkeypatch, protocol):
    provider = "openai" if protocol == "openai" else "anthropic"
    state = Engine.initial("native", TaskRequest(question="native tools fixture", execution_mode="live", provider=provider).model_dump())
    state["provider_config"].update(tool_mode="native", supports_tools=True, base_url="https://fixture.example/v1", supports_temperature=False)
    traffic, searched = [], []
    calls = [{"id": "call-" + str(i), "type": "function", "function": {"name": "search_web", "arguments": json.dumps({"query": query})}} for i, query in enumerate(["first", "second"])]
    def transport(request):
        body = json.loads(request.content)
        traffic.append(body)
        if len(traffic) == 1:
            if protocol == "anthropic":
                assert body["system"] and request.headers["x-api-key"] == KEY
                return httpx.Response(200, json={"content": [{"type": "text", "text": "Search"}, *[{"type": "tool_use", "id": c["id"], "name": "search_web", "input": json.loads(c["function"]["arguments"])} for c in calls]], "usage": {"input_tokens": 3, "output_tokens": 2}})
            assert body["max_completion_tokens"] == 2600 and "temperature" not in body
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": calls}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        if protocol == "anthropic":
            results = body["messages"][-1]["content"]
            assert body["messages"][-1]["role"] == "user"
            assert [b["tool_use_id"] for b in results] == ["call-0", "call-1"]
            assert all(json.loads(b["content"])["untrusted_external_data"] for b in results)
            return httpx.Response(200, json={"content": [{"type": "text", "text": '{"done":true}'}], "usage": {"input_tokens": 8, "output_tokens": 1}})
        results = [m for m in body["messages"] if m["role"] == "tool"]
        assert [r["tool_call_id"] for r in results] == ["call-0", "call-1"]
        assert all(json.loads(r["content"])["untrusted_external_data"] for r in results)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"done":true}'}}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}})
    def search(client, query):
        searched.append(query)
        client.before_attempt()
        return [{"title": query, "url": "https://fixture.example/" + query, "excerpt": "actual fixture evidence", "retrieved": True}]
    monkeypatch.setattr("agentlab.providers.SearchClient.search", search)
    client = ModelClient(state, lambda *args: None, lambda *args, **kwargs: 100, credential_resolver=lambda *args: KEY, transport=httpx.MockTransport(transport))
    assert client.call("searcher", {}, "{}") == {"done": True}
    assert searched == ["first", "second"] and len(state["sources"]) == 2 and state["search_calls"] == 2


@pytest.mark.parametrize("arguments", ["[]", '{"query":"valid","extra":"invalid"}', "{invalid", '{"query":3}'])
def test_all_native_arguments_validate_before_any_tool_runs(monkeypatch, arguments):
    state = Engine.initial("native-invalid", TaskRequest(question="invalid second tool must not run first", execution_mode="live").model_dump())
    state["provider_config"].update(tool_mode="native", supports_tools=True)
    calls = [{"id": "valid", "function": {"name": "search_web", "arguments": '{"query":"first"}'}},
             {"id": "invalid", "function": {"name": "search_web", "arguments": arguments}}]
    searches = []
    monkeypatch.setattr("agentlab.providers.SearchClient.search", lambda *args: searches.append(args))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": calls}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    client = ModelClient(state, lambda *args: None, lambda *args, **kwargs: 100, credential_resolver=lambda *args: KEY, transport=transport)
    with pytest.raises(ProviderError, match="无效参数"):
        client.call("searcher", {}, "{}")
    assert searches == []


def test_explicit_legacy_history_claim_preserves_old_tasks(tmp_path):
    store = Store(tmp_path)
    request = TaskRequest(question="旧单用户历史不能丢失", review_plan=False).model_dump()
    store.create("old-task", request)
    Engine(store).run("old-task")
    with TestClient(create_app(tmp_path)) as one, TestClient(create_app(tmp_path)) as two:
        assert one.get("/api/tasks").json() == [] and one.get("/api/history/unclaimed").json()["count"] == 1
        assert one.post("/api/history/claim").json()["claimed"] == 1
        assert one.get("/api/tasks/old-task").json()["status"] == "completed"
        assert two.get("/api/tasks/old-task").status_code == 404
        assert two.post("/api/history/claim").json()["claimed"] == 0
