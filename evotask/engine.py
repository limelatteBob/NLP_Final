from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .config import Config
from .collaboration import ACTION_HELP, ancestors, descendants, normalize_action, urls_in
from .providers import ProviderUnavailable
from .retrieval import recall
from .storage import AGENTS, Store, atomic_json, digest, safe_path, uid
from .tools import CAPABILITIES, Tools
from .outputs import validate_outputs


TERMINAL = {"succeeded", "failed", "cancelled"}
CITATION = re.compile(r"\[(doc_[a-f0-9]{12}:[a-f0-9]{8}:\d+)\]")


class RunCancelled(Exception):
    """Cooperative cancellation at model/tool boundaries."""


def validate_checks(checks: object) -> list[dict]:
    if not isinstance(checks, list) or not 1 <= len(checks) <= 8:
        raise ValueError("Every task needs 1–8 acceptance checks")
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in {"exists", "contains", "python", "human", "sources"}:
            raise ValueError("Invalid acceptance check")
        if check["kind"] == "human":
            if not isinstance(check.get("reason"), str) or not check["reason"].strip():
                raise ValueError("Human check requires a reason")
        else:
            safe_path(Path.cwd(), check.get("path", ""))
            if check["path"].replace("\\", "/").startswith("inputs/"):
                raise ValueError("Acceptance checks must target this task's outputs, not inherited inputs")
            if check["kind"] == "contains" and (not isinstance(check.get("text"), str) or not check["text"]):
                raise ValueError("Contains check needs nonempty text")
            if check["kind"] == "python" and not check["path"].endswith(".py"):
                raise ValueError("Python acceptance check must target a .py file")
            if check['kind'] == 'sources' and check.get('policy', 'any') not in {'any', 'url', 'local'}:
                raise ValueError('Source policy must be any, url or local')
    return checks


def task_from_spec(spec: dict, task_id: str | None = None, parent: str | None = None, depth: int = 0) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Task must be an object")
    identifier = task_id or spec.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", identifier):
        raise ValueError("Invalid task ID")
    if spec.get("role") not in AGENTS:
        raise ValueError("Unknown role template")
    for key in ("title", "objective"):
        if not isinstance(spec.get(key), str) or not 1 <= len(spec[key]) <= 4000:
            raise ValueError(f"Task {key} is missing or too long")
    required = list(spec.get("required_tools", [])) if isinstance(spec.get("required_tools", []), list) else None
    if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
        raise ValueError("required_tools must be a list of names")
    checks = validate_checks(spec.get("checks"))
    if any(check["kind"] == "python" for check in checks) and "python_run" not in required:
        required.append("python_run")
    if not any(set(required) <= tools for tools in CAPABILITIES.values()):
        raise ValueError("No single role has the required tools; split the task")
    dependencies = spec.get("depends_on", [])
    if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
        raise ValueError("depends_on must be a list of task IDs")
    return {"id": identifier, "title": spec["title"], "objective": spec["objective"], "role": spec["role"],
            "required_tools": required, "depends_on": dependencies, "checks": checks,
            "parent": parent, "depth": depth, "owner": None, "status": "pending", "history": [],
            "steps": 0, "error_streak": 0, "inputs": [], "children": [], "artifact_ids": [],
            "memory_hits": [], "bids": [], "summary": "", "allowed_citations": [],
            "outputs": validate_outputs(spec.get("outputs"))}


def validate_plan(specs: object, limit: int) -> list[dict]:
    if not isinstance(specs, list) or not 1 <= len(specs) <= limit:
        raise ValueError("Plan must contain a bounded, nonempty task list")
    tasks = [task_from_spec(spec) for spec in specs]
    ids = {task["id"] for task in tasks}
    if len(ids) != len(tasks):
        raise ValueError("Duplicate task IDs")
    remaining = {task["id"]: set(task["depends_on"]) for task in tasks}
    if any(not deps <= ids for deps in remaining.values()):
        raise ValueError("Unknown task dependency")
    completed = set()
    while remaining:
        ready = {identifier for identifier, deps in remaining.items() if deps <= completed}
        if not ready:
            raise ValueError("Task dependency graph contains a cycle")
        completed |= ready
        remaining = {identifier: deps for identifier, deps in remaining.items() if identifier not in ready}
    return tasks


