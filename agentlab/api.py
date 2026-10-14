import asyncio
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .engine import Engine, benchmark_result
from .models import TaskRequest, ResumeRequest, BenchmarkRequest
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent
# Only this project's explicit .env; never discover a parent/sibling .env.
load_dotenv(ROOT / ".env", override=False)


def create_app(data_dir=None):
    store = Store(Path(data_dir or os.getenv("AGENTLAB_DATA_DIR", ROOT / "data")))
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

    @app.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "请从本应用页面提交请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def get_task(task_id):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    def run(task_id, resume=None):
        try:
            Engine(store).run(task_id, resume=resume)
        except Exception:
            # Engine persists a safe, user-visible recovery message.
            pass
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

    def new_task(req):
        task_id = uuid.uuid4().hex
        req["provider_config"] = Engine.initial(task_id, req)["provider_config"]
        if req.get("parent_id"):
            parent = get_task(req["parent_id"])
            if parent["status"] not in {"completed", "limited"}:
                raise HTTPException(409, "请等上一轮结束后再追问")
            old = parent.get("result") or {}
            req["question"] = req["question"] + "\n\n上一轮研究问题：" + parent["question"][:1000] + "\n上一轮报告（需要重新核验）：" + old.get("report", "")[:6000]
        store.create(task_id, req)
        return task_id

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "agentlab", "schema_version": "1.0", "version": "1.0.0",
                "providers": {p: {"configured": bool(os.getenv(p.upper() + "_API_KEY")),
                                   "model": os.getenv(p.upper() + "_MODEL", "qwen-plus" if p == "qwen" else "moonshot-v1-8k")}
                              for p in ["qwen", "kimi"]},
                "search": os.getenv("AGENTLAB_SEARCH", "wikipedia"), "live_calls_require_explicit_selection": True}

    @app.get("/api/tasks")
    def tasks():
        return store.list()

    @app.post("/api/tasks", status_code=201)
    def create(req: TaskRequest):
        task_id = new_task(req.model_dump())
        schedule(task_id)
        return {"id": task_id, "status": "queued"}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        result = get_task(task_id)
        try:
            state = Engine(store).snapshot(task_id)
            if state.get("task_id"):
                result["result"] = {**state, "interrupts": state.get("interrupts", [])}
        except Exception:
            pass
        result["events"] = store.events(task_id)
        return result

    @app.post("/api/tasks/{task_id}/resume")
    def resume(task_id: str, req: ResumeRequest):
        task = get_task(task_id)
        if task["status"] not in {"paused", "interrupted"}:
            raise HTTPException(409, "只有暂停或中断的任务可以继续")
        state = Engine(store).snapshot(task_id)
        interrupts = state.get("interrupts", [])
        if interrupts and interrupts[0].get("kind") == "clarification" and not req.direction.strip():
            raise HTTPException(422, "请填写补充说明")
        if req.plan is not None and any(not p.strip() or len(p) > 500 for p in req.plan):
            raise HTTPException(422, "每项计划需为 1–500 字")
        schedule(task_id, req.model_dump() if interrupts else None)
        return {"id": task_id, "status": "queued"}

    @app.post("/api/tasks/{task_id}/stop")
    def stop(task_id: str):
        task = get_task(task_id)
        if task["status"] not in {"running", "queued"}:
            raise HTTPException(409, "任务当前未运行")
        store.update(task_id, cancel=1)
        store.event(task_id, "workflow", "stopping", "已请求停止，当前有超时限制的网络调用结束后生效。")
        return {"status": "stopping"}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        get_task(task_id)
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
    def export(task_id: str, format: str = "markdown"):
        task = get_task(task_id)
        state = task.get("result") or Engine(store).snapshot(task_id)
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
    def benchmark(req: BenchmarkRequest):
        payload = TaskRequest(question=req.question, mode=req.mode, execution_mode=req.execution_mode, provider=req.provider,
                              review_plan=False, max_calls=req.max_calls, timeout_seconds=req.timeout_seconds,
                              research_instructions=req.research_instructions, temperature=req.temperature).model_dump()
        payload["external_task_id"] = req.task_id
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        payload["request_fingerprint"] = fingerprint
        with benchmark_lock:
            existing = store.find_benchmark(req.task_id, fingerprint)
            if existing:
                if existing["status"] in {"queued", "running"}:
                    raise HTTPException(409, f"该 task_id 已在执行，run_id={existing['id']}，请读取已有任务")
                return benchmark_result(existing, existing.get("result") or {}, store)
            task_id = new_task(payload)
        state = Engine(store).run(task_id)
        return benchmark_result(get_task(task_id), state, store)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")
    return app


app = create_app()
