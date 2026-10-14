import json
import hashlib
import os
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from typing import Any, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from langgraph.errors import GraphInterrupt
from .providers import ModelClient, SearchClient, BudgetExceeded, ProviderError, verify_citations

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
_checkpoint_setup_lock = threading.RLock()


class ResearchState(TypedDict, total=False):
    task_id: str
    question: str
    direction: str
    execution_mode: str
    provider: str
    mode: str
    review_plan: bool
    max_iterations: int
    max_calls: int
    timeout_seconds: int
    plan: list[str]
    queries: list[str]
    searched: list[str]
    sources: list[dict]
    draft: str
    report: str
    review: dict
    citation_check: dict
    calls: int
    model_calls: int
    simulated_calls: int
    search_calls: int
    input_tokens: int
    output_tokens: int
    usage_complete: bool
    cost_usd: float | None
    cost_complete: bool
    iteration: int
    elapsed_ms: float
    route: str
    stop_reason: str
    error: str
    error_category: str
    failed_node: str
    clarification: str
    recovered: bool
    status: str
    provider_config: dict
    response_models: list[dict]
    research_instructions: str
    temperature: float
    mock_scenario: str
    owner_id: str
    model_config_id: str | None
    max_tokens: int


class Engine:
    def __init__(self, store, model_factory=ModelClient, search_factory=SearchClient, credential_guard=None):
        self.store, self.model_factory, self.search_factory = store, model_factory, search_factory
        self.credential_guard = credential_guard
        self.deadline = None
        self.running_id = None

    @contextmanager
    def graph(self, task_id):
        conn = sqlite3.connect(self.store.checkpoints, check_same_thread=False, timeout=15)
        try:
            # Polling and a freshly scheduled worker may first open the same file.
            # Initialize SQLite/checkpointer schema once at a time before either reads.
            with _checkpoint_setup_lock:
                conn.execute("PRAGMA journal_mode=WAL")
                saver = SqliteSaver(conn)
                saver.setup()
            yield self.build(saver), {"configurable": {"thread_id": task_id}, "recursion_limit": 100}
        finally:
            conn.close()

    def event(self, node, status, detail):
        self.store.event(self.running_id, node, status, detail)

    def guard(self, state, model=False, tool=False):
        if self.credential_guard and state["execution_mode"] == "live":
            self.bind_configuration(state)
            self.credential_guard(state.get("owner_id"), state.get("provider_config", {}))
        task = self.store.get(state["task_id"])
        if task and task["cancel_requested"]:
            raise BudgetExceeded("用户已停止任务")
        remaining = (self.deadline or time.monotonic() + state["timeout_seconds"]) - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("已达到执行时间上限（不计人工等待）")
        ledger = self.store.attempt_counts(state["task_id"])
        if ledger["model"] > state.get("model_calls", 0):
            state["usage_complete"] = False
            state["cost_usd"] = None
        state["calls"] = max(state["calls"], sum(ledger.values()))
        state["model_calls"] = max(state["model_calls"], ledger["model"])
        state["simulated_calls"] = max(state["simulated_calls"], ledger["simulated"])
        state["search_calls"] = max(state["search_calls"], ledger["search"])
        if (model or tool) and state["calls"] >= state["max_calls"]:
            raise BudgetExceeded("已达到总调用预算（包括模型、搜索与重试）")
        if model or tool:
            kind = "search" if tool else "simulated" if state["execution_mode"] == "mock" else "model"
            self.store.attempt(state["task_id"], kind)
        return remaining

    def bind_configuration(self, state):
        # Explicitly claimed legacy checkpoints predate session and credential IDs.
        # Bind only their missing auth metadata from the owner's persisted request.
        if not state.get("owner_id"):
            request = (self.store.get(state["task_id"]) or {}).get("request", {})
            if request.get("owner_id"):
                state["owner_id"] = request["owner_id"]
                if not state.get("provider_config", {}).get("credential_id") and request.get("provider_config", {}).get("credential_id"):
                    state["provider_config"] = dict(request["provider_config"])

    def model(self, state):
        self.bind_configuration(state)
        return self.model_factory(state, self.event, self.guard)

    def payload(self, s):
        return {"question": s.get("direction") or s["question"], "original_question": s["question"],
                "research_instructions": s.get("research_instructions", ""),
                "mock_scenario": s.get("mock_scenario", "standard"),
                "search_queries": s.get("queries", []),
                "plan": s.get("plan", []), "external_sources": s.get("sources", []), "draft": s.get("draft", ""),
                "review": s.get("review", {}), "iteration": s["iteration"]}

    def wrap(self, node, fn, pause=False):
        def run(state):
            s = dict(state)
            started = time.monotonic()
            if not pause:
                self.event(node, "running", "开始执行")
            try:
                if not pause:
                    self.guard(s)
                updates = fn(s)
                s.update(updates or {})
                if not pause:
                    self.guard(s)
                    self.event(node, "done", self.describe(node, s))
            except GraphInterrupt:
                raise
            except BudgetExceeded as exc:
                s.update(route="finalize", stop_reason=str(exc), status="cancelled" if str(exc).startswith("用户已停止") else "limited")
                self.event(node, "limited", str(exc))
            except ProviderError as exc:
                s.update(route="recover", error=str(exc), failed_node=node, error_category=exc.category)
                self.event(node, "error", str(exc))
            except Exception:
                # No raw response, request or environment text is written to the task.
                s.update(route="recover", error="该节点执行异常；检查配置后可从当前节点重试。", failed_node=node)
                self.event(node, "error", s["error"])
            if not pause:
                s["elapsed_ms"] = s.get("elapsed_ms", 0) + (time.monotonic() - started) * 1000
            return s
        return run

    @staticmethod
    def describe(node, s):
        if node == "planner":
            return f"已生成 {len(s.get('plan', []))} 项计划"
        if node == "searcher":
            return f"累计 {len(s.get('sources', []))} 条来源"
        if node == "reviewer":
            return "审查路由：" + s.get("route", "")
        if node == "analyst":
            return f"完成第 {s['iteration']} 轮分析"
        return "节点完成"

    def planner(self, s):
        out = self.model(s).call("planner", self.payload(s),
            '{"plan":["具体子任务"],"queries":["简洁搜索词"],"clarification":"必要时向用户提问，否则空字符串"}')
        plan, queries = out.get("plan"), out.get("queries")
        if not isinstance(plan, list) or not plan or not all(isinstance(x, str) and x.strip() for x in plan):
            raise ProviderError("规划角色未返回有效计划。")
        if not isinstance(queries, list) or not queries or not all(isinstance(x, str) and x.strip() for x in queries):
            raise ProviderError("规划角色未返回有效搜索词。")
        clarification = str(out.get("clarification") or "")[:1000]
        return {"plan": [p[:500] for p in plan[:8]], "queries": [q[:250] for q in queries[:4]],
                "clarification": clarification, "route": "clarify" if clarification else "human_plan" if s["review_plan"] else "searcher"}

    def human_plan(self, s):
        answer = interrupt({"kind": "plan", "message": "请审核计划。可以调整研究方向或逐行编辑计划，然后继续。",
                            "plan": s["plan"], "question": s["question"]})
        direction = str(answer.get("direction") or "").strip()
        plan = answer.get("plan")
        return {"plan": plan or s["plan"], "direction": direction or s.get("direction", ""),
                "queries": [direction[:250]] if direction else s["queries"], "route": "searcher", "status": "running"}

    def clarify(self, s):
        answer = interrupt({"kind": "clarification", "message": s.get("clarification") or "当前资料不足，请补充更具体的研究对象、时间范围或关键词。",
                            "plan": s.get("plan", []), "question": s["question"]})
        direction = str(answer.get("direction") or "").strip()
        if not direction:
            # A missing answer is harmless and remains an explicit request for user input.
            return {"route": "clarify", "clarification": "请填写补充说明后继续。"}
        return {"direction": direction, "queries": [direction[:250]], "plan": answer.get("plan") or s.get("plan", []),
                "route": "searcher", "status": "running"}

    def recover(self, s):
        answer = interrupt({"kind": "error", "message": s["error"], "category": s.get("error_category", "provider_error"), "failed_node": s["failed_node"],
                            "plan": s.get("plan", []), "question": s["question"]})
        direction = str(answer.get("direction") or "").strip()
        return {"route": s["failed_node"], "error": "", "direction": direction or s.get("direction", ""),
                "queries": [direction[:250]] if direction else s.get("queries", []), "status": "running", "recovered": True}

    def searcher(self, s):
        if self.native(s):
            self.model(s).call("searcher", self.payload(s), '{"done":true,"notes":"工具检索覆盖范围与局限"}')
            if not s.get("sources"):
                return {"route": "clarify", "clarification": "原生搜索工具没有返回资料，请明确关键词。"}
            return {"route": "single" if s["mode"] == "single" else "analyst"}
        found = list(s.get("sources", []))
        searched = list(s.get("searched", []))
        queries = [x for x in s.get("queries", [s["question"]]) if x not in searched][:2]
        if not queries:
            queries = [(s.get("direction") or s["question"])[:160] + " evidence " + str(s["iteration"])]
        for query in queries:
            # The search adapter charges each HTTP attempt, including the retry.
            def charge():
                remaining = self.guard(s, tool=True)
                s["calls"] += 1
                s["search_calls"] += 1
                return remaining
            search = self.search_factory(s["execution_mode"], self.event, timeout=self.guard(s))
            search.engine = s.get("provider_config", {}).get("search", "wikipedia")
            search.before_attempt = charge
            results = search.search(query)
            searched.append(query)
            for item in results:
                if item["url"] not in {x["url"] for x in found}:
                    item = dict(item, id=str(len(found) + 1), retrieved_at=time.time())
                    found.append(item)
            self.event("searcher", "evidence", f"查询「{query}」返回 {len(results)} 条结果")
            s.update(sources=found[:24], searched=searched)
        s.update(sources=found[:24], searched=searched)
        if not found:
            return {"sources": [], "searched": searched, "clarification": "没有检索到匹配的资料。请缩小范围或改用简洁关键词。", "route": "clarify"}
        return {"sources": found[:24], "searched": searched, "route": "single" if s["mode"] == "single" else "analyst"}

    def analyst(self, s):
        out = self.model(s).call("analyst", self.payload(s),
            '{"report":"中文 Markdown，含结论、依据、局限，使用 [1] 格式引用输入来源","missing":["缺失信息"],"queries":["补充搜索词"]}')
        report = out.get("report")
        if not isinstance(report, str) or len(report.strip()) < 20:
            raise ProviderError("分析角色未返回有效研究草稿。")
        return {"draft": report[:30000], "route": "reviewer"}

    def reviewer(self, s):
        check = verify_citations(s.get("draft", ""), s.get("sources", []))
        payload = dict(self.payload(s), deterministic_citation_check=check)
        out = self.model(s).call("reviewer", payload,
            '{"decision":"finish|search_more|revise|clarify","reasons":["具体审查依据"],"queries":["需要时补搜关键词"],"clarification":"需要时向用户提问"}')
        decision = out.get("decision")
        if decision not in {"finish", "search_more", "revise", "clarify"}:
            raise ProviderError("审查角色给出了不受支持的路由。")
        reasons = out.get("reasons", [])
        if not check["all_mapped"]:
            decision = "revise"
            reasons = [*reasons, "程序核验：存在缺失或不存在的引用编号，必须修正。"]
        if not s.get("sources"):
            decision = "search_more"
        route = {"finish": "finalize", "search_more": "searcher", "revise": "analyst", "clarify": "clarify"}[decision]
        updates = {"citation_check": check, "review": {"decision": decision, "reasons": reasons}, "route": route}
        if decision != "finish" and s["iteration"] >= s["max_iterations"]:
            updates.update(route="finalize", stop_reason="达到最大迭代次数，保留未通过的草稿", status="limited")
        elif decision != "finish":
            updates["iteration"] = s["iteration"] + 1
        if decision == "search_more":
            queries = out.get("queries") or []
            updates["queries"] = [str(x)[:250] for x in queries[:3]] or [(s.get("direction") or s["question"])[:180] + " evidence limitations"]
        if decision == "clarify":
            updates["clarification"] = str(out.get("clarification") or "请明确研究范围和需要补充的资料。")[:1500]
        return updates

    def single(self, s):
        out = self.model(s).call("single", self.payload(s),
            '{"report":"中文 Markdown，必须使用输入来源编号 [1] 并说明局限；资料不足可留空", "queries":["需要额外搜索才填写"], "clarification":"需要用户补充才填写"}')
        report = out.get("report", "")
        if out.get("clarification"):
            return {"route": "clarify", "clarification": str(out["clarification"])[:1500]}
        if out.get("queries") and s["iteration"] < s["max_iterations"]:
            return {"draft": str(report)[:30000], "queries": [str(x)[:250] for x in out["queries"][:3]],
                    "iteration": s["iteration"] + 1, "route": "single" if self.native(s) else "searcher"}
        if out.get("queries"):
            return {"draft": str(report)[:30000], "route": "finalize", "status": "limited", "stop_reason": "单 Agent 达到最大迭代次数，仍需要补充检索"}
        if not isinstance(report, str) or len(report.strip()) < 20:
            raise ProviderError("单 Agent 未返回有效报告。")
        return {"draft": report[:30000], "route": "finalize"}

    def finalize(self, s):
        report = s.get("draft", "")
        check = verify_citations(report, s.get("sources", []))
        status = s.get("status") if s.get("status") in {"limited", "cancelled"} else "completed" if check["all_mapped"] else "limited"
        if s.get("stop_reason"):
            report += "\n\n## 执行停止说明\n" + s["stop_reason"] + "。以下输出保留为未完成结果。"
        if not report.strip():
            report = "# 研究未完成\n\n" + (s.get("stop_reason") or "没有生成有效报告，请查看执行记录。")
        if not check["all_mapped"]:
            report += "\n\n⚠ 引用映射未通过，不能把本结果作为可靠研究结论。"
        return {"report": report, "citation_check": check, "status": status, "route": "end"}

    def build(self, checkpointer):
        builder = StateGraph(ResearchState)
        names = ["planner", "human_plan", "searcher", "analyst", "reviewer", "clarify", "recover", "single", "finalize"]
        for name in names:
            builder.add_node(name, self.wrap(name, getattr(self, name), pause=name in {"human_plan", "clarify", "recover", "finalize"}))
        builder.add_conditional_edges(START, lambda s: ("single" if self.native(s) else "searcher") if s["mode"] == "single" else "planner")
        for name in names:
            if name == "finalize":
                builder.add_edge(name, END)
            else:
                builder.add_conditional_edges(name, lambda s: s["route"], {n: n for n in names})
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def native(s):
        return s["execution_mode"] == "live" and s.get("provider_config", {}).get("tool_mode") == "native"

    @staticmethod
    def initial(task_id, request):
        provider = request["provider"].upper()
        from .configuration import PRESETS
        defaults = {p.upper(): values for p, values in PRESETS.items()}
        frozen = {"provider": request["provider"], "base_url": os.getenv(provider + "_BASE_URL", defaults[provider][0]),
                  "model": os.getenv(provider + "_MODEL", defaults[provider][1]), "temperature": request.get("temperature", 0),
                  "protocol": defaults[provider][2], "max_tokens": request.get("max_tokens", 2600), "supports_tools": True,
                  "input_price": os.getenv(provider + "_INPUT_PRICE_PER_MILLION", ""),
                  "output_price": os.getenv(provider + "_OUTPUT_PRICE_PER_MILLION", ""),
                  "tool_mode": os.getenv("AGENTLAB_TOOL_MODE", "json"),
                  "search": os.getenv("AGENTLAB_SEARCH", "wikipedia")}
        frozen = dict(request.get("provider_config", frozen))
        frozen["research_instructions_sha256"] = hashlib.sha256(request.get("research_instructions", "").encode()).hexdigest()
        return {**request, "provider_config": frozen, "response_models": [], "task_id": task_id, "direction": "", "plan": [], "queries": [request["question"][:250]],
                "searched": [], "sources": [], "draft": "", "report": "", "calls": 0, "model_calls": 0,
                "simulated_calls": 0, "search_calls": 0, "input_tokens": 0, "output_tokens": 0,
                "usage_complete": True, "cost_usd": None, "cost_complete": True, "iteration": 1, "elapsed_ms": 0., "status": "running",
                "route": "", "stop_reason": "", "error": "", "recovered": False}

    def run(self, task_id, resume=None, retry=False):
        self.running_id = task_id
        task = self.store.get(task_id)
        self.event("workflow", "configuration", "研究策略 SHA256: " + hashlib.sha256(task["request"].get("research_instructions", "").encode()).hexdigest())
        self.store.update(task_id, status="running", error="")
        with self.graph(task_id) as (graph, config):
            snapshot = graph.get_state(config)
            previous = snapshot.values or {}
            remaining = task["request"]["timeout_seconds"] - previous.get("elapsed_ms", 0) / 1000
            self.deadline = time.monotonic() + remaining
            if previous:
                arg = Command(resume=resume) if resume is not None else None
            else:
                arg = self.initial(task_id, task["request"])
            try:
                for update in graph.stream(arg, config, stream_mode="updates"):
                    if "__interrupt__" in update:
                        item = update["__interrupt__"][0].value
                        self.event("human", "paused", item["message"])
                snapshot = graph.get_state(config)
                state = dict(snapshot.values)
                paused = [i.value for t in snapshot.tasks for i in t.interrupts]
                state["interrupts"] = paused
                state["trace"] = [{"node": e["node"], "status": e["status"], "detail": e["detail"]} for e in self.store.events(task_id)]
                status = "paused" if paused else state.get("status", "completed")
                self.store.update(task_id, status=status, result=state, error=state.get("error", ""))
                self.event("workflow", status, "等待人工处理" if paused else "执行结束")
                return state
            except Exception:
                self.store.update(task_id, status="interrupted", error="工作流执行中断；最近检查点已保留，可继续运行。")
                self.event("workflow", "interrupted", "工作流中断，保留检查点")
                raise

    def snapshot(self, task_id):
        with self.graph(task_id) as (graph, config):
            snapshot = graph.get_state(config)
            state = dict(snapshot.values)
            state["interrupts"] = [i.value for t in snapshot.tasks for i in t.interrupts]
            return state


