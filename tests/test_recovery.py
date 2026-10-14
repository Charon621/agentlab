import os
import subprocess
import sys
import time
from pathlib import Path
from agentlab.storage import Store

ROOT = Path(__file__).resolve().parent.parent


def worker(mode, directory):
    return subprocess.run([sys.executable, "-m", "tests.recovery_worker", mode, str(directory)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)


def test_pause_resume_survives_two_real_processes(tmp_path):
    one = worker("pause", tmp_path)
    assert one.returncode == 0, one.stderr
    two = worker("resume", tmp_path)
    assert two.returncode == 0, two.stderr
    state = Store(tmp_path).get("restart")["result"]
    assert state["direction"] == "只研究 SQLite 持久化" and state["model_calls"] == 0


def test_abrupt_process_kill_resumes_last_completed_node(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "tests.recovery_worker", "crash", str(tmp_path)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 12
        while not (tmp_path / "in_analyst.txt").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (tmp_path / "in_analyst.txt").exists(), "worker did not reach analyst"
        proc.terminate()
        proc.wait(timeout=5)
        restored = worker("recover", tmp_path)
        assert restored.returncode == 0, restored.stderr
        store = Store(tmp_path)
        assert store.get("crashed")["status"] == "completed"
        # The completed planner and search are not re-run after the kill.
        done = [e["node"] for e in store.events("crashed") if e["status"] == "done"]
        assert done.count("planner") == 1 and done.count("searcher") == 1
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stderr:
            proc.stderr.close()
