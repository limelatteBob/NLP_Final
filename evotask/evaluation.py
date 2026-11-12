from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from .config import Config
from .engine import Engine
from .providers import make_provider
from .retrieval import KnowledgeBase
from .sandbox import DockerSandbox
from .storage import Store, digest, uid


def external_checks(store: Store, run: dict, checks: list[dict], config: Config | None = None) -> list[dict]:
    final_ids = {identifier for task in run["tasks"] for identifier in task["artifact_ids"]}
    outputs = [artifact for artifact in run["artifacts"] if artifact["id"] in final_ids]
    results = []
    for check in checks:
        candidates = [a for a in outputs if a["path"] == check["path"]]
        ok = bool(candidates)
        if ok and check["kind"] == "contains":
            ok = any(check["text"] in store.artifact_bytes(a).decode("utf-8") for a in candidates)
        elif check["kind"] == "python":
            details = {"error": "No submitted source artifact"}
            if ok:
                if not config or not check["path"].endswith(".py"):
                    raise ValueError("Python benchmark requires config and a .py artifact")
                workspace = store.root / "benchmark" / uid("check")
                workspace.mkdir(parents=True)
                (workspace / "solution.py").write_bytes(store.artifact_bytes(candidates[-1]))
                (workspace / "acceptance.py").write_text("import solution\n" + check["assertions"], encoding="utf-8")
                try:
                    details = DockerSandbox(config).execute(workspace, "acceptance.py")
                    ok = details["success"]
                except (ValueError, RuntimeError, OSError) as exc:
                    details, ok = {"error": str(exc)}, False
            results.append({"check": check, "passed": ok, "execution": details})
            continue
        elif check["kind"] not in {"exists", "contains"}:
            raise ValueError("External suite checks support exists, contains and python")
        results.append({"check": check, "passed": ok})
    return results


def evaluate(root: Path, suite: Path, config: Config, progress=print) -> Path:
    cases = [json.loads(line) for line in suite.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases:
        raise ValueError("Evaluation suite is empty")
    batch = root.resolve() / "evaluations" / uid("batch")
    batch.mkdir(parents=True)
    rows = []
    for index, case in enumerate(cases):
        store = Store(batch / f"case_{index:03d}")
        started = time.monotonic()
        try:
            with store.lock():
                for source in case.get("knowledge", []):
                    KnowledgeBase(store).add((suite.parent / source).resolve())
                engine = Engine(store, config, make_provider(config))
                run = engine.new(case["goal"], [(suite.parent / p).resolve() for p in case.get("inputs", [])],
                                 memory=case.get("memory", False))
                engine.advance(run)
                checks = external_checks(store, run, case.get("checks", []), config)
                expected = case.get("expected_status", "succeeded")
                behavior = run["status"] == expected and all(c["passed"] for c in checks)
                success = run["status"] == "succeeded" and all(c["passed"] for c in checks)
                rows.append({"id": case["id"], "run_id": run["id"], "status": run["status"],
                             "task_success": success, "expected_behavior_pass": behavior, "checks": checks,
                             "metrics": engine.metrics(run), "seconds": time.monotonic() - started})
        except Exception as exc:
            rows.append({"id": case["id"], "status": "failed", "task_success": False,
                         "expected_behavior_pass": False, "error": str(exc), "seconds": time.monotonic() - started})
        finally:
            store.close()
        progress(f"{case['id']}: {rows[-1]['status']} / external_pass={rows[-1]['expected_behavior_pass']}")
    times = sorted(row["seconds"] for row in rows)
    successes = [row["seconds"] for row in rows if row["task_success"]]
    summary = {"mode": config.provider, "cases": len(rows),
               "task_completion_rate": sum(r["task_success"] for r in rows) / len(rows),
               "expected_behavior_pass_rate": sum(r["expected_behavior_pass"] for r in rows) / len(rows),
               "all_case_median_seconds": statistics.median(times),
               "all_case_p95_seconds": times[max(0, math.ceil(0.95 * len(times)) - 1)],
               "successful_case_median_seconds": statistics.median(successes) if successes else None,
               "suite_sha256": digest(suite.read_bytes()), "config_hash": config.fingerprint(),
               "memory_policy": "isolated fresh state for each case; no cross-case learning"}
    (batch / "results.json").write_text(json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# EvoTask 评测报告", "", "DEMO 结果只验证软件流程，不代表真实 LLM 性能。" if config.provider == "demo"
             else "真实 API 模式；外部检查仍只覆盖测试集声明的条件。", "",
             "所有失败、异常和等待人工输入的任务保留在分母。HITL 预期行为通过不等于任务已完成。", "",
             "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", "",
             "| 用例 | 状态 | 任务完成 | 外部预期行为通过 | 耗时（秒） |", "|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['id']} | {row['status']} | {row['task_success']} | {row['expected_behavior_pass']} | {row['seconds']:.3f} |")
    path = batch / "performance.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