class Engine:
    def __init__(self, store: Store, config: Config, provider, sandbox=None, progress=None, cancelled=None):
        self.store, self.config, self.provider = store, config, provider
        self.tools = Tools(store, config, sandbox)
        self.progress = progress or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise RunCancelled("Stopped by the user")

    def cancel(self, run: dict) -> dict:
        if run["status"] in TERMINAL:
            return run
        for task in run["tasks"]:
            if task["status"] not in TERMINAL:
                if task["owner"]:
                    self.store.snapshot(run, task)
                task["status"], task["summary"] = "cancelled", "Stopped by the user; partial outputs archived"
        if run["pending"]:
            run["human_wait_seconds"] += time.time() - run["pending"]["created"]
        run["pending"], run["status"] = None, "cancelled"
        self.emit(run, "human", "run_cancelled", {"reason": "Stopped by the user"})
        self.finalize(run)
        self.store.save(run)
        self.write_report(run)
        return run

    def emit(self, run: dict, agent: str, kind: str, data: dict) -> None:
        self.store.event(run["id"], agent, kind, data)
        if kind in {"task_assigned", "delegated", "task_succeeded", "attempt_failed", "waiting_human", "run_finished"}:
            self.progress(f"[{agent}] {kind}: {json.dumps(data, ensure_ascii=False)[:500]}")

    def new(self, goal: str, inputs: list[Path] | None = None, memory: bool = True, require_review: bool = False,
            *, input_names: list[str] | None = None, knowledge_base_ids: list[str] | None = None,
            persist: bool = True, run_id: str | None = None) -> dict:
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 12000:
            raise ValueError("Goal must contain 1–12000 characters")
        self.tools.kb.validate_bases(knowledge_base_ids)
        run = {"id": run_id or uid("run"), "goal": goal, "status": "planning", "created": time.time(), "ended": None,
               "config": self.config.snapshot(), "config_hash": self.config.fingerprint(),
               "mode": "demo" if self.config.provider == "demo" else "live", "tasks": [], "artifacts": [],
               "pending": None, "inflight": None, "steps": 0, "model_calls": 0, "active_seconds": 0.0,
               "human_wait_seconds": 0.0, "clarifications": [], "memory_enabled": memory,
               "initial_memory_hashes": {a: digest(json.dumps(self.store.memory(a), sort_keys=True).encode()) for a in AGENTS},
               "initial_inputs": [], "failure": None, "finish_recorded": False, "require_review": require_review,
               "knowledge_base_ids": knowledge_base_ids, "revision_count": 0, "revisions": [], "artifact_schema": 2}
        for index, source in enumerate(inputs or []):
            if source.suffix.lower() not in {".md", ".txt"} or not source.is_file():
                raise ValueError("Task inputs must be existing .md/.txt files")
            name = input_names[index] if input_names is not None else source.name
            if '/' in name or '\\' in name:
                raise ValueError('Input names must not contain paths')
            relative = f"inputs/user/{index}_{name}"
            for agent in AGENTS:
                target = safe_path(self.store.workspace(agent, run["id"]), relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            run["initial_inputs"].append(relative)
            run.setdefault("input_manifest", []).append({"path": relative, "name": name})
        if persist:
            self.store.message("group", "human", goal)
            self.store.save(run)
            self.emit(run, "coordinator", "run_created", {"mode": run["mode"], "config_hash": run["config_hash"]})
        return run

    def model(self, run: dict, purpose: str, context: dict) -> dict:
        self.check_cancelled()
        if run["model_calls"] >= self.config.max_model_calls:
            raise RuntimeError("Model call budget exhausted")
        run["model_calls"] += 1
        self.store.save(run)  # Count even failed/ambiguous network calls.
        self.emit(run, context.get("agent", "coordinator"), "model_request", {"purpose": purpose, "number": run["model_calls"]})
        result = self.provider.complete(purpose, context)
        self.check_cancelled()
        if not isinstance(result, dict):
            raise ValueError("Provider must return a JSON object")
        return result

    def collaboration_context(self, run: dict, task: dict) -> dict:
        upstream = ancestors(run['tasks'], task['id'])
        downstream = descendants(run['tasks'], task['id'])
        return {'upstream': [{k: t[k] for k in ('id','title','objective','checks','status','summary')}
                             for t in run['tasks'] if t['id'] in upstream],
                'downstream_requirements': [{k: t[k] for k in ('id','role','objective','checks')}
                                           for t in run['tasks'] if t['id'] in downstream],
                'revision_feedback': task.get('revision_feedback'),
                'source_evidence': task.get('source_evidence', []),
                'frozen_tests': task.get('revision_tests', [])}

    def record_sources(self, run: dict, task: dict, name: str, args: dict, result: dict) -> None:
        sources = []
        if name == 'web_search':
            sources = [{'url': s['url'], 'title': s.get('title', ''), 'kind': 'search_result'} for s in result.get('results', [])]
        elif name == 'fetch_url' and result.get('url'):
            sources = [{'url': result['url'], 'kind': 'fetched_page'}]
        elif name == 'retrieve_documents':
            sources = [{'url': p['source_url'], 'title': p['source'], 'kind': 'user_provided', 'citation': p['citation']}
                       for p in result.get('passages', []) if p.get('source_url')]
        elif name == 'read_file' and args.get('path') in run['initial_inputs']:
            # Reading an agent's own invented link must not turn it into observed evidence.
            sources = [{'url': url, 'kind': 'user_input', 'path': args['path']} for url in urls_in(result.get('content', ''))]
        evidence = task.setdefault('source_evidence', [])
        evidence.extend(s for s in sources if s not in evidence)

    def refresh_inputs(self, run: dict, task: dict) -> None:
        """Deliver authorized ancestor evidence and refresh revision aliases with an audit trail."""
        source_tasks = ancestors(run['tasks'], task['id'])
        source_ids = {identifier for t in run['tasks'] if t['id'] in source_tasks for identifier in t['artifact_ids']}
        source_ids.update(task.get('handoff_ids', []))
        bindings = task.setdefault('input_bindings', [])
        # Older runs have only immutable artifact paths; reconstruct their bindings.
        for artifact in run['artifacts']:
            path = f"inputs/{artifact['task_id']}/{artifact['id']}_{Path(artifact['path']).name}"
            if path in task['inputs'] and not any(b['path'] == path for b in bindings):
                bindings.append({'path': path, 'task_id': artifact['task_id'], 'artifact_path': artifact['path'], 'artifact_id': artifact['id']})
        for artifact in run['artifacts']:
            if artifact['id'] not in source_ids:
                continue
            previous = [b for b in bindings if b['task_id'] == artifact['task_id'] and b['artifact_path'] == artifact['path']]
            if previous:
                for binding in previous:
                    if binding['artifact_id'] != artifact['id']:
                        target = safe_path(self.store.workspace(task['owner'], run['id']), binding['path'])
                        target.write_bytes(self.store.artifact_bytes(artifact))
                        self.emit(run, task['owner'], 'artifact_refreshed', {'task_id': task['id'], 'path': binding['path'],
                            'previous_artifact': binding['artifact_id'], 'artifact_id': artifact['id'], 'sha256': artifact['sha256']})
                        binding['artifact_id'] = artifact['id']
            else:
                path = self.store.handoff(run, task['owner'], artifact)
                task['inputs'].append(path)
                bindings.append({'path': path, 'task_id': artifact['task_id'], 'artifact_path': artifact['path'], 'artifact_id': artifact['id']})
            task['allowed_citations'].extend(c for c in artifact.get('citations', []) if c not in task['allowed_citations'])
            evidence = task.setdefault('source_evidence', [])
            evidence.extend(e for e in artifact.get('source_evidence', []) if e not in evidence)
            source_task = next(t for t in run['tasks'] if t['id'] == artifact['task_id'])
            if 'source_evidence' not in source_task:
                for event in source_task['history']:
                    action, result = event.get('action', {}), event.get('result', {})
                    self.record_sources(run, source_task, action.get('name'), action.get('args', {}), result)
            evidence.extend(e for e in source_task.get('source_evidence', []) if e not in evidence)

    def request_revision(self, run: dict, verifier: dict, reason: str, target_id: str | None = None) -> dict:
        """Reopen the author and descendants; retain contracts, frozen tests and all snapshots."""
        if verifier['role'] != 'verifier' or not verifier['owner']:
            raise ValueError('Only an assigned verifier can request an upstream revision')
        if run.get('revision_count', 0) >= 2:
            raise ValueError('Revision budget exhausted (two rounds per run)')
        if run['steps'] >= self.config.max_steps or run['model_calls'] >= self.config.max_model_calls or run['active_seconds'] >= self.config.active_timeout:
            raise ValueError('Run budget exhausted; start a new task with the saved evidence')
        candidates = [t for t in run['tasks'] if t['id'] in verifier['depends_on'] and t['status'] == 'succeeded' and t['role'] != 'verifier']
        target = next((t for t in candidates if t['id'] == target_id), None) if target_id else (candidates[0] if len(candidates) == 1 else None)
        if not target or not reason.strip():
            raise ValueError('Revision needs a specific successful direct dependency and an observed failure')
        affected = descendants(run['tasks'], target['id']) | {target['id']}
        if any(t['children'] for t in run['tasks'] if t['id'] in affected):
            raise ValueError('This revision spans delegated children; start a new task with archived outputs')
        # Pin executable checks so the verifier cannot make a failed assertion disappear.
        paths = {c['path'] for c in verifier['checks'] if c['kind'] == 'python'}
        paths.update(h.get('action', {}).get('args', {}).get('path') for h in verifier['history']
                     if h.get('action', {}).get('name') == 'python_run')
        frozen = verifier.setdefault('revision_tests', [])
        for path in sorted(p for p in paths if isinstance(p, str)):
            source = self.tools._file(self.store.workspace(verifier['owner'], run['id']), path)
            if source.is_file() and not any(f['path'] == path for f in frozen):
                artifact = self.store.archive(run, verifier['owner'], path, verifier['id'])
                frozen.append({'path': path, 'artifact_id': artifact['id'], 'sha256': artifact['sha256']})
        number = run.get('revision_count', 0) + 1
        feedback = {'round': number, 'from_task': verifier['id'], 'target_task': target['id'], 'reason': reason[:4000],
                    'verifier_objective': verifier['objective'], 'checks': verifier['checks'], 'frozen_tests': frozen[:], 'created': time.time()}
        run.setdefault('revisions', []).append(feedback)
        run['revision_count'] = number
        for task in run['tasks']:
            if task['id'] not in affected:
                continue
            for artifact in run['artifacts']:
                if artifact['id'] in task['artifact_ids']:
                    artifact['verification'] = 'superseded'
            task.update(status='pending', artifact_ids=[], error_streak=0, steps=0, revision_feedback=feedback)
            task['history'].append({'event': 'revision_requested', 'feedback': feedback})
            if task['owner']:
                memories = self.store.memory(task['owner'])
                for entry in memories:
                    if entry.get('run_id') == run['id'] and entry.get('kind') == 'success' and task['id'] in entry.get('id', '').split(':'):
                        entry.update(verified=False, superseded=True)
                atomic_json(self.store.root / 'agents' / task['owner'] / 'memory.json', memories)
        run.update(status='running', pending=None, failure=None, ended=None, finish_recorded=False)
        self.emit(run, verifier['owner'], 'revision_requested', feedback)
        self.store.message('group', verifier['owner'], f"第 {number} 轮返修：退回 {target['id']}。{reason[:500]}；保留原验收要求并重新验证。")
        self.store.save(run)
        return feedback

    def retry_verification(self, run: dict) -> dict:
        if run['status'] != 'failed':
            raise ValueError('Only a failed run can be reopened for revision')
        verifier = next((t for t in run['tasks'] if t['role'] == 'verifier' and t['status'] == 'failed'
                         and any(h.get('result', {}).get('observed_error') for h in t['history'])), None)
        if not verifier:
            raise ValueError('No failed verifier with recorded evidence is available')
        reason = next(h['result']['observed_error'] for h in reversed(verifier['history']) if h.get('result', {}).get('observed_error'))
        return self.request_revision(run, verifier, reason)

    def retryable_action_tasks(self, run: dict) -> list[dict]:
        """Only reopen format failures that this controller can now interpret safely."""
        if (run['status'] != 'failed' or run['pending'] or run['inflight'] or run.get('failure')
                or run.get('action_retry_count', 0) >= 2
                or run['steps'] >= self.config.max_steps
                or run['model_calls'] >= self.config.max_model_calls
                or run['active_seconds'] >= self.config.active_timeout):
            return []
        candidates = []
        for task in run['tasks']:
            if (task['status'] != 'failed' or not task['owner'] or not task['history']
                    or task['steps'] >= self.config.max_task_steps):
                continue
            last = task['history'][-1]
            result = last.get('result', {})
            if not (result.get('observed_error') == 'Invalid agent action type'
                    or result.get('error_type') == 'ActionProtocolError'):
                continue
            try:
                action = normalize_action(last.get('action'))
                if action['type'] != 'tool' or action == last.get('action'):
                    continue
                self.tools.validate(task['owner'], action['name'], action['args'])
            except (ValueError, PermissionError, TypeError):
                continue
            candidates.append(task)
        return candidates

    def retry_actions(self, run: dict) -> None:
        if Config(**run['config']) != self.config:
            raise ValueError('Resume requires the original configuration')
        tasks = self.retryable_action_tasks(run)
        if not tasks:
            raise ValueError('No recoverable action format failure within the remaining budget')
        number = run.get('action_retry_count', 0) + 1
        affected = set()
        for task in tasks:
            affected.update(descendants(run['tasks'], task['id']))
            task.update(status='running', error_streak=0, summary='')
            task['history'].append({'event': 'action_retry_requested', 'round': number,
                'guidance': 'The controller now supports the previous tool format. Inspect workspace files '
                            'and resume from observed results; no old action has been replayed. ' + ACTION_HELP})
        for task in run['tasks']:
            if (task['id'] in affected and task['status'] == 'failed'
                    and task['summary'] == 'Required dependency did not succeed'):
                task.update(status='pending', summary='', error_streak=0)
        run.update(status='running', ended=None, finish_recorded=False, action_retry_count=number)
        self.emit(run, 'human', 'action_retry_requested', {'task_ids': [t['id'] for t in tasks],
            'round': number, 'message': '继续格式错误处的任务，保留上游结果、历史和原预算。'})
        self.store.save(run)

    def wait(self, run: dict, task: dict | None, kind: str, question: str, action: dict | None = None,
             prepared: dict | None = None) -> None:
        pending = {"id": uid("request"), "task_id": task["id"] if task else None, "kind": kind,
                   "question": question, "action": action, "prepared": prepared, "created": time.time()}
        run["pending"], run["status"] = pending, "waiting_human"
        if task:
            task["status"] = "waiting_human"
        self.store.save(run)
        self.emit(run, task["owner"] if task else "coordinator", "waiting_human",
                  {key: pending[key] for key in ("id", "kind", "question", "task_id")})

    def assign(self, run: dict, task: dict) -> None:
        candidates = [agent for agent in AGENTS if set(task["required_tools"]) <= CAPABILITIES[agent]]
        if task["role"] == "verifier":
            upstream_owners = {t["owner"] for t in run["tasks"] if t["id"] in task["depends_on"]}
            candidates = [agent for agent in candidates if agent not in upstream_owners]
        # A different agent must perform delegated work; same-owner re-entry is not collaboration.
        if task["parent"]:
            parent = next(t for t in run["tasks"] if t["id"] == task["parent"])
            candidates = [a for a in candidates if a != parent["owner"]]
        task['bids'] = []  # Never reuse an acceptance from an earlier failed assignment.
        for agent in candidates:
            bid = self.model(run, "bid", {"agent": agent, "task": task, "tools": self.tools.specs(agent)})
            if type(bid.get("accept")) is not bool or not isinstance(bid.get("reason"), str):
                raise ValueError("Malformed capability bid")
            confidence = bid.get("confidence", 0)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError("Bid confidence must be between 0 and 1")
            task["bids"].append({"agent": agent, **bid})
            self.emit(run, agent, "capability_bid", {"task_id": task["id"], **bid})
        accepted = [bid for bid in task["bids"] if bid["accept"]]
        if not accepted:
            raise RuntimeError("No capable agent accepted this task")
        task["owner"] = max(accepted, key=lambda bid: (bid["confidence"], bid["agent"] == task["role"]))["agent"]
        task["status"] = "running"
        task["inputs"] = list(run["initial_inputs"])
        self.refresh_inputs(run, task)
        if run["memory_enabled"]:
            task["memory_hits"] = recall(self.store, task["owner"], task["objective"])
        self.emit(run, task["owner"], "memory_recalled", {"task_id": task["id"], "ids": [m["id"] for m in task["memory_hits"]]})
        self.emit(run, task["owner"], "task_assigned", {"task_id": task["id"], "title": task["title"], "objective": task["objective"]})
        self.store.message("group", task["owner"], f"接受任务 {task['id']}: {task['title']}")
        self.store.save(run)

    def fail_attempt(self, run: dict, task: dict, action: dict, exc: Exception, terminal: bool = False) -> None:
        message = str(exc)[:2000]
        task["error_streak"] += 1
        snapshot = self.store.snapshot(run, task) if task["owner"] else []
        record = {"error_type": type(exc).__name__, "observed_error": message,
                  "cause_hypothesis": None, "artifact_ids": [a["id"] for a in snapshot]}
        task["history"].append({"action": action, "result": {"success": False, **record}})
        self.emit(run, task["owner"] or "coordinator", "attempt_failed", {"task_id": task["id"], **record})
        self.store.message("group", task["owner"] or "coordinator", f"任务 {task['id']} 尝试失败：{message[:300]}；已有产物已归档。")
        if terminal or task["error_streak"] >= 3:
            task["status"], task["summary"] = "failed", message
        self.store.save(run)

    def delegate(self, run: dict, task: dict, action: dict) -> dict:
        if len(run["tasks"]) >= self.config.max_tasks or task["depth"] >= self.config.max_delegate_depth:
            raise ValueError("Delegation task/depth budget exceeded")
        child = task_from_spec(action, uid("sub"), task["id"], task["depth"] + 1)
        child["depends_on"] = []
        paths = action.get("inputs", [])
        if not isinstance(paths, list) or len(paths) > 10:
            raise ValueError("Delegate inputs must be a bounded list of local files")
        child["handoff_ids"] = []
        for path in paths:
            artifact = self.store.archive(run, task["owner"], path, task["id"])
            self.check_citations(task, artifact)
            child["handoff_ids"].append(artifact["id"])
        run["tasks"].append(child)
        task["children"].append(child["id"])
        task["status"] = "waiting_child"
        self.emit(run, task["owner"], "delegated", {"from_task": task["id"], "to_task": child["id"], "role": child["role"], "title": child["title"]})
        return {"child_task": child["id"], "status": "waiting_child"}

    def check_citations(self, task: dict, artifact: dict) -> None:
        citations = set(CITATION.findall(self.store.artifact_bytes(artifact).decode("utf-8")))
        if not citations <= set(task.get("allowed_citations", [])):
            raise ValueError("Artifact cites passages not retrieved or received through an authorized handoff")
        artifact["citations"] = sorted(citations)
        artifact['source_evidence'] = task.get('source_evidence', [])[:]

    def check_frozen_tests(self, run: dict, task: dict) -> None:
        for frozen in task.get('revision_tests', []):
            path = self.tools._file(self.store.workspace(task['owner'], run['id']), frozen['path'])
            if not path.is_file() or digest(path.read_bytes()) != frozen['sha256']:
                raise ValueError('Frozen verification script changed; original assertions must be retained')

    def finish(self, run: dict, task: dict, action: dict, approved: bool = False, frozen: list[dict] | None = None) -> None:
        paths = action.get("artifacts")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 15:
            raise ValueError("finish must export 1–15 artifact paths")
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate artifact path")
        root = self.store.workspace(task["owner"], run["id"])
        for path in paths:
            self.tools._file(root, path, writing=True)
        required_outputs = {o['path'] for o in task.get('outputs', []) if o['kind'] == 'deliverable'}
        if not required_outputs <= set(paths):
            raise ValueError('finish must export every declared deliverable: ' + ', '.join(sorted(required_outputs - set(paths))))
        if frozen:
            artifacts = []
            for approved_artifact in frozen:
                stored = next(a for a in run["artifacts"] if a["id"] == approved_artifact["id"])
                if stored["sha256"] != approved_artifact["sha256"]:
                    raise ValueError("Approved artifact manifest changed")
                artifacts.append(stored)
        else:
            artifacts = [self.store.archive(run, task["owner"], p, task["id"], origin="finish") for p in paths]
        outcomes = []
        try:
            self.check_frozen_tests(run, task)
            for artifact in artifacts:
                self.check_citations(task, artifact)
            for check in sorted(task["checks"], key=lambda c: c['kind'] == 'human'):
                kind = check["kind"]
                if kind == "human":
                    if not approved:
                        reasons = "\n".join(c["reason"] for c in task["checks"] if c["kind"] == "human")
                        self.wait(run, task, "acceptance", reasons, action, {"artifacts": artifacts})
                        return
                    outcomes.append({"kind": "human", "passed": True})
                    continue
                path = self.tools._file(root, check["path"])
                if not path.is_file():
                    raise ValueError(f"Acceptance failed: missing {check['path']}")
                if path.stat().st_size > self.config.max_file_bytes:
                    raise ValueError("Acceptance target exceeds file limit")
                if kind == "contains" and check["text"] not in path.read_text(encoding="utf-8"):
                    raise ValueError(f"Acceptance failed: {check['path']} missing required text")
                if kind == 'sources':
                    content = path.read_text(encoding='utf-8')
                    local = set(CITATION.findall(content)) & set(task.get('allowed_citations', []))
                    observed = {e['url'] for e in task.get('source_evidence', []) if e.get('url')}
                    cited_urls = set(urls_in(content)) & observed
                    policy = check.get('policy', 'any')
                    if not (cited_urls if policy == 'url' else local if policy == 'local' else cited_urls or local):
                        raise ValueError(f"Acceptance failed: {check['path']} needs observed sources ({policy}); do not invent links")
                if kind == "python":
                    self.tools.validate(task["owner"], "python_run", {"path": check["path"]})
                    run["inflight"] = {"task_id": task["id"], "action": {"type": "verification", "check": check}, "started": time.time()}
                    self.store.save(run)
                    result = self.tools.sandbox.execute(root, check["path"])
                    run["inflight"] = None
                    task['last_verification'] = {'step': task['steps'], 'path': check['path'], 'result': result}
                    self.check_frozen_tests(run, task)
                    outcomes.append({"kind": kind, "path": check["path"], "result": result})
                    if not result["success"]:
                        raise ValueError("Independent Python check failed: " + result["output"][:2000])
                else:
                    outcomes.append({"kind": kind, "path": check["path"], "passed": True})
            # The version exported/approved must still match after any tests ran.
            for artifact in artifacts:
                current = safe_path(root, artifact["path"])
                if digest(current.read_bytes()) != artifact["sha256"]:
                    raise ValueError("Artifact changed during verification/approval; submit a new snapshot")
        except Exception:
            for artifact in artifacts:
                artifact["verification"] = "invalid"
            raise
        for artifact in artifacts:
            artifact["verification"] = "verified"
            artifact["verification_scope"] = "declared_checks_only"
        task["artifact_ids"] = [a["id"] for a in artifacts]
        task["status"] = "succeeded"
        task["summary"] = str(action.get("summary", ""))[:3000]
        task["history"].append({"action": action, "result": {"checks": outcomes, "success": True}})
        self.emit(run, task["owner"], "task_succeeded", {"task_id": task["id"], "artifacts": task["artifact_ids"], "checks": outcomes, "summary": task["summary"]})
        self.store.message("group", task["owner"], f"任务 {task['id']} 通过声明的检查：{task['summary']}")
        if run["memory_enabled"]:
            self.store.remember(task["owner"], {"id": f"{run['id']}:{task['id']}:success:{run.get('revision_count', 0)}", "run_id": run["id"],
                "trigger": task["objective"], "lesson": "此前类似任务通过以下检查；仍需在新任务重验：" + json.dumps(task["checks"], ensure_ascii=False),
                "verified": True, "evidence": {"artifacts": task["artifact_ids"], "checks": outcomes}, "kind": "success"})
        self.store.save(run)

    def normalize_decision(self, run: dict, task: dict, action: dict) -> dict:
        normalized = normalize_action(action)
        if normalized != action:
            self.emit(run, task['owner'], 'action_normalized', {'task_id': task['id'],
                'original_type': action.get('type'), 'original_keys': sorted(action),
                'type': normalized['type'], 'name': normalized.get('name')})
        return normalized

    def action(self, run: dict, task: dict, action: dict, approved: bool = False, prepared: dict | None = None) -> None:
        action = self.normalize_decision(run, task, action)
        kind = action["type"]
        if kind == 'request_revision':
            self.request_revision(run, task, str(action.get('reason', '')), action.get('target_task'))
            return
        if kind == "ask":
            question = action.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("Clarification requires a question")
            self.wait(run, task, "clarification", question[:4000], action)
            return
        if kind == "finish":
            self.finish(run, task, action, approved, prepared.get("artifacts") if prepared else None)
            return
        if kind == "fail":
            raise RuntimeError("Agent reported inability to finish: " + str(action.get("reason", "")))
        if kind == "delegate":
            result = self.delegate(run, task, action)
        else:
            name, args = action.get("name"), action.get("args", {})
            if not approved:
                prepared = self.tools.prepare(run, task, name, args)
                if prepared:
                    self.wait(run, task, "tool_approval", f"批准 {name}？具体版本和目标见 prepared。", action, prepared)
                    return
            run["inflight"] = {"task_id": task["id"], "action": action, "started": time.time()}
            self.store.save(run)  # Never silently replay a potentially side-effecting operation after a crash.
            if name == 'python_run':
                self.check_frozen_tests(run, task)
            result = self.tools.execute(run, task, name, args, prepared)
            run["inflight"] = None
            if name == 'python_run':
                task['last_verification'] = {'step': task['steps'], 'path': args['path'], 'result': result}
                self.check_frozen_tests(run, task)
            if result.get("success") is False:
                raise RuntimeError(json.dumps(result, ensure_ascii=False))
            if name == "retrieve_documents":
                task["allowed_citations"].extend(p["citation"] for p in result["passages"])
            self.record_sources(run, task, name, args, result)
            self.emit(run, task["owner"], "tool_result", {"task_id": task["id"], "name": name, "result": result})
        task["history"].append({"action": action, "result": result})
        task["error_streak"] = 0
        self.store.save(run)

    def resolve_children(self, run: dict) -> None:
        by_id = {task["id"]: task for task in run["tasks"]}
        for task in run["tasks"]:
            if task["status"] == "waiting_child":
                children = [by_id[c] for c in task["children"]]
                if all(child["status"] in TERMINAL for child in children):
                    # Failed delegated tasks remain in the denominator; the parent can still repair its own work.
                    for child in children:
                        if child["id"] in task.get("received_children", []):
                            continue
                        for artifact in run["artifacts"]:
                            if artifact["id"] in child["artifact_ids"]:
                                task["inputs"].append(self.store.handoff(run, task["owner"], artifact))
                                task["allowed_citations"].extend(artifact.get("citations", []))
                                task.setdefault('source_evidence', []).extend(artifact.get('source_evidence', []))
                        task["history"].append({"event": "child_completed", "task_id": child["id"],
                                                "status": child["status"], "summary": child["summary"]})
                        task.setdefault("received_children", []).append(child["id"])
                    task["status"] = "running"

    def advance(self, run: dict) -> dict:
        if Config(**run["config"]) != self.config:
            raise ValueError("Configuration changed; resume with the original configuration")
        if run["status"] in TERMINAL or run["pending"]:
            self.write_report(run)
            return run
        start = time.monotonic()
        try:
            self.check_cancelled()
            if run["inflight"]:
                old = run["inflight"]
                task = next(t for t in run["tasks"] if t["id"] == old["task_id"])
                run["inflight"] = None
                self.fail_attempt(run, task, old["action"], RuntimeError(
                    "Previous tool execution interrupted; outcome uncertain. Not replayed; inspect archived outputs and remote side effects."), terminal=True)
            if run["status"] == "planning":
                planning_memory = recall(self.store, "coordinator", run["goal"]) if run["memory_enabled"] else []
                plan = self.model(run, "plan", {"goal": run["goal"], "clarifications": run["clarifications"],
                    "inputs": run["initial_inputs"], "memory": planning_memory,
                    'knowledge_bases': [b for b in self.tools.kb.bases() if run.get('knowledge_base_ids') is None or b['id'] in run['knowledge_base_ids']],
                    "capabilities": {a: self.tools.specs(a) for a in AGENTS}})
                self.emit(run, "coordinator", "memory_recalled", {"ids": [m["id"] for m in planning_memory]})
                if plan.get("question"):
                    self.wait(run, None, "planning", str(plan["question"])[:4000])
                    return run
                run["tasks"] = validate_plan(plan.get("tasks"), self.config.max_tasks)
                if run.get("require_review"):
                    # Plans need not be topologically ordered. Review every final
                    # deliverable task, not simply the last item in the model's list.
                    upstream = {dependency for task in run["tasks"] for dependency in task["depends_on"]}
                    for task in run["tasks"]:
                        if task["id"] not in upstream and not any(c["kind"] == "human" for c in task["checks"]):
                            task["checks"].append({"kind": "human", "reason": "请检查最终产物及引用来源，是否接受这个版本？"})
                run["status"] = "running"
                self.emit(run, "coordinator", "plan_created", {"tasks": run["tasks"]})
                self.store.save(run)
            while not run["pending"]:
                self.check_cancelled()
                elapsed = run["active_seconds"] + time.monotonic() - start
                if elapsed >= self.config.active_timeout or run["steps"] >= self.config.max_steps:
                    raise RuntimeError("Run active-time or step budget exhausted")
                self.resolve_children(run)
                by_id = {task["id"]: task for task in run["tasks"]}
                for task in run["tasks"]:
                    if task["status"] == "pending" and any(by_id[d]["status"] in {"failed", "cancelled"} for d in task["depends_on"]):
                        task["status"], task["summary"] = "failed", "Required dependency did not succeed"
                ready = [task for task in run["tasks"] if task["status"] == "running" or
                         (task["status"] == "pending" and all(by_id[d]["status"] == "succeeded" for d in task["depends_on"]))]
                if not ready:
                    if all(t["status"] in TERMINAL for t in run["tasks"]):
                        run["status"] = "succeeded" if all(t["status"] == "succeeded" for t in run["tasks"]) else "failed"
                        break
                    raise RuntimeError("No runnable task; unresolved dependency state")
                task = ready[0]
                action = {"type": "scheduler"}
                try:
                    if not task["owner"]:
                        self.assign(run, task)
                    elif task['status'] == 'pending':
                        self.refresh_inputs(run, task)
                        task['status'] = 'running'
                    if task["steps"] >= self.config.max_task_steps:
                        raise RuntimeError("Task step budget exhausted")
                    task["steps"] += 1
                    run["steps"] += 1
                    context = {"agent": task["owner"], "goal": run["goal"], "task": task,
                               "tools": self.tools.specs(task["owner"]),
                               "workspace_files": self.tools.files(self.store.workspace(task["owner"], run["id"])),
                               'collaboration': self.collaboration_context(run, task),
                               'knowledge_base_ids': run.get('knowledge_base_ids'),
                               "clarifications": run["clarifications"]}
                    action = self.model(run, "act", context)
                    action = self.normalize_decision(run, task, action)
                    self.action(run, task, action)
                except Exception as exc:
                    if isinstance(exc, (ProviderUnavailable, RunCancelled)):
                        raise
                    run["inflight"] = None
                    self.fail_attempt(run, task, action, exc, terminal=(
                        task["steps"] >= self.config.max_task_steps or run["model_calls"] >= self.config.max_model_calls
                        or action.get("type") == "fail"))
                    verification = task.get('last_verification', {})
                    if (task['role'] == 'verifier' and verification.get('step') == task['steps']
                            and not verification.get('result', {}).get('success', True)
                            and 'AssertionError' in verification.get('result', {}).get('output', '')
                            and (action.get('name') == 'python_run' or action.get('type') == 'finish')):
                        try:
                            self.request_revision(run, task, str(exc))
                        except ValueError as revision_error:
                            task['status'] = 'failed'
                            task['summary'] = f'{exc}\nRevision unavailable: {revision_error}'[:4000]
                            self.emit(run, task['owner'], 'revision_unavailable', {'task_id': task['id'], 'reason': str(revision_error)})
                self.store.save(run)
        except RunCancelled:
            self.cancel(run)
        except Exception as exc:
            run["status"], run["failure"] = "failed", str(exc)[:2000]
            for task in run["tasks"]:
                if task["status"] not in TERMINAL:
                    self.fail_attempt(run, task, {"type": "controller"}, exc, terminal=True)
        finally:
            run["active_seconds"] += time.monotonic() - start
            if run["status"] in TERMINAL:
                self.finalize(run)
            self.store.save(run)
            self.write_report(run)
        return run

    def resume(self, run_id: str, request_id: str, reply: str) -> dict:
        run = self.store.load(run_id)
        if Config(**run["config"]) != self.config:
            raise ValueError("Resume requires the original configuration")
        pending = run["pending"]
        if not pending or pending["id"] != request_id:
            raise ValueError("No matching pending request; old approvals cannot be reused")
        task = next((t for t in run["tasks"] if t["id"] == pending["task_id"]), None)
        approval = pending["kind"] in {"tool_approval", "acceptance"}
        if approval and reply.lower() not in {"yes", "no"}:
            raise ValueError("Approval reply must be exactly yes or no")
        if not approval and not reply.strip():
            raise ValueError("Clarification reply cannot be empty")
        run["human_wait_seconds"] += time.time() - pending["created"]
        run["pending"] = None
        run["status"] = "running" if task else "planning"
        if task:
            task["status"] = "running"
        self.emit(run, "human", "human_reply", {"request_id": request_id, "kind": pending["kind"], "reply": reply})
        self.store.save(run)
        started = time.monotonic()
        if approval:
            if reply.lower() == "no":
                self.fail_attempt(run, task, pending["action"], PermissionError("Human rejected the operation/acceptance"), terminal=True)
            else:
                try:
                    self.action(run, task, pending["action"], approved=True, prepared=pending["prepared"])
                except Exception as exc:
                    run["inflight"] = None
                    self.fail_attempt(run, task, pending["action"], exc)
        elif task:
            task["history"].append({"action": pending["action"], "result": {"human_reply": reply}})
        else:
            run["clarifications"].append({"question": pending["question"], "answer": reply})
        run["active_seconds"] += time.monotonic() - started
        self.store.save(run)
        return self.advance(run)

    def finalize(self, run: dict) -> None:
        if run["finish_recorded"]:
            return
        run["ended"] = time.time()
        run["finish_recorded"] = True
        if run["memory_enabled"]:
            self.store.remember("coordinator", {"id": f"{run['id']}:coordination:{run.get('revision_count', 0)}", "run_id": run["id"],
                "trigger": run["goal"], "lesson": "此前任务的执行结果（不保证新任务同样成功）：" +
                json.dumps({"status": run["status"], "tasks": [{"title": t["title"], "owner": t["owner"], "status": t["status"]} for t in run["tasks"]]}, ensure_ascii=False),
                "verified": True, "kind": "execution_history", "evidence": {"run_id": run["id"]}})
            for task in run["tasks"]:
                if not task["owner"]:
                    continue
                errors = [h["result"] for h in task["history"] if h.get("result", {}).get("observed_error")]
                if errors:
                    self.store.remember(task["owner"], {"id": f"{run['id']}:{task['id']}:failure:{run.get('revision_count', 0)}", "run_id": run["id"],
                        "trigger": task["objective"], "lesson": "已观察到以下失败，下次执行前检查同类条件；不是已验证的修复方法：" +
                        errors[-1]["observed_error"][:1000], "verified": True, "kind": "observed_failure",
                        "evidence": errors[-1]})
        self.emit(run, "coordinator", "run_finished", {"status": run["status"], "mode": run["mode"], "metrics": self.metrics(run)})

    def metrics(self, run: dict) -> dict:
        total = len(run["tasks"])
        success = sum(t["status"] == "succeeded" for t in run["tasks"])
        end = run["ended"] or time.time()
        return {"mode": run["mode"], "overall_success": run["status"] == "succeeded",
                "subtasks_total": total, "subtasks_succeeded": success,
                "subtask_completion_rate": success / total if total else 0.0,
                "execution_seconds": round(end - run["created"], 4),
                "active_seconds": round(run["active_seconds"], 4),
                "human_wait_seconds": round(run["human_wait_seconds"] + (
                    time.time() - run["pending"]["created"] if run["pending"] else 0), 4),
                "model_calls": run["model_calls"], "steps": run["steps"],
                "completion_basis": "declared_checks; external benchmark validation reported separately"}

    def write_report(self, run: dict) -> Path:
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"# EvoTask {run['id']}", "", f"- 状态：{run['status']}", f"- 模式：{run['mode']}",
                 "- 验证范围：仅声明的检查；不等于所有事实都已核验。", ""]
        if run["mode"] == "demo":
            lines.extend(["> DEMO：确定性模拟模型，不是实际 LLM 性能结果。", ""])
        lines.extend(["## 目标", "", run["goal"], "", "## 指标", "", "```json",
                      json.dumps(self.metrics(run), ensure_ascii=False, indent=2), "```", "", "## 子任务", ""])
        for task in run["tasks"]:
            lines.append(f"- `{task['id']}` / {task['owner'] or 'unassigned'} / {task['status']}: {task['summary'] or task['title']}")
        lines.extend(["", "## 产物与验证状态", ""])
        for artifact in run["artifacts"]:
            lines.append(f"- `{artifact['id']}` — `{artifact['path']}` — {artifact['verification']} — SHA256 `{artifact['sha256']}`")
        if run["pending"]:
            lines.extend(["", "## 等待人工输入", "", "```json", json.dumps(run["pending"], ensure_ascii=False, indent=2), "```"])
        failures = [event for event in self.store.events(run["id"]) if event["kind"] == "attempt_failed"]
        if failures or run["failure"]:
            lines.extend(["", "## 失败证据", "", "```json", json.dumps(
                {"controller_failure": run["failure"], "attempts": [event["data"] for event in failures]}, ensure_ascii=False, indent=2), "```"])
        destination = directory / "status.md"
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if run["status"] == "failed":
            (directory / "failure.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination
