"""A08: verify experiment plan structure and frozen comparison metadata."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_experiment_plan_has_60_units_and_metadata():
    plan_path = ROOT / "examples" / "experiment_plan.json"
    if not plan_path.exists():
        import subprocess
        subprocess.run(
            [str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "scripts" / "prepare_experiment.py")],
            cwd=str(ROOT), capture_output=True,
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # 15 questions × 2 modes × 2 repeats = 60
    assert plan["total_units"] == 60
    assert len(plan["units"]) == 60
    # Each unit has unique ID
    ids = [u["unit_id"] for u in plan["units"]]
    assert len(set(ids)) == 60
    # Metadata present
    assert "dataset_hash" in plan and len(plan["dataset_hash"]) == 64
    assert "prompt_hash" in plan and len(plan["prompt_hash"]) == 64
    assert "code_state" in plan and "head" in plan["code_state"]
    assert "seed" in plan
    # Frozen config shared by both architectures
    fc = plan["frozen_config"]
    assert fc["temperature"] == 0
    assert fc["internal_call_upper_bound"] > 0
    # Search cache schema with symmetric miss policy
    assert "miss_policy" in plan["search_cache_schema"]
    # Quality rubric independent of Agent self-review
    assert "Human" in plan["quality_rubric"]["scorer"] or "human" in plan["quality_rubric"]["scorer"].lower()
    # All units have required fields
    for u in plan["units"]:
        assert "unit_id" in u
        assert "case_id" in u
        assert "mode" in u and u["mode"] in ("single", "multi")
        assert "repeat" in u
        assert "order" in u
        assert "rubric" in u


def test_experiment_plan_mode_alternation():
    """Within each repeat, single/multi order alternates so one side doesn't always go first."""
    plan_path = ROOT / "examples" / "experiment_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    # Group by case_id and repeat
    from collections import defaultdict
    by_case = defaultdict(list)
    for u in plan["units"]:
        by_case[(u["case_id"], u["repeat"])].append(u)
    for key, units in by_case.items():
        modes = [u["mode"] for u in sorted(units, key=lambda x: x["order"])]
        assert len(modes) == 2
        assert set(modes) == {"single", "multi"}
        assert modes == (['single','multi'] if key[1]%2==0 else ['multi','single'])
