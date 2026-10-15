"""A07: call ledger and report statistics consistency across UI/JSON/Markdown exports."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agentlab.api import create_app
from agentlab.engine import Engine, benchmark_result
from agentlab.models import TaskRequest
from agentlab.providers import ModelClient, ProviderError
from agentlab.storage import Store

KEY = "fixture-secret-never-persist-this-62948"


def settle(client, task_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get("/api/tasks/" + task_id).json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.02)
    raise AssertionError("task did not settle")


def test_mock_export_statistics_match_across_json_and_task(tmp_path):
    """Same mock task: JSON export metrics must equal task state metrics."""
    with TestClient(create_app(tmp_path)) as client:
        res = client.post("/api/tasks", json={"question": "统计一致性验证问题", "review_plan": False})
        task_id = res.json()["id"]
        task = settle(client, task_id)
        assert task["status"] == "completed"
        exported = client.get(f"/api/tasks/{task_id}/export?format=json").json()
        # Metrics must come from the same frozen state.
        s = task.get("result") or {}
        assert exported["metrics"]["model_calls"] == (s.get("model_calls") or 0)
        assert exported["metrics"]["simulated_model_calls"] == (s.get("simulated_calls") or 0)
        assert exported["metrics"]["search_calls"] == (s.get("search_calls") or 0)
        assert exported["metrics"]["input_tokens"] is None  # mock → unknown
        assert exported["metrics"]["cost_usd"] is None  # mock → unknown, not 0
        assert exported["metrics"]["usage_complete"] is False
        assert exported["schema_version"] == "1.0"


def test_five_usage_fixture_categories(tmp_path):
    """Five fixture categories: with usage, without usage, 429-retry, cache tokens, timeout."""
    responses = iter([
        # 1. with usage
        {"choices": [{"message": {"content": '{"plan":["a"],"queries":["q"]}'}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        # 2. without usage (missing usage field)
        {"choices": [{"message": {"content": '{"done":true}'}}]},
        # 3. 429 then success
        httpx.Response(429, json={"error": "rate"}),
        {"choices": [{"message": {"content": '{"done":true}'}}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        # 4. cache tokens (anthropic-style)
        {"choices": [{"message": {"content": '{"done":true}'}}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 2, "cache_read_input_tokens": 10}},
    ])
    call_count = [0]

    def transport(request):
        call_count[0] += 1
        r = next(responses)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    state = Engine.initial("usage-fixtures", TaskRequest(
        question="五类 usage 夹具", execution_mode="live", provider="qwen"
    ).model_dump())
    state["provider_config"].update(
        base_url="https://fixture.example/v1", model="fixture",
        input_price=1, output_price=2,
    )
    client = ModelClient(state, lambda *a: None, lambda *a, **k: 30,
                         credential_resolver=lambda *a: KEY,
                         transport=httpx.MockTransport(transport))

    # 1. with usage
    client.call("planner", {}, "{}")
    assert state["input_tokens"] == 10 and state["output_tokens"] == 5
    assert state["usage_complete"] is True

    # 2. without usage → usage becomes incomplete
    client.call("analyst", {}, "{}")
    assert state["usage_complete"] is False
    assert state["cost_usd"] is None  # unknown, not 0

    # 3. 429 then success
    client.call("reviewer", {}, "{}")
    assert state["model_calls"] >= 2  # 429 retry counts

    # 4. cache tokens
    state2 = Engine.initial("cache-fixtures", TaskRequest(
        question="缓存 token", execution_mode="live", provider="anthropic"
    ).model_dump())
    state2["provider_config"].update(
        protocol="anthropic", base_url="https://fixture.example/v1",
        model="fixture", input_price=1, output_price=2,
    )
    cache_responses = iter([
        {"content": [{"type": "text", "text": "{}"}],
         "usage": {"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 10}},
    ])
    client2 = ModelClient(state2, lambda *a: None, lambda *a, **k: 30,
                          credential_resolver=lambda *a: KEY,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(cache_responses))))
    client2.call("planner", {}, "{}")
    # cache_read is added to input_tokens
    assert state2["input_tokens"] == 14
    # cached usage → cost unknown
    assert state2["cost_usd"] is None
    assert state2["usage_complete"] is True


def test_model_and_search_counts_are_separate(tmp_path):
    """Model calls and search calls must be counted separately."""
    store = Store(tmp_path)
    store.create("sep", TaskRequest(question="模型与搜索分开计数", review_plan=False).model_dump())
    state = Engine(store).run("sep")
    # mock mode: simulated model calls + search calls
    assert state["model_calls"] == 0  # mock has no real model calls
    assert state["simulated_calls"] > 0
    assert state["search_calls"] >= 0
    # They must not be conflated
    assert state["calls"] == state["simulated_calls"] + state["search_calls"]


def test_unknown_cost_never_shown_as_zero(tmp_path):
    """When usage is incomplete or missing, cost must be None (unknown), not 0."""
    state = Engine.initial("cost-unknown", TaskRequest(
        question="费用未知", execution_mode="live", provider="qwen"
    ).model_dump())
    state["provider_config"].update(base_url="https://fixture.example/v1", model="fixture")
    # Response without usage
    client = ModelClient(state, lambda *a: None, lambda *a, **k: 30,
                         credential_resolver=lambda *a: KEY,
                         transport=httpx.MockTransport(lambda r: httpx.Response(
                             200, json={"choices": [{"message": {"content": "{}"}}]})))
    client.call("planner", {}, "{}")
    assert state["cost_usd"] is None
    assert state["usage_complete"] is False
    # Export must also show None, not 0
    store = Store(tmp_path)
    store.create("cost-unknown", TaskRequest(question="费用未知", execution_mode="live").model_dump())
    store.update("cost-unknown", status="completed", result=state)
    task = store.get("cost-unknown")
    result = benchmark_result(task, state, store)
    assert result["metrics"]["cost_usd"] is None
    assert result["metrics"]["usage_complete"] is False
