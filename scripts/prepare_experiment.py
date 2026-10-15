"""A08: prepare fair architecture comparison plan and reproducible retrieval cache.

Generates experiment plan (question × single/multi × repeat), frozen search cache
schema, and quality evaluation rubric. Default: no real model calls.
"""
import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state() -> dict:
    def run(*args):
        r = subprocess.run(["git", "-C", str(ROOT)] + list(args), capture_output=True)
        return r.stdout.decode("utf-8", errors="replace").strip()
    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--short")),
        "status_short": run("status", "--short").splitlines(),
    }


def build_plan(tasks: list, repeats: int, seed: int) -> list:
    """Build execution order: question × single/multi × repeat.

    Order alternates single/multi per repeat so one side doesn't always go first.
    """
    import random
    rng = random.Random(seed)
    units = []
    for task in tasks:
        for repeat in range(repeats):
            modes = ["single", "multi"] if repeat % 2 == 0 else ["multi", "single"]
            for mode in modes:
                units.append({
                    "unit_id": f"{task['id']}-{mode}-r{repeat}",
                    "case_id": task["id"],
                    "difficulty": task["difficulty"],
                    "question": task["question"],
                    "mode": mode,
                    "repeat": repeat,
                    "rubric": task["rubric"],
                })
    # Shuffle paired blocks; preserve the counterbalanced within-pair order.
    blocks=[units[i:i+2] for i in range(0,len(units),2)]
    rng.shuffle(blocks)
    units=[unit for block in blocks for unit in block]
    for i, u in enumerate(units):
        u["order"] = i
    return units


def main():
    parser = argparse.ArgumentParser(description="Generate experiment plan and frozen retrieval schema")
    parser.add_argument("--tasks", type=Path, default=ROOT / "examples" / "research_tasks.json")
    parser.add_argument("--output", type=Path, default=ROOT / "examples" / "experiment_plan.json")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    assert len(tasks) == 15, f"Expected 15 tasks, got {len(tasks)}"

    git = git_state()
    prompt = "说明结论边界、反例与资料来源；证据不足时明确标记。"

    plan = {
        "schema_version": "1.0",
        "experiment_id": uuid.uuid4().hex,
        "created": datetime.now(timezone.utc).isoformat(),
        "strategy_version": "1.0",
        "dataset_hash": file_hash(args.tasks),
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "code_state": git,
        "seed": args.seed,
        "repeats": args.repeats,
        "max_calls": args.max_calls,
        "timeout_seconds": args.timeout_seconds,
        "research_instructions": prompt,
        "warning": "Mock plan only; no real model calls. Quality evaluation is independent of Agent self-review.",
        "frozen_config": {
            "temperature": 0,
            "tool_mode": "json",
            "search_adapter": "wikipedia",
            "internal_call_upper_bound": args.max_calls,
            "note": "Both architectures share the same frozen model/config/temperature/tools/call limit.",
        },
        "search_cache_schema": {
            "description": "Pre-frozen retrieval: query → snippets, URL, retrieved_at, hash. Both architectures read the same cache.",
            "miss_policy": "Symmetric: both architectures use cache; if cache miss, both record miss (no side goes online).",
            "fields": ["query", "url", "title", "excerpt", "retrieved_at", "sha256", "retrieved"],
        },
        "quality_rubric": {
            "dimensions": ["factual_accuracy", "citation_support", "completeness", "reasoning_quality", "limitation_awareness"],
            "scale": "1-5 per dimension",
            "scorer": "Human independent review; NOT Agent self-evaluation.",
            "note": "Report quality scores only after human review. Unreviewed items marked as 'not_reviewed'.",
        },
        "units": build_plan(tasks, args.repeats, args.seed),
    }
    plan["total_units"] = len(plan["units"])
    plan["estimated_duration_minutes"] = round(
        plan["total_units"] * args.timeout_seconds / 60 / 2,  # parallel 2
        1,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Experiment plan: {plan['total_units']} units")
    print(f"  dataset_hash: {plan['dataset_hash'][:16]}...")
    print(f"  prompt_hash:  {plan['prompt_hash'][:16]}...")
    print(f"  code HEAD:    {git['head'][:12]} (dirty={git['dirty']})")
    print(f"  seed:         {args.seed}")
    print(f"  output:       {args.output}")


if __name__ == "__main__":
    main()