def benchmark_result(task, state, store):
    mock = task["request"]["execution_mode"] == "mock"
    token_known = state.get("usage_complete", True) and not mock
    return {"schema_version": "1.0", "task_id": task["request"].get("external_task_id", task["id"]),
            "run_id": task["id"], "mode": task["request"]["mode"], "execution_mode": task["request"]["execution_mode"],
            "output": state.get("report", state.get("draft", "")), "sources": state.get("sources", []),
            "metrics": {"latency_ms": round(state.get("elapsed_ms", 0), 2), "model_calls": state.get("model_calls", 0),
                        "simulated_model_calls": state.get("simulated_calls", 0), "search_calls": state.get("search_calls", 0),
                        "input_tokens": state.get("input_tokens", 0) if token_known else None,
                        "output_tokens": state.get("output_tokens", 0) if token_known else None,
                        "cost_usd": state.get("cost_usd"), "success": task["status"] == "completed",
                        "usage_complete": token_known},
            "trace": [{"node": e["node"], "status": e["status"], "detail": e["detail"]} for e in store.events(task["id"])],
            "citation_check": state.get("citation_check", {}), "status": task["status"],
            "model_config": state.get("provider_config", task["request"].get("provider_config", {})),
            "warning": "离线固定演示；不能当作模型能力或架构实验结论。" if mock else "引用映射核验不等于事实正确性。"}
