"""Regression: test isolation blocks dotenv, real keys, and external network."""
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agentlab.api import create_app

ROOT = Path(__file__).resolve().parent.parent
KEY = "fixture-secret-never-persist-this-62948"


def test_no_env_keys_present_at_test_start():
    """After isolation, no real provider keys remain in the process env."""
    for name in (
        "QWEN_API_KEY", "KIMI_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY",
        "XIAOMI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY", "OPENROUTER_API_KEY",
    ):
        assert os.environ.get(name) is None, f"{name} leaked into test process"


def test_dotenv_disabled():
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"


def test_parent_fake_keys_do_not_leak_into_child(tmp_path):
    """Parent sets multiple fake keys; child subprocess must not see them or load .env."""
    env = {
        **os.environ,
        "QWEN_API_KEY": "parent-fake-qwen-key-should-not-leak",
        "KIMI_API_KEY": "parent-fake-kimi-key-should-not-leak",
        "OPENAI_API_KEY": "parent-fake-openai-key-should-not-leak",
        "TAVILY_API_KEY": "parent-fake-tavily-key-should-not-leak",
    }
    script = (
        "import os, sys; sys.path.insert(0, r'%s');\n"
        "from tests import isolation;\n"
        "keys = [k for k in os.environ if 'API_KEY' in k.upper() and os.environ.get(k)];\n"
        "print('LEAKED=' + repr(keys) if keys else 'CLEAN')\n"
        "print('DOTENV=' + os.environ.get('PYTHON_DOTENV_DISABLED', 'MISSING'))\n"
    ) % str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout, f"child leaked keys: {result.stdout}"
    assert "DOTENV=1" in result.stdout


def test_uninjected_network_request_fails_immediately():
    """Without a mock transport, any external model call is blocked by socket isolation."""
    from agentlab.providers import ModelClient, ProviderError
    from agentlab.engine import Engine
    from agentlab.models import TaskRequest

    state = Engine.initial("iso-test", TaskRequest(question="isolation", execution_mode="live").model_dump())
    state["provider_config"].update(base_url="https://api.example.com/v1", model="test")
    client = ModelClient(state, lambda *a: None, lambda *a, **k: 30,
                         credential_resolver=lambda *a: KEY, transport=None)
    with pytest.raises((ProviderError, OSError, Exception)):
        client.call("planner", {}, "{}")


def test_mock_transport_is_allowed_and_app_works(tmp_path):
    """Injected MockTransport works; app creates and responds without network."""
    calls = []

    def fixture(request):
        calls.append(request.url.host)
        body = {"model": "test", "choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return httpx.Response(200, json=body)

    app = create_app(tmp_path, model_transport=httpx.MockTransport(fixture))
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        res = client.post("/api/tasks", json={"question": "isolation mock path"})
        assert res.status_code == 201
