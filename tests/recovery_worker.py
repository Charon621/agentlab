"""Invoked by test_recovery.py in a separate OS process."""
import sys
import time
from pathlib import Path
from agentlab.engine import Engine
from agentlab.models import TaskRequest
from agentlab.providers import ModelClient
from agentlab.storage import Store

mode, folder = sys.argv[1:]
folder = Path(folder)
store = Store(folder)
if mode == "pause":
    store.create("restart", TaskRequest(question="跨进程恢复研究任务").model_dump())
    Engine(store).run("restart")
    assert store.get("restart")["status"] == "paused"
elif mode == "resume":
    Engine(store).run("restart", resume={"direction": "只研究 SQLite 持久化", "plan": ["检索 SQLite", "核验引用"]})
    assert store.get("restart")["status"] == "completed"
elif mode == "crash":
    class Slow(ModelClient):
        def call(self, role, payload, schema):
            if role == "analyst":
                (folder / "in_analyst.txt").write_text("started", encoding="utf-8")
                time.sleep(60)
            return super().call(role, payload, schema)
    store.create("crashed", TaskRequest(question="强制中断后恢复研究", review_plan=False).model_dump())
    Engine(store, model_factory=Slow).run("crashed")
elif mode == "recover":
    store.recover_interrupted()
    assert store.get("crashed")["status"] == "interrupted"
    Engine(store).run("crashed")
    assert store.get("crashed")["status"] == "completed"
