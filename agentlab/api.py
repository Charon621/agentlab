import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .engine import Engine, benchmark_result
from .models import TaskRequest, ResumeRequest, BenchmarkRequest, ModelConfigRequest, GrantRequest, CredentialRequest
from .storage import Store
from .configuration import ModelRegistry, PRESETS
from .providers import ModelClient, ProviderError, SearchClient
from .network import validate_model_url
import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
# Only this project's explicit .env; never discover a parent/sibling .env.
load_dotenv(ROOT / ".env", override=False)


def create_app(data_dir=None, model_transport=None, allow_private_fixture=False):
    if allow_private_fixture and not isinstance(model_transport, httpx.MockTransport):
        raise ValueError("Private endpoints require an explicitly injected MockTransport fixture")
    store = Store(Path(data_dir or os.getenv("AGENTLAB_DATA_DIR", ROOT / "data")))
    registry = ModelRegistry(store)
    model_factory = partial(ModelClient, credential_resolver=registry.resolve, transport=model_transport, allow_fixture=allow_private_fixture)

    def engine():
        return Engine(store, model_factory=model_factory, credential_guard=registry.resolve)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agentlab")
    active = set()
    lock = threading.Lock()
    benchmark_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AgentLab 研究工作台", version="1.0.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.state.store = store
    app.state.models = registry

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request, exc):
        # FastAPI's default errors include the rejected input, including api_key.
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, status_code=422)

    @app.middleware("http")
    async def local_origin(request, call_next):
        owner, new_cookie = registry.session(request.cookies.get("agentlab_session"))
        request.state.owner_id = owner
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host"):
                return JSONResponse({"detail": "请从本应用页面提交请求。"}, status_code=403)
        response = await call_next(request)
        if new_cookie:
            response.set_cookie("agentlab_session", new_cookie, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=31536000)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def get_task(task_id, owner):
        task = store.get(task_id)
        if not task or task["request"].get("owner_id") != owner:
            raise HTTPException(404, "任务不存在")
        return task

    def run(task_id, resume=None):
        try:
            engine().run(task_id, resume=resume)
        except Exception:
            # Engine persists a safe, user-visible recovery message.
            current = store.get(task_id)
            if current and current["status"] in {"queued", "running"}:
                store.update(task_id, status="interrupted", error="工作流初始化中断；请从检查点继续。")
                store.event(task_id, "workflow", "interrupted", "初始化未完成，任务可恢复。")
        finally:
            with lock:
                active.discard(task_id)

    def schedule(task_id, resume=None):
        with lock:
            if task_id in active:
                raise HTTPException(409, "任务已经运行中")
            active.add(task_id)
        store.update(task_id, status="queued", cancel=0)
        executor.submit(run, task_id, resume)

    def new_task(req, owner, grant=None):
        task_id = uuid.uuid4().hex
        req["owner_id"] = owner
        if req["execution_mode"] == "live":
            config_id = req.get("model_config_id")
            if not config_id:
                config_id = registry.list(owner)["default_model_config_id"]
            if config_id:
                frozen = dict(req.get("provider_config") or registry.freeze(owner, config_id))
                req.update(provider=frozen["provider"], model_config_id=config_id, temperature=frozen["temperature"], max_tokens=frozen["max_tokens"])
                req["provider_config"] = dict(frozen)
                if grant:
                    req["provider_config"]["integration_grant_id"] = grant["id"]
            else:
                # Legacy request remains recoverable, but does not borrow global env keys.
                frozen = Engine.initial(task_id, req)["provider_config"]
                req["provider_config"] = {**frozen, "id": "unconfigured", "config_version": 0, "credential_id": "missing"}
        req["provider_config"] = Engine.initial(task_id, req)["provider_config"]
        if req.get("parent_id"):
            parent = get_task(req["parent_id"], owner)
            if parent["status"] not in {"completed", "limited"}:
                raise HTTPException(409, "请等上一轮结束后再追问")
            old = parent.get("result") or {}
            req["question"] = req["question"] + "\n\n上一轮研究问题：" + parent["question"][:1000] + "\n上一轮报告（需要重新核验）：" + old.get("report", "")[:6000]
        store.create(task_id, req)
        return task_id

    @app.get("/api/health")
    def health(request: Request):
        return {"status": "ok", "app": "agentlab", "schema_version": "1.0", "version": "1.0.0",
                "providers": {p: {"configured": any(c["provider"] == p and c["configured"] for c in registry.list(request.state.owner_id)["models"]), "model": values[1]}
                              for p, values in PRESETS.items()},
                "search": os.getenv("AGENTLAB_SEARCH", "wikipedia"), "live_calls_require_explicit_selection": True}

    @app.get("/api/tasks")
    def tasks(request: Request):
        return store.list(request.state.owner_id)

    @app.post("/api/tasks", status_code=201)
    def create(req: TaskRequest, request: Request):
        task_id = new_task(req.model_dump(), request.state.owner_id)
        schedule(task_id)
        return {"id": task_id, "status": "queued"}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, request: Request):
        result = get_task(task_id, request.state.owner_id)
        try:
            state = engine().snapshot(task_id)
            if state.get("task_id"):
                result["result"] = {**state, "interrupts": state.get("interrupts", [])}
        except Exception:
            pass
        result["events"] = store.events(task_id)
        return result

    @app.post("/api/tasks/{task_id}/resume")
    def resume(task_id: str, req: ResumeRequest, request: Request):
        task = get_task(task_id, request.state.owner_id)
        if task["status"] not in {"paused", "interrupted"}:
            raise HTTPException(409, "只有暂停或中断的任务可以继续")
        state = engine().snapshot(task_id)
        interrupts = state.get("interrupts", [])
        if interrupts and interrupts[0].get("kind") == "clarification" and not req.direction.strip():
            raise HTTPException(422, "请填写补充说明")
        if req.plan is not None and any(not p.strip() or len(p) > 500 for p in req.plan):
            raise HTTPException(422, "每项计划需为 1–500 字")
        schedule(task_id, req.model_dump() if interrupts else None)
        return {"id": task_id, "status": "queued"}

    @app.post("/api/tasks/{task_id}/stop")
    def stop(task_id: str, request: Request):
        task = get_task(task_id, request.state.owner_id)
        if task["status"] not in {"running", "queued"}:
            raise HTTPException(409, "任务当前未运行")
        store.update(task_id, cancel=1)
        store.event(task_id, "workflow", "stopping", "已请求停止，当前有超时限制的网络调用结束后生效。")
        return {"status": "stopping"}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        get_task(task_id, request.state.owner_id)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            pass
        async def generate():
            cursor = after
            while not await request.is_disconnected():
                items = store.events(task_id, cursor)
                for event in items:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                task = store.get(task_id)
                if task["status"] not in {"queued", "running"}:
                    yield "event: state\ndata: " + json.dumps({"status": task["status"]}) + "\n\n"
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/tasks/{task_id}/export")
    def export(task_id: str, request: Request, format: str = "markdown"):
        task = get_task(task_id, request.state.owner_id)
        state = task.get("result") or engine().snapshot(task_id)
        if format == "json":
            result = benchmark_result(task, state, store)
            result["configuration"] = {**task["request"], "provider_config": state.get("provider_config", {})}
            return PlainTextResponse(json.dumps(result, ensure_ascii=False, indent=2), media_type="application/json",
                                     headers={"Content-Disposition": f'attachment; filename="agentlab-{task_id}.json"'})
        if format != "markdown":
            raise HTTPException(422, "支持 markdown 或 json")
        text = state.get("report") or state.get("draft") or "尚未生成报告。"
        text += "\n\n## 来源\n" + "\n".join(f"[{s['id']}] {s['title']}\n{s['url']}\n片段：{s['excerpt']}\n" for s in state.get("sources", []))
        text += "\n\n> 引用映射不代表事实正确。执行模式：" + task["request"]["execution_mode"]
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="agentlab-{task_id}.md"'})

    @app.post("/api/benchmark/run")
    def benchmark(req: BenchmarkRequest, request: Request):
        grant = None
        owner = request.state.owner_id
        if request.headers.get("authorization"):
            if not req.model_config_id:
                raise HTTPException(422, "联动请求需明确 model_config_id")
            grant = registry.authorize(request.headers.get("authorization"), req.model_config_id)
            owner = grant["owner_id"]
        payload = TaskRequest(question=req.question, mode=req.mode, execution_mode=req.execution_mode, provider=req.provider,
                              review_plan=False, max_calls=req.max_calls, timeout_seconds=req.timeout_seconds,
                              research_instructions=req.research_instructions, temperature=req.temperature,
                              model_config_id=req.model_config_id, max_tokens=req.max_tokens).model_dump()
        payload["owner_id"] = owner
        config_id = req.model_config_id or (registry.list(owner)["default_model_config_id"] if req.execution_mode == "live" else None)
        if config_id:
            frozen = registry.freeze(owner, config_id)
            if req.expected_config_version is not None and req.expected_config_version != frozen["config_version"]:
                raise HTTPException(409, "模型配置版本已变化。请重新导入配置或恢复原版本后启动实验。")
            payload.update(model_config_id=config_id, provider=frozen["provider"], temperature=frozen["temperature"], max_tokens=frozen["max_tokens"])
            payload["provider_config"] = frozen
        if grant:
            payload["provider_config"]["integration_grant_id"] = grant["id"]
        payload["external_task_id"] = req.task_id
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        payload["request_fingerprint"] = fingerprint
        with benchmark_lock:
            existing = store.find_benchmark(req.task_id, fingerprint, owner)
            if existing:
                if existing["status"] in {"queued", "running"}:
                    raise HTTPException(409, f"该 task_id 已在执行，run_id={existing['id']}，请读取已有任务")
                return benchmark_result(existing, existing.get("result") or {}, store)
            task_id = new_task(payload, owner, grant)
        state = engine().run(task_id)
        return benchmark_result(get_task(task_id, owner), state, store)

    def validate_config(req):
        values = req.model_dump()
        try:
            values["base_url"] = validate_model_url(values["base_url"], allow_private_fixture)
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        return values

    @app.get("/api/models")
    def models(request: Request):
        return registry.list(request.state.owner_id)

    @app.get("/api/history/unclaimed")
    def unclaimed_history():
        return {"count": registry.unclaimed_history()}

    @app.post("/api/history/claim")
    def claim_history(request: Request):
        return registry.claim_history(request.state.owner_id)

    @app.post("/api/models", status_code=201)
    def add_model(req: ModelConfigRequest, request: Request):
        return registry.save(request.state.owner_id, validate_config(req))

    @app.put("/api/models/{config_id}")
    def edit_model(config_id: str, req: ModelConfigRequest, request: Request):
        return registry.save(request.state.owner_id, validate_config(req), config_id)

    @app.delete("/api/models/{config_id}")
    def delete_model(config_id: str, request: Request):
        registry.delete(request.state.owner_id, config_id)
        return {"deleted": True}

    @app.post("/api/models/{config_id}/default")
    def default_model(config_id: str, request: Request):
        return registry.set_default(request.state.owner_id, config_id)

    @app.delete("/api/models/{config_id}/credential")
    def delete_key(config_id: str, request: Request):
        registry.delete_credentials(request.state.owner_id, config_id)
        return {"configured": False}

    @app.post("/api/models/{config_id}/credentials")
    def restore_key(config_id: str, req: CredentialRequest, request: Request):
        if "\n" in req.api_key or "\r" in req.api_key:
            raise HTTPException(422, "密钥不得包含换行")
        return registry.restore_credential(request.state.owner_id, config_id, req.credential_id, req.api_key)

    @app.post("/api/models/import-env/{provider}")
    def import_env(provider: str, request: Request):
        if provider not in PRESETS or provider == "custom":
            raise HTTPException(422, "不支持此提供商")
        key = os.getenv(provider.upper() + "_API_KEY")
        if not key:
            raise HTTPException(422, "本项目环境中没有该提供商密钥")
        registry.claim_env(request.state.owner_id, provider)
        base, model, protocol = PRESETS[provider]
        try:
            req = ModelConfigRequest(name=provider + " / 项目环境", provider=provider, protocol=protocol,
                model=os.getenv(provider.upper() + "_MODEL", model), base_url=os.getenv(provider.upper() + "_BASE_URL", base), api_key=key,
                supports_tools=os.getenv("AGENTLAB_TOOL_MODE", "json") == "native", tool_mode=os.getenv("AGENTLAB_TOOL_MODE", "json"),
                input_price=os.getenv(provider.upper() + "_INPUT_PRICE_PER_MILLION") or None,
                output_price=os.getenv(provider.upper() + "_OUTPUT_PRICE_PER_MILLION") or None)
        except ValidationError:
            raise HTTPException(422, "本项目环境配置无效，请核对模型、工具模式和价格。") from None
        return registry.save(request.state.owner_id, validate_config(req))

    @app.post("/api/models/{config_id}/test")
    def test_model(config_id: str, request: Request):
        config = registry.get(request.state.owner_id, config_id)
        s = {"execution_mode": "live", "provider": config["provider"], "provider_config": {**config, "max_tokens": 16},
             "owner_id": request.state.owner_id, "calls": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "usage_complete": True}
        started = time.monotonic()
        try:
            model_factory(s, lambda *args: None, lambda *args, **kwargs: 10).call("connection", {"instruction": "Reply OK."}, "{}", _raw=True)
            result = {"ok": True, "category": "connected", "message": "模型接口已返回有效响应。该测试会产生一次最小模型请求。", "model": (s.get("response_models") or [{}])[-1].get("model")}
        except ProviderError as exc:
            result = {"ok": False, "category": exc.category, "message": str(exc), "model": config["model"]}
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["config_version"] = config["config_version"]
        result["tested_at"] = time.time()
        registry.record_test(request.state.owner_id, config_id, config["config_version"], result)
        return result

    @app.post("/api/integration/grants", status_code=201)
    def grant_model(req: GrantRequest, request: Request):
        try:
            return registry.grant(request.state.owner_id, req.model_config_id)
        except ProviderError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/integration/grants")
    def grant_list(request: Request):
        return registry.list_grants(request.state.owner_id)

    @app.delete("/api/integration/grants/{grant_id}")
    def revoke_grant(grant_id: str, request: Request):
        return registry.revoke(request.state.owner_id, grant_id)

    @app.get("/api/integration/model")
    def integration_model(model_config_id: str, request: Request):
        grant = registry.authorize(request.headers.get("authorization"), model_config_id)
        return {"model_config": registry.public(grant["owner_id"], registry.get(grant["owner_id"], model_config_id)), "expires": grant["expires"]}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")
    return app


app = create_app()
