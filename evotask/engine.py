from __future__ import annotations

import json
import copy
import hashlib
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .outputs import validate_outputs, validate_output_contract
from .acceptance import requirements, validate_contract, file_manifest, classify_failure, fingerprint, read_review, ReviewBlocked
from .config import Config
from .collaboration import ACTION_HELP, ancestors, descendants, normalize_action, urls_in
from .providers import ProviderUnavailable
from .retrieval import recall
from .storage import AGENTS, Store, atomic_json, digest, safe_path, uid
from .tools import CAPABILITIES, Tools
from .teams import Teams
from .interaction import INTERACTION_POLICY, clarification, require_prose, task_locale, task_text
from . import verification_guard as guard



TERMINAL = {"succeeded", "failed", "cancelled"}
CITATION = re.compile(r"\[(doc_[a-f0-9]{12}:[a-f0-9]{8}:\d+)\]")


class RunCancelled(Exception):
    """Cooperative cancellation at model/tool boundaries."""


class RevisionTargetRequired(ValueError):
    """The verifier must identify the responsible author among multiple dependencies."""


def validate_checks(checks: object) -> list[dict]:
    if not isinstance(checks, list) or not 1 <= len(checks) <= 8:
        raise ValueError("Every task needs 1–8 acceptance checks")
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in {"exists", "contains", "python", "human", "sources", "review", "sections", "browser"}:
            raise ValueError("Invalid acceptance check")
        if check["kind"] == "human":
            if not isinstance(check.get("reason"), str) or not check["reason"].strip():
                raise ValueError("Human check requires a reason")
        else:
            if check['kind']=='browser':
                from .browser_checks import validate_steps
                validate_steps(check.get('steps'))
            if check['kind']=='sections' and (not isinstance(check.get('headings'),list) or not check['headings'] or any(not isinstance(h,str) or not h.strip() for h in check['headings'])):
                raise ValueError('Sections check needs nonempty headings')
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


def task_from_spec(spec: dict, task_id: str | None = None, parent: str | None = None, depth: int = 0,
                   capabilities: dict | None = None) -> dict:
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
    if not any(set(required) <= tools for tools in (capabilities or CAPABILITIES).values()):
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


def validate_plan(specs: object, limit: int, capabilities: dict | None = None) -> list[dict]:
    if not isinstance(specs, list) or not 1 <= len(specs) <= limit:
        raise ValueError("Plan must contain a bounded, nonempty task list")
    tasks = [task_from_spec(spec, capabilities=capabilities) for spec in specs]
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

    def bind_run(self, run: dict) -> None:
        self.store.bind_team(run.get('team_id'))
        team = run.get('team_snapshot')
        self.tools.capabilities = ({a: set(team['roles'][a]['tools']) for a in AGENTS}
                                   if team else {a:set(names) for a,names in CAPABILITIES.items()})
        for permissions in self.tools.capabilities.values():
            permissions.add('team_context')
            if 'python_run' in permissions:
                permissions.add('verify_revision')

    def role_spec(self, run: dict, spec: dict) -> dict:
        if not isinstance(spec, dict):
            raise ValueError('Task must be an object')
        role = spec.get('role')
        if isinstance(role, str) and role in AGENTS:
            return spec
        normalize = lambda value: re.sub(r'[\s_-]+', '', value.casefold())
        candidates = {a for a in AGENTS if isinstance(role, str) and normalize(role) == normalize(a)}
        if run.get('team_snapshot') and isinstance(role, str):
            candidates.update(a for a, profile in run['team_snapshot']['roles'].items()
                              if normalize(role) == normalize(profile['name']))
        if len(candidates) != 1:
            raise ValueError(f'Unknown or ambiguous role {str(role)[:80]!r}; use internal role IDs: {", ".join(AGENTS)}')
        slot = candidates.pop()
        self.emit(run, 'coordinator', 'role_normalized', {'original_role': role, 'role': slot})
        return dict(spec, role=slot)

    def plan_tasks(self, run: dict, planning_memory: list, started: float) -> list[dict] | None:
        context = {'goal': run['goal'], 'clarifications': run['clarifications'], 'inputs': run['initial_inputs'],
                   'memory': planning_memory,
                   'knowledge_bases': [b for b in self.tools.kb.bases() if run.get('knowledge_base_ids') is None
                                       or b['id'] in run['knowledge_base_ids']],
                   'capabilities': {a: self.tools.specs(a) for a in AGENTS}}
        for attempt in range(3):
            if run['active_seconds'] + time.monotonic() - started >= self.config.active_timeout:
                raise RuntimeError('Run active-time budget exhausted')
            plan = {}
            try:
                plan = self.model(run, 'plan', context)
                self.emit(run, 'coordinator', 'plan_received', {'attempt': attempt + 1, 'plan': plan})
                if plan.get('question'):
                    question, options = clarification(plan)
                    self.wait(run, None, 'planning', question, options=options)
                    return None
                specs = plan.get('tasks')
                if isinstance(specs, list):
                    specs = [self.role_spec(run, spec) for spec in specs]
                tasks = validate_plan(specs, self.config.max_tasks, self.tools.capabilities)
                if self.config.provider != 'demo' and run.get('execution_schema', 0) >= 3:
                    validate_output_contract(specs)
                    required = requirements(plan.get('requirements'))
                    validate_contract(tasks, required)
                    run['requirements'] = required
                    run['requirements_context_version'] = Teams(self.store).decisions(run['team_id'])['version'] if run.get('team_id') else 0
                return tasks
            except (ValueError, TypeError) as exc:
                self.emit(run, 'coordinator', 'plan_rejected', {'reason': str(exc), 'attempt': attempt + 1})
                if attempt == 2:
                    raise
                context.update(previous_plan=plan, validation_error=str(exc),
                               correction='Correct the validation error and follow the task language policy. No tasks or tools have executed. Use the four internal role IDs.')
        raise RuntimeError('Plan validation failed')

    def cancel(self, run: dict) -> dict:
        self.bind_run(run)
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
            persist: bool = True, run_id: str | None = None,
            team_id: str | None = None, source_run_id: str | None = None, artifact_ids: list[str] | None = None,
            team_snapshot: dict | None = None, pinned_inputs: list | None = None) -> dict:
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 12000:
            raise ValueError("Goal must contain 1–12000 characters")
        self.tools.kb.validate_bases(knowledge_base_ids)
        teams = Teams(self.store)
        team = copy.deepcopy(team_snapshot) if team_snapshot else teams.get(team_id) if team_id else None
        if team and team['id'] != team_id:
            raise ValueError('Team snapshot does not match the selected team')
        self.store.bind_team(team_id)
        history = teams.history(team_id) if team_id else None
        inherited = pinned_inputs if pinned_inputs is not None else teams.inputs(team_id, source_run_id, artifact_ids or [])
        run = {"id": run_id or uid("run"), "goal": goal, "status": "planning", "created": time.time(), "ended": None,
               "config": self.config.snapshot(), "config_hash": self.config.fingerprint(),
               "mode": "demo" if self.config.provider == "demo" else "live", "tasks": [], "artifacts": [],
               "pending": None, "inflight": None, "steps": 0, "model_calls": 0, "active_seconds": 0.0,
               "human_wait_seconds": 0.0, "clarifications": [], "memory_enabled": memory,
               "initial_memory_hashes": {a: digest(json.dumps(self.store.memory(a), sort_keys=True).encode()) for a in AGENTS},
               "initial_inputs": [], "failure": None, "finish_recorded": False, "require_review": require_review,
               "knowledge_base_ids": knowledge_base_ids, "revision_count": 0, "revisions": [], "artifact_schema": 2, "execution_schema": 4, "requirements": [],
               'interaction_locale': 'zh' if self.config.provider == 'demo' else task_locale(goal),
               'team_id': team_id, 'team_snapshot': team, 'group_history': history,
               'inherited_inputs': [], 'input_manifest': []}
        self.bind_run(run)
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
            with source.open('rb') as stream:
                run['input_manifest'].append({'path': relative, 'name': name, 'input_path': relative, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()})
        for metadata, content in inherited:
            relative = metadata['input_path']
            if (not relative.startswith('inputs/') or relative in run['initial_inputs']
                    or digest(content) != metadata['sha256']
                    or (metadata.get('source_run_id') and len(content) > self.config.max_file_bytes)):
                raise ValueError('Invalid, duplicate or oversized inherited snapshot')
            content.decode('utf-8')
            for agent in AGENTS:
                target = safe_path(self.store.workspace(agent, run['id']), relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            run['initial_inputs'].append(relative)
            run['input_manifest'].append(copy.deepcopy(metadata))
            if metadata.get('source_run_id'):
                run['inherited_inputs'].append(copy.deepcopy(metadata))
        if persist:
            self.store.message("group", "human", goal)
            self.store.save(run)
            self.emit(run, "coordinator", "run_created", {"mode": run["mode"], "config_hash": run["config_hash"]})
        return run

    def rerun(self, previous: dict) -> dict:
        if previous['status'] not in {'failed', 'cancelled'}:
            raise ValueError('Only failed or cancelled runs may be restarted')
        snapshots = []
        for relative in previous['initial_inputs']:
            content = safe_path(self.store.workspace('coordinator', previous['id']), relative).read_bytes()
            metadata = next((m for m in previous.get('input_manifest', []) if m['input_path'] == relative),
                            {'input_path': relative, 'sha256': digest(content)})
            if digest(content) != metadata['sha256']:
                raise ValueError('Original input snapshot has changed')
            snapshots.append((metadata, content))
        run = self.new(previous['goal'], memory=previous['memory_enabled'], require_review=previous.get('require_review', False),
                       knowledge_base_ids=previous.get('knowledge_base_ids'), team_id=previous.get('team_id'),
                       team_snapshot=previous.get('team_snapshot'), pinned_inputs=snapshots)
        run.update(rerun_of=previous['id'], uploaded_files=copy.deepcopy(previous.get('uploaded_files', [])))
        self.store.save(run)
        return run

    def model(self, run: dict, purpose: str, context: dict) -> dict:
        self.bind_run(run)
        context = dict(context, current_time_utc=datetime.now(timezone.utc).isoformat(),
                       task_goal=run['goal'], interaction_policy=INTERACTION_POLICY, clarifications=run['clarifications'])
        if run.get('team_snapshot'):
            context = dict(context, team=run['team_snapshot'],
                           role_profile=run['team_snapshot']['roles'][context.get('agent', 'coordinator')],
                           recent_group_messages=[{'sender': m['sender'], 'body': m['body'][:1200]}
                                                  for m in self.store.messages('group', limit=8)])
        context['requirements'] = run.get('requirements', [])
        context['inherited_manifest'] = [{k:m[k] for k in ('source_run_id','artifact_id','path','input_path','sha256','source_requirements') if k in m} for m in run.get('inherited_inputs',[])]
        if run.get('team_id'):
            context['durable_context'] = Teams(self.store).decisions(run['team_id'])
            version = context['durable_context']['version']
            if run.get('requirements_context_version') is not None and version != run.get('observed_context_version',run['requirements_context_version']):
                self.emit(run,'coordinator','requirements_context_changed',{'previous':run.get('observed_context_version',run['requirements_context_version']),'current':version,'plan_requirements_retained':True})
                run['observed_context_version']=version
            context['requirements_context_changed'] = version != run.get('requirements_context_version',version)
        if purpose == 'plan':
            context = dict(context, group_history=run.get('group_history'), inherited_inputs=run.get('inherited_inputs', []))
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
                'input_bindings': task.get('input_bindings', []),
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
        source_ids.update(task.get('repair_input_ids', []))
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

    def request_revision(self, run: dict, verifier: dict, reason: str, target_id: str | None = None, *, origin: str | None = None) -> dict:
        """Reopen the author and descendants; retain contracts, frozen tests and all snapshots."""
        if verifier['role'] != 'verifier' or not verifier['owner']:
            raise ValueError('Only an assigned verifier can request an upstream revision')
        if run.get('revision_count', 0) >= 2:
            raise ValueError('Revision budget exhausted (two rounds per run)')
        if run['steps'] >= self.config.max_steps or run['model_calls'] >= self.config.max_model_calls or run['active_seconds'] >= self.config.active_timeout:
            raise ValueError('Run budget exhausted; start a new task with the saved evidence')
        allowed_sources = ancestors(run['tasks'],verifier['id']) if run.get('execution_schema',0)>=3 and target_id else verifier['depends_on']
        candidates = [t for t in run['tasks'] if t['id'] in allowed_sources and t['status'] == 'succeeded' and t['role'] != 'verifier']
        if not target_id and len(candidates) > 1 and reason.strip():
            self.freeze_verification(run, verifier)
            raise RevisionTargetRequired('Choose the responsible author with request_revision(target_task, reason). '
                                         'Candidate task IDs: ' + ', '.join(t['id'] for t in candidates) +
                                         '. The failing verification scripts are frozen; do not edit their assertions.')
        target = next((t for t in candidates if t['id'] == target_id), None) if target_id else (candidates[0] if len(candidates) == 1 else None)
        if not target or not reason.strip():
            raise ValueError('Revision needs a specific successful direct dependency and an observed failure')
        affected = descendants(run['tasks'], target['id']) | {target['id']}
        if any(t['children'] for t in run['tasks'] if t['id'] in affected):
            raise ValueError('This revision spans delegated children; start a new task with archived outputs')
        frozen = self.freeze_verification(run, verifier)
        number = run.get('revision_count', 0) + 1
        feedback = {'round': number, 'from_task': verifier['id'], 'target_task': target['id'], 'reason': reason[:4000],
                    'verifier_objective': verifier['objective'], 'checks': verifier['checks'], 'frozen_tests': frozen[:], 'created': time.time()}
        # Include attribution before any event or saved snapshot exposes the revision.
        if origin is not None:
            feedback['origin'] = origin
        # Reverse handoff is explicitly authorized and pinned; ancestor lookup alone cannot deliver QA files.
        target.setdefault('repair_input_ids', []).extend(f['artifact_id'] for f in frozen if f['artifact_id'] not in target.get('repair_input_ids', []))
        feedback['failure_evidence'] = copy.deepcopy(verifier.get('last_verification'))
        feedback['requirements'] = copy.deepcopy(run.get('requirements', []))
        feedback['reproduction_inputs'] = []
        observed = (verifier.get('last_verification') or {}).get('result',{}).get('verification',{}).get('manifest',{})
        for binding in verifier.get('input_bindings',[]):
            if binding['path'] not in observed:
                continue
            artifact = next(a for a in run['artifacts'] if a['id']==binding['artifact_id'])
            content = self.store.artifact_bytes(artifact)
            if digest(content) != observed[binding['path']]:
                raise ValueError('Repair package evidence changed; rerun verification before revision')
            package_path = f"inputs/repairs/round_{number}/{binding['path']}"
            destination = safe_path(self.store.workspace(target['owner'],run['id']),package_path)
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists() and digest(destination.read_bytes()) != artifact['sha256']:
                raise ValueError('Repair package conflicts with an existing input; cannot replace silently')
            destination.write_bytes(content)
            if package_path not in target['inputs']:
                target['inputs'].append(package_path)
            feedback['reproduction_inputs'].append({'path':package_path,'original_test_path':binding['path'],'sha256':artifact['sha256']})
        if target['owner']:
            self.refresh_inputs(run, target)
            feedback['test_inputs'] = [b.copy() for b in target.get('input_bindings', []) if b['artifact_id'] in target['repair_input_ids']]
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
                atomic_json(self.store.memory_path(task['owner']), memories)
        run.update(status='running', pending=None, failure=None, ended=None, finish_recorded=False)
        self.emit(run, verifier['owner'], 'revision_requested', feedback)
        self.store.message('group', verifier['owner'], task_text(run, f"第 {number} 轮返修：退回 {target['id']}。{reason[:500]}；保留原验收要求并重新验证。",
                                     f"Revision round {number}: returned {target['id']} to the author. {reason[:500]} Recheck against the original criteria."))
        self.store.save(run)
        return feedback

    def freeze_verification(self, run: dict, verifier: dict) -> list[dict]:
        paths = {c['path'] for c in verifier['checks'] if c['kind'] == 'python'}
        paths.update(h.get('action', {}).get('args', {}).get('path') for h in verifier['history']
                     if h.get('action', {}).get('name') == 'python_run')
        frozen = verifier.setdefault('revision_tests', [])
        for path in sorted(p for p in paths if isinstance(p, str)):
            source = self.tools._file(self.store.workspace(verifier['owner'], run['id']), path)
            if source.is_file() and not any(f['path'] == path for f in frozen):
                artifact = self.store.archive(run, verifier['owner'], path, verifier['id'])
                frozen.append({'path': path, 'artifact_id': artifact['id'], 'sha256': artifact['sha256']})
        return frozen

    def retry_verification(self, run: dict) -> dict:
        self.bind_run(run)
        if run['status'] != 'failed':
            raise ValueError('Only a failed run can be reopened for revision')
        verifier = next((t for t in run['tasks'] if t['role'] == 'verifier' and t['status'] == 'failed'
                         and any(h.get('result', {}).get('observed_error') for h in t['history'])), None)
        if not verifier:
            raise ValueError('No failed verifier with recorded evidence is available')
        if run.get('execution_schema',0)>=3:
            if run['model_calls']>=self.config.max_model_calls or run['active_seconds']>=self.config.active_timeout or verifier['steps']>=self.config.max_task_steps or run.get('diagnostic_resume_count',0)>=2:
                raise ValueError('Diagnostic resume budget exhausted; start a new task with archived evidence')
            run['diagnostic_resume_count']=run.get('diagnostic_resume_count',0)+1
            run.update(status='running',failure=None,ended=None,finish_recorded=False,pending=None)
            verifier.update(status='running',error_streak=0,summary='')
            for dependent in run['tasks']:
                if dependent['id'] in descendants(run['tasks'],verifier['id']) and dependent['summary']=='Required dependency did not succeed':
                    dependent.update(status='pending',summary='')
            self.freeze_verification(run,verifier)
            observed=verifier.get('last_verification',{}).get('result',{}).get('verification',{})
            if observed.get('executed',0)>0:
                self.diagnose_verification(run,verifier)
            self.emit(run,'human','diagnostic_resume',{'task_id':verifier['id'],'attempt':run['diagnostic_resume_count']})
            self.store.save(run)
            return run
        reason = next(h['result']['observed_error'] for h in reversed(verifier['history']) if h.get('result', {}).get('observed_error'))
        try:
            return self.request_revision(run, verifier, reason)
        except RevisionTargetRequired as exc:
            if verifier['steps'] >= self.config.max_task_steps or run.get('revision_selection_retries', 0) >= 2:
                raise ValueError('Revision selection budget exhausted') from exc
            run['revision_selection_retries'] = run.get('revision_selection_retries', 0) + 1
            verifier.update(status='running', error_streak=0, summary='')
            verifier['history'].append({'event': 'revision_target_required', 'guidance': str(exc)})
            run.update(status='running', pending=None, failure=None, ended=None, finish_recorded=False)
            self.emit(run, verifier['owner'], 'revision_target_required', {'task_id': verifier['id'], 'reason': str(exc)})
            self.store.save(run)
            return {'selection_required': True}

    def retryable_action_tasks(self, run: dict) -> list[dict]:
        """Only reopen format failures that this controller can now interpret safely."""
        self.bind_run(run)
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
             prepared: dict | None = None, options: list[str] | None = None) -> None:
        if kind in {'planning', 'clarification'} and any(
                item['question'].strip() == question.strip() for item in run['clarifications']):
            raise ValueError(task_text(run, '这个问题已得到回答，请读取 clarifications 并继续；仅对新的阻塞信息提问。',
                                       'This question has already been answered. Use clarifications and ask only about new blockers.'))
        pending = {"id": uid("request"), "task_id": task["id"] if task else None, "kind": kind,
                   "question": question, "options": options or [], "action": action, "prepared": prepared, "created": time.time()}
        run["pending"], run["status"] = pending, "waiting_human"
        if task:
            task["status"] = "waiting_human"
        self.store.save(run)
        self.emit(run, task["owner"] if task else "coordinator", "waiting_human",
                  {key: pending[key] for key in ("id", "kind", "question", "options", "task_id")})
        if run.get('team_id'):
            prompt = question + ('\n' + ' / '.join(options) if options else '')
            self.store.message('group', task['owner'] if task else 'coordinator', prompt)

    def assign(self, run: dict, task: dict) -> None:
        candidates = [agent for agent in AGENTS if set(task["required_tools"]) <= self.tools.capabilities[agent]]
        if task["role"] == "verifier":
            upstream = ancestors(run['tasks'], task['id'])
            upstream_owners = {t["owner"] for t in run["tasks"] if t["id"] in upstream}
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
            if self.config.provider != 'demo':
                require_prose(bid['reason'], 'Bid reason')
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
        for item in run.get('inherited_inputs', []):
            task['allowed_citations'].extend(item.get('citations', []))
            task.setdefault('source_evidence', []).extend(item.get('source_evidence', []))
        self.refresh_inputs(run, task)
        if run["memory_enabled"]:
            task["memory_hits"] = recall(self.store, task["owner"], task["objective"])
        self.emit(run, task["owner"], "memory_recalled", {"task_id": task["id"], "ids": [m["id"] for m in task["memory_hits"]]})
        self.emit(run, task["owner"], "task_assigned", {"task_id": task["id"], "title": task["title"], "objective": task["objective"]})
        self.store.message("group", task["owner"], task_text(run, "接受任务", "Accepted task") + f" {task['id']}: {task['title']}")
        self.store.save(run)

    def fail_attempt(self, run: dict, task: dict, action: dict, exc: Exception, terminal: bool = False) -> None:
        message = str(exc)[:2000]
        task["error_streak"] += 1
        snapshot = self.store.snapshot(run, task) if task["owner"] else []
        result = task.get('last_verification', {}).get('result', {})
        category = classify_failure(message, result)
        evidence = result.get('verification',{})
        observed = re.sub(r'EVOTASK_VERIFICATION_[a-f0-9]+', 'EVOTASK_VERIFICATION', result.get('output',message)) if task.get('last_verification',{}).get('step')==task['steps'] else message
        version = json.dumps({'targets': evidence.get('targets') or evidence.get('manifest', {}),
                              'test_sha256': result.get('test_artifact', {}).get('sha256'),
                              'check': task.get('last_verification', {}).get('check')}, sort_keys=True)
        key = fingerprint(category, observed, version)
        repeats = task.setdefault('failure_fingerprints', {})
        repeats[key] = repeats.get(key, 0) + 1
        task['failure_diagnosis'] = {'category': category, 'fingerprint': key, 'occurrences': repeats[key], 'evidence': task.get('last_verification')}
        if run.get('execution_schema', 0) >= 3 and repeats[key] >= 3:
            terminal = True
        record = {"category": category, "fingerprint": key, "occurrences": repeats[key], "error_type": type(exc).__name__, "observed_error": message,
                  "cause_hypothesis": None, "artifact_ids": [a["id"] for a in snapshot]}
        task["history"].append({"action": action, "result": {"success": False, **record}})
        self.emit(run, task["owner"] or "coordinator", "attempt_failed", {"task_id": task["id"], **record})
        self.store.message("group", task["owner"] or "coordinator", task_text(run, f"任务 {task['id']} 尝试失败：{message[:300]}；已有产物已归档。",
                                     f"Task {task['id']} attempt failed: {message[:300]}. Partial outputs archived."))
        if terminal or task["error_streak"] >= 3:
            task["status"], task["summary"] = "failed", message
        self.store.save(run)

    def delegate(self, run: dict, task: dict, action: dict) -> dict:
        if len(run["tasks"]) >= self.config.max_tasks or task["depth"] >= self.config.max_delegate_depth:
            raise ValueError("Delegation task/depth budget exceeded")
        child = task_from_spec(self.role_spec(run, action), uid("sub"), task["id"], task["depth"] + 1, self.tools.capabilities)
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
            target = self.tools._file(root, path, writing=True)
            if self.config.provider != 'demo' and not frozen:
                if target.stat().st_size > self.config.max_file_bytes:
                    raise ValueError('交付文件超过配置的大小限制。')
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
            if run.get('execution_schema', 0) >= 3:
                # Every check, including browser and review checks, must see the authorized version.
                by_id = {a['id']: a for a in run['artifacts']}
                for binding in task.get('input_bindings', []):
                    expected = by_id[binding['artifact_id']]['sha256']
                    if file_manifest(root, [binding['path']])[binding['path']] != expected:
                        raise ValueError('Authorized input snapshot changed: ' + binding['path'])
            for artifact in artifacts:
                self.check_citations(task, artifact)
                if task['role']=='verifier' and artifact['path'].endswith('.json'):
                    value = json.loads(self.store.artifact_bytes(artifact))
                    if isinstance(value,dict) and 'verdict' in value:
                        # A structured negative/blocked review can never be promoted by an unrelated passing test.
                        review = read_review(safe_path(root,artifact['path']))
                        for item in review['requirements']:
                            for evidence in item['evidence']:
                                if file_manifest(root,[evidence['path']])[evidence['path']] != evidence['sha256']:
                                    raise ValueError('Review evidence hash mismatch: '+evidence['path'])
            for check in sorted(task["checks"], key=lambda c: c['kind'] == 'human'):
                kind = check["kind"]
                if kind == "human":
                    if not approved:
                        reasons = [c["reason"] for c in task["checks"] if c["kind"] == "human"]
                        self.wait(run, task, "acceptance", task_text(run, "是否接受本次交付？可先查看文件及验收要求。",
                                            "Accept this delivery? You can review the files and acceptance criteria first."), action,
                                  {"artifacts": artifacts, "review_checks": reasons})
                        return
                    outcomes.append({"kind": "human", "passed": True})
                    continue
                actual_path = check['path']
                if check.get('source_task'):
                    actual_path = self.resolve_input(task, check['source_task'], check['path'])
                path = self.tools._file(root, actual_path)
                if not path.is_file():
                    raise ValueError(f"Acceptance failed: missing {check['path']}")
                if path.stat().st_size > self.config.max_file_bytes:
                    raise ValueError("Acceptance target exceeds file limit")
                if kind == "contains" and check["text"] not in path.read_text(encoding="utf-8"):
                    raise ValueError(f"Acceptance failed: {actual_path} missing required text {check['text']!r}")
                if kind == 'sources':
                    content = path.read_text(encoding='utf-8')
                    local = set(CITATION.findall(content)) & set(task.get('allowed_citations', []))
                    observed = {e['url'] for e in task.get('source_evidence', []) if e.get('url')}
                    cited_urls = set(urls_in(content)) & observed
                    policy = check.get('policy', 'any')
                    if not (cited_urls if policy == 'url' else local if policy == 'local' else cited_urls or local):
                        raise ValueError(f"Acceptance failed: {check['path']} needs observed sources ({policy}); do not invent links")
                if kind == 'sections':
                    actual = {re.sub(r'\s+',' ',line.lstrip('#').strip()).casefold() for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('#')}
                    if path.suffix.lower()=='.html':
                        from .acceptance import html_headings
                        actual=html_headings(path.read_text(encoding='utf-8'))
                    missing = [h for h in check['headings'] if re.sub(r'\s+',' ',h.strip()).casefold() not in actual]
                    if missing:
                        raise ValueError(f'Section check failed: {actual_path}; missing={missing}; actual={sorted(actual)}')
                if kind == 'browser':
                    from .browser_checks import verify_browser
                    result = verify_browser(root,actual_path,check['steps'],self.config.tool_timeout)
                    result['verification']['requirement_ids']=check.get('requirement_ids',[])
                    task['last_verification']={'step':task['steps'],'path':actual_path,'result':result,'check':copy.deepcopy(check)}
                    self.emit(run,task['owner'],'verification_evidence',{'task_id':task['id'],'path':actual_path,'result':result})
                    outcomes.append({'kind':kind,'path':actual_path,'result':result})
                    if not result['success']:
                        raise ValueError('Browser check failed: '+result['output'])
                if kind == 'review':
                    review = read_review(path)
                    required = set(check.get('requirement_ids', []))
                    reviewed = {r['id'] for r in review['requirements'] if r['status']=='pass'}
                    if not required <= reviewed:
                        raise ValueError('Review is missing requirements: ' + ', '.join(sorted(required-reviewed)))
                    for item in review['requirements']:
                        for evidence in item['evidence']:
                            manifest = file_manifest(root, [evidence['path']])
                            if evidence.get('sha256') != manifest[evidence['path']]:
                                raise ValueError('Review evidence hash mismatch: ' + evidence['path'])
                    outcomes.append({'kind':kind,'path':actual_path,'review':review})
                if kind == "python":
                    self.tools.validate(task["owner"], "python_run", {"path": check["path"]})
                    run["inflight"] = {"task_id": task["id"], "action": {"type": "verification", "check": check}, "started": time.time()}
                    self.store.save(run)
                    result = self.verify(run, task, check)
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
            artifact["acceptance_evidence"] = copy.deepcopy(outcomes)
            artifact["requirements"] = copy.deepcopy(run.get("requirements", []))
        task["artifact_ids"] = [a["id"] for a in artifacts]
        task["status"] = "succeeded"
        task["summary"] = str(action.get("summary", ""))[:3000]
        task["history"].append({"action": action, "result": {"checks": outcomes, "success": True}})
        self.emit(run, task["owner"], "task_succeeded", {"task_id": task["id"], "artifacts": task["artifact_ids"], "checks": outcomes, "summary": task["summary"]})
        self.store.message("group", task["owner"], task_text(run, f"任务 {task['id']} 通过声明的检查：{task['summary']}",
                                     f"Task {task['id']} passed the declared checks: {task['summary']}"))
        if run["memory_enabled"]:
            self.store.remember(task["owner"], {"id": f"{run['id']}:{task['id']}:success:{run.get('revision_count', 0)}", "run_id": run["id"],
                "trigger": task["objective"], "lesson": "此前类似任务通过以下检查；仍需在新任务重验：" + json.dumps(task["checks"], ensure_ascii=False),
                "verified": True, "evidence": {"artifacts": task["artifact_ids"], "checks": outcomes}, "kind": "success"})
        self.store.save(run)

    def diagnose_verification(self, run: dict, task: dict) -> None:
        evidence = task['last_verification']
        if 'check' not in evidence and evidence.get('result',{}).get('sandbox')=='isolated-browser-context':
            for check in task['checks']:
                if check['kind']=='browser':
                    actual=self.resolve_input(task,check['source_task'],check['path']) if check.get('source_task') else check['path']
                    if actual==evidence['path']:evidence['check']=copy.deepcopy(check);break
        root = self.store.workspace(task['owner'],run['id'])
        files = {}
        observed = evidence.get('result',{}).get('verification',{}).get('manifest',{})
        source_paths = list(evidence.get('result',{}).get('verification',{}).get('targets',{})) or [b['path'] for b in task.get('input_bindings',[]) if b['path'] in observed][:5]
        for path in [evidence['path']] + source_paths:
            source = self.tools._file(root,path)
            if source.is_file():
                guard.require_file(self.tools, run, task, path)
                files[path] = source.read_text(encoding='utf-8')
        decision = self.model(run,'diagnose',{'agent':task['owner'],'task':task,'failure':evidence,
            'files':files,'collaboration':self.collaboration_context(run,task),'goal':run['goal']})
        decision = guard.review_diagnosis(self, run, task, decision, files, evidence)
        category = decision.get('failure_kind')
        if category not in {'business','test','path','environment','evidence','uncertain'} or not isinstance(decision.get('reason'),str):
            raise ValueError('Diagnosis requires failure_kind and evidence-based reason')
        task['history'].append({'event':'failure_diagnosed','decision':decision,'evidence':copy.deepcopy(evidence)})
        self.emit(run,task['owner'],'failure_diagnosed',{'task_id':task['id'],'decision':decision})
        if category=='business':
            self.action(run,task,dict(decision,type='request_revision'))
        elif category=='test' and evidence.get('check',{}).get('kind') in {'browser','sections','contains'}:
            check=evidence['check']
            proposal=self.model(run,'repair_check',{'agent':task['owner'],'task':task,'diagnosis':decision,'files':files,
                'failure':evidence,'goal':run['goal'],'check_index':task['checks'].index(check),'original_check':check})
            proposal=self.normalize_decision(run,task,proposal)
            if proposal['type'] not in {'revise_check','ask'}:raise ValueError('Return revise_check or ask for a declarative test error')
            self.action(run,task,proposal)
        elif category=='test' and any(f['path']==evidence['path'] for f in task.get('revision_tests',[])):
            proposal=self.model(run,'repair_test',{'agent':task['owner'],'task':task,'diagnosis':decision,'files':files,
                'failure':evidence,'goal':run['goal'],'required_action':'revise_test or ask; do not rerun unchanged failed tests'})
            proposal=self.normalize_decision(run,task,proposal)
            if proposal['type'] not in {'revise_test','ask'}:
                raise ValueError('Test diagnosis must propose a test revision or ask for clarification')
            if proposal['type']=='revise_test' and proposal.get('path')!=evidence['path']:
                raise ValueError('Test correction must target the observed failing script')
            self.action(run,task,proposal)
        elif guard.enabled(self, run) and category in {'uncertain', 'evidence'}:
            self.wait(run, task, 'clarification', task_text(run, '验收依据仍不明确，请确认：', 'Please clarify the acceptance criteria: ') + decision['reason'][:700],
                      {'type': 'ask', 'question': decision['reason'][:700]})
        else:
            task['history'].append({'event':'repair_guidance' ,'guidance':
                'Wrong frozen test: use revise_test with exact corrected content/reason/requirement_ids; preserve valid business assertions. Path/environment: fix inputs/test harness, never reopen author without evidence. Uncertain: ask human.'})

    @staticmethod
    def resolve_input(task: dict, source: str, path: str) -> str:
        bindings = [b for b in task.get('input_bindings', []) if b['task_id']==source and b['artifact_path']==path]
        if len(bindings) != 1:
            raise ValueError(f'Input contract unresolved: {source}/{path}; inspect input_bindings instead of guessing/copying filenames')
        return bindings[0]['path']

    def verify(self, run: dict, task: dict, check: dict) -> dict:
        root = self.store.workspace(task['owner'], run['id'])
        script = guard.require_file(self.tools, run, task, check['path'], python=True)
        if run.get('execution_schema',0) < 3 or not hasattr(self.tools.sandbox,'verify'):
            return self.tools.sandbox.execute(root, check['path'])
        targets = [t['path'] if t['task_id']==task['id'] else self.resolve_input(task,t['task_id'],t['path']) for t in check.get('targets',[])]
        for target in targets:
            guard.require_file(self.tools, run, task, target, bounded=False)
        expected = file_manifest(root, targets)
        for binding in task.get('input_bindings',[]):
            if binding['path'] in expected:
                artifact = next(a for a in run['artifacts'] if a['id']==binding['artifact_id'])
                if expected[binding['path']] != artifact['sha256']:
                    raise ValueError('Authorized input snapshot changed: '+binding['path'])
        guard.review_test(self, run, task, check, script, expected, target_root=root)
        test_snapshot = self.store.archive(run,task['owner'],check['path'],task['id'],origin='verification')
        result = self.tools.sandbox.verify(root, check['path'])
        result['test_artifact'] = {'id':test_snapshot['id'],'sha256':test_snapshot['sha256']}
        evidence = result.get('verification', {})
        evidence['requirement_ids'] = check.get('requirement_ids', [])
        evidence['targets'] = expected
        missing = [p for p,h in expected.items() if evidence.get('manifest',{}).get(p) != h]
        if missing:
            result['success'] = False
            result['output'] += '\nAcceptance target not exercised: ' + ', '.join(missing)
        task['last_verification'] = {'step':task['steps'],'path':check['path'],'result':result}
        if not result['success'] and task['role']=='verifier' and evidence.get('executed',0)>0:
            self.freeze_verification(run,task)
            result['diagnosis_required'] = 'Executed failing assertions are archived and frozen. Diagnose against requirements: business defect -> request_revision; wrong expectation -> revise_test with human approval. Never weaken a failed test silently.'
        self.emit(run,task['owner'],'verification_evidence',{'task_id':task['id'],'path':check['path'],'result':result})
        return result

    def propose_test_revision(self, run: dict, task: dict, action: dict) -> None:
        import difflib
        if task['role'] != 'verifier':
            raise ValueError('Only the independent verifier may propose a frozen test correction')
        if guard.enabled(self, run) and guard.reviewed_kind(run, task) != 'test':
            if not task.get('last_verification'):
                raise ValueError('A test revision requires an observed failure and reviewed diagnosis')
            self.diagnose_verification(run, task)
            return
        path = action.get('path')
        if not any(f['path']==path for f in task.get('revision_tests',[])):
            raise ValueError('revise_test requires an existing frozen test; repair unfrozen test scripts using file tools')
        reason, content = action.get('reason'), action.get('content')
        ids = action.get('requirement_ids',[])
        if not isinstance(reason,str) or not reason.strip() or not isinstance(content,str) or len(content.encode())>self.config.max_file_bytes:
            raise ValueError('Test correction needs reason and bounded complete replacement content')
        if not ids or not set(ids) <= {r['id'] for r in run.get('requirements',[])}:
            raise ValueError('Test correction must identify known requirements')
        target = self.tools._file(self.store.workspace(task['owner'],run['id']),path,writing=True)
        old = target.read_bytes()
        if digest(old) == digest(content.encode()):
            raise ValueError('Proposed test has no changes')
        prepared = {'path':path,'old_hash':digest(old),'content':content,'reason':reason,'requirement_ids':ids,
                    'diff':''.join(difflib.unified_diff(old.decode().splitlines(True),content.splitlines(True),fromfile='frozen',tofile='proposed'))}
        self.wait(run,task,'test_revision',task_text(run, '测试与需求存在冲突。是否批准以下测试修订？修改后必须重新验证。',
                      'The test conflicts with the requirements. Approve the proposed revision and rerun verification?'), action, prepared)

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
        if self.config.provider != 'demo' and not approved:
            for field in {'finish': ('summary',), 'delegate': ('title', 'objective'),
                          'fail': ('reason',), 'request_revision': ('reason',)}.get(kind, ()):
                require_prose(action.get(field, ''), field)
        if kind == 'revise_check':
            if guard.enabled(self, run) and guard.reviewed_kind(run, task) != 'test':
                if not task.get('last_verification'):
                    raise ValueError('Check revision requires observed evidence and reviewed diagnosis')
                self.diagnose_verification(run, task)
                return
            from .check_revision import propose
            propose(self,run,task,action)
            return
        if kind == 'revise_test':
            self.propose_test_revision(run, task, action)
            return
        if kind == 'request_revision':
            if guard.enabled(self, run):
                if not task.get('last_verification'):
                    raise ValueError('Run a concrete check before requesting automatic business repair')
                if guard.reviewed_kind(run, task) != 'business':
                    self.diagnose_verification(run, task)
                    return
                action = dict(task['reviewed_diagnosis']['decision'], type='request_revision')
            if run.get('execution_schema', 0) >= 3:
                if action.get('failure_kind') != 'business' or not action.get('requirement_ids'):
                    raise ValueError('Diagnose first: business revision needs failure_kind=business and requirement_ids. Fix test/path errors locally; use revise_test for frozen tests; ask when uncertain.')
                if not set(action['requirement_ids']) <= {r['id'] for r in run.get('requirements', [])}:
                    raise ValueError('Revision references unknown requirements')
                if not task.get('last_verification') and not action.get('evidence'):
                    raise ValueError('Business revision requires observed test failure or explicit defect evidence')
            self.request_revision(run, task, str(action.get('reason', '')), action.get('target_task'))
            return
        if kind == "ask":
            question, options = clarification(action)
            self.wait(run, task, "clarification", question, action, options=options)
            return
        if kind == "finish":
            self.finish(run, task, action, approved, prepared.get("artifacts") if prepared else None)
            return
        if kind == "fail":
            if run.get('execution_schema',0)>=3 and task['role']=='verifier' and any(c['kind']=='python' for c in task['checks']) and not task.get('last_verification'):
                raise ValueError('Verifier must create and execute a minimal independent check before declaring an implementation defect terminal. Reproducible business defects use request_revision; missing evidence/environment uses ask with a concrete blocker.')
            raise RuntimeError("Agent reported inability to finish: " + str(action.get("reason", "")))
        if kind == "delegate":
            result = self.delegate(run, task, action)
        else:
            name, args = action.get("name"), action.get("args", {})
            if not approved:
                prepared = self.tools.prepare(run, task, name, args)
                if prepared:
                    label = (task_text(run, '发布代码到 GitHub', 'publishing code to GitHub') if name == 'git_publish'
                             else task_text(run, '删除文件', 'deleting the file'))
                    question = task_text(run, f"是否允许{label}？请先核对下方的文件与目标。",
                                         f"Allow {label}? Review the files and target first.")
                    self.wait(run, task, "tool_approval", question, action, prepared)
                    return
            run["inflight"] = {"task_id": task["id"], "action": action, "started": time.time()}
            self.store.save(run)  # Never silently replay a potentially side-effecting operation after a crash.
            if name == 'python_run':
                self.check_frozen_tests(run, task)
            planned_check = next((c for c in task['checks'] if c['kind']=='python' and c['path']==args.get('path')), None)
            if name == 'python_run' and planned_check and run.get('execution_schema',0) >= 3:
                result = self.verify(run, task, planned_check)
            else:
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
        self.bind_run(run)
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
                self.emit(run, "coordinator", "memory_recalled", {"ids": [m["id"] for m in planning_memory]})
                tasks = self.plan_tasks(run, planning_memory, start)
                if tasks is None:
                    return run
                run['tasks'] = tasks
                if run.get("require_review"):
                    # Plans need not be topologically ordered. Review every final
                    # deliverable task, not simply the last item in the model's list.
                    upstream = {dependency for task in run["tasks"] for dependency in task["depends_on"]}
                    for task in run["tasks"]:
                        if task["id"] not in upstream and not any(c["kind"] == "human" for c in task["checks"]):
                            task["checks"].append({"kind": "human", "reason": task_text(run, "请检查最终产物及引用来源，是否接受这个版本？",
                                                                   "Review the final outputs and sources. Do you accept this version?")})
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
                    feedback = task.get('revision_feedback') or {}
                    if feedback.get('target_task') == task['id']:
                        root = self.store.workspace(task['owner'],run['id'])
                        for item in feedback.get('reproduction_inputs',[]):
                            if file_manifest(root,[item['path']])[item['path']] != item['sha256']:
                                raise ValueError('Repair package input hash mismatch: '+item['path'])
                        for item in feedback.get('test_inputs',[]):
                            artifact = next(a for a in run['artifacts'] if a['id']==item['artifact_id'])
                            if file_manifest(root,[item['path']])[item['path']] != artifact['sha256']:
                                raise ValueError('Repair package test hash mismatch: '+item['path'])
                    context = {"agent": task["owner"], "goal": run["goal"], "task": task,
                               "tools": self.tools.specs(task["owner"]),
                               "workspace_files": self.tools.files(self.store.workspace(task["owner"], run["id"])),
                               'collaboration': self.collaboration_context(run, task),
                               'knowledge_base_ids': run.get('knowledge_base_ids'),
                               "clarifications": run["clarifications"]}
                    context['execution_readiness'] = guard.readiness(self.tools, run, task)
                    if not context['execution_readiness']['verify_revision_ready']:
                        context['tools'].pop('verify_revision', None)
                    action = self.model(run, "act", context)
                    action = self.normalize_decision(run, task, action)
                    self.action(run, task, action)
                except Exception as exc:
                    if isinstance(exc, (ProviderUnavailable, RunCancelled)):
                        raise
                    run["inflight"] = None
                    if isinstance(exc,ReviewBlocked):
                        task['history'].append({'event':'review_blocked','review':exc.review})
                        self.wait(run,task,'review_blocked',task_text(run, '独立审查缺少足够证据，暂不能交付。请补充来源或说明需要调整的要求。',
                                                      'Independent review needs more evidence. Please add sources or clarify the requirements.'), action, {'review':exc.review})
                        continue
                    self.fail_attempt(run, task, action, exc, terminal=(
                        task["steps"] >= self.config.max_task_steps or run["model_calls"] >= self.config.max_model_calls
                        or action.get("type") == "fail" and not str(exc).startswith('Verifier must')))
                    verification = task.get('last_verification', {})
                    if (run.get('execution_schema',0)>=3 and task['role']=='verifier' and task['steps']<self.config.max_task_steps and run['model_calls']<self.config.max_model_calls and run.get('revision_count',0)<2
                            and verification.get('step')==task['steps'] and not verification.get('result',{}).get('success',True)
                            and verification.get('result',{}).get('verification',{}).get('executed',0)>0):
                        try:
                            self.diagnose_verification(run,task)
                        except ProviderUnavailable:
                            raise
                        except (ValueError,TypeError) as diagnosis_error:
                            task['history'].append({'event':'diagnosis_incomplete','reason':str(diagnosis_error)})
                            self.emit(run,task['owner'],'diagnosis_incomplete',{'task_id':task['id'],'reason':str(diagnosis_error)})
                    if (run.get('execution_schema',0) < 3 and task['role'] == 'verifier' and verification.get('step') == task['steps']
                            and not verification.get('result', {}).get('success', True)
                            and 'AssertionError' in verification.get('result', {}).get('output', '')
                            and (action.get('name') == 'python_run' or action.get('type') == 'finish')):
                        try:
                            self.request_revision(run, task, str(exc))
                        except RevisionTargetRequired as revision_error:
                            task['history'].append({'event': 'revision_target_required', 'guidance': str(revision_error)})
                            self.emit(run, task['owner'], 'revision_target_required', {'task_id': task['id'], 'reason': str(revision_error)})
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
        self.bind_run(run)
        if Config(**run["config"]) != self.config:
            raise ValueError("Resume requires the original configuration")
        pending = run["pending"]
        if not pending or pending["id"] != request_id:
            raise ValueError("No matching pending request; old approvals cannot be reused")
        task = next((t for t in run["tasks"] if t["id"] == pending["task_id"]), None)
        if pending['kind'] in {'acceptance','test_revision','check_revision'} and reply.startswith('revise:'):
            reason = reply[7:].strip()
            target_id = task['id']
            if '|' in reason:
                candidate, detail = reason.split('|',1)
                if any(t['id']==candidate for t in run['tasks']):
                    target_id, reason = candidate, detail.strip()
            if not reason:
                raise ValueError('Quality revision needs specific feedback')
            if target_id != task['id']:
                reviewer = next((t for t in run['tasks'] if t['role']=='verifier' and t.get('owner') and target_id in ancestors(run['tasks'],t['id'])),None)
                if not reviewer:
                    raise ValueError('Selected target has no independent reviewer; choose its own review or provide clarification')
                self.request_revision(run,reviewer,'人工质量反馈：'+reason,target_id,origin='human_quality_feedback')
                self.store.message('group','human',task_text(run, f'质量反馈（{target_id}）：', f'Quality feedback ({target_id}): ') + reason)
                run['clarifications'].append({'question':'交付验收修改意见','answer':reason,'task_id':target_id})
                self.emit(run,'human','quality_revision_requested',{'task_id':target_id,'reason':reason,'request_id':request_id})
                run['human_wait_seconds'] += time.time()-pending['created']
                self.store.save(run)
                return self.advance(run)
            run['human_wait_seconds'] += time.time() - pending['created']
            self.emit(run, 'human', 'quality_revision_requested' , {'task_id': task['id'], 'reason': reason, 'request_id':request_id})
            run['clarifications'].append({'question':'交付验收修改意见', 'answer':reason, 'task_id':task['id']})
            self.store.message('group','human',task_text(run, f"质量反馈（{task['id']}）：", f"Quality feedback ({task['id']}): ") + reason)
            if task['role']=='verifier' and any(t['status']=='succeeded' and t['id'] in task['depends_on'] for t in run['tasks']):
                # Give the independent verifier the feedback; it must diagnose the responsible task.
                task['history'].append({'event':'human_quality_feedback','reason':reason})
            else:
                for a in pending.get('prepared',{}).get('artifacts',[]):
                    next(x for x in run['artifacts'] if x['id']==a['id'])['verification']='superseded'
                task['history'].append({'event':'human_quality_feedback','reason':reason})
            run.update(pending=None,status='running')
            task.update(status='running')
            self.store.save(run)
            return self.advance(run)
        if pending['kind'] in {'check_revision','test_revision'} and reply.startswith('no:'):
            feedback=reply[3:].strip()
            if not feedback:raise ValueError('Test review feedback cannot be empty')
            run['clarifications'].append({'question':'测试修订被拒绝的原因','answer':feedback,'task_id':task['id']})
            self.emit(run,'human','test_revision_feedback',{'request_id':request_id,'reason':feedback})
            reply='no'
        if pending['kind']=='check_revision':
            from .check_revision import apply
            apply(self,run,task,pending,reply)
            run['human_wait_seconds']+=time.time()-pending['created']
            run.update(pending=None,status='running');task['status']='running'
            self.store.save(run)
            return self.advance(run)
        if pending['kind'] == 'test_revision':
            if reply.lower() not in {'yes','no'}:
                raise ValueError('Test revision reply must be yes or no')
            prepared = pending['prepared']
            root = self.store.workspace(task['owner'],run['id'])
            path = self.tools._file(root,prepared['path'],writing=True)
            if digest(path.read_bytes()) != prepared['old_hash']:
                raise ValueError('Test changed after proposal; stale approval rejected')
            if reply.lower()=='yes':
                path.write_bytes(prepared['content'].encode('utf-8'))
                artifact = self.store.archive(run,task['owner'],prepared['path'],task['id'])
                task['revision_tests'] = [f for f in task.get('revision_tests',[]) if f['path'] != prepared['path']]
                task['revision_tests'].append({'path':prepared['path'],'artifact_id':artifact['id'],'sha256':artifact['sha256']})
            self.emit(run,'human','test_revision_reviewed',{'request_id':request_id,'approved':reply.lower()=='yes','old_hash':prepared['old_hash'],'new_hash':digest(prepared['content'].encode()),'reason':prepared['reason']})
            task['history'].append({'event':'test_revision_reviewed','approved':reply.lower()=='yes','reason':prepared['reason']})
            run['human_wait_seconds'] += time.time()-pending['created']
            run.update(pending=None,status='running'); task['status']='running'
            check = next((c for c in task['checks'] if c['kind']=='python' and c['path']==prepared['path']), {'kind':'python','path':prepared['path']})
            # Resume against the exact retained/approved test immediately; don't ask the model to rewrite it again.
            started=time.monotonic()
            try:
                result=self.verify(run,task,check)
                task['history'].append({'event':'test_revision_reverified','approved':reply.lower()=='yes','result':result})
                if result['success']:
                    task['failure_diagnosis']={'category':'resolved','evidence':task['last_verification']}
                    task['error_streak']=0
                else:
                    self.fail_attempt(run,task,{'type':'verification','path':check['path']},ValueError(result['output']))
                    if run['model_calls']<self.config.max_model_calls:
                        self.diagnose_verification(run,task)
            except (ProviderUnavailable,RunCancelled):
                raise
            except Exception as exc:
                self.fail_attempt(run,task,{'type':'verification','path':check['path']},exc)
                self.wait(run,task,'verification_blocked',task_text(run, '测试修订已记录，但复验暂未完成。请处理环境或输入问题后回复继续。',
                                                   'The test revision is saved. Resolve the environment or input issue and reply to resume verification.'), prepared={'error':str(exc)[:2000]})
            finally:
                run['active_seconds']+=time.monotonic()-started
                self.store.save(run)
            return self.advance(run)
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
        if run.get('team_id'):
            self.store.message('group', 'human', reply)
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
            run['clarifications'].append({'question': pending['question'], 'answer': reply, 'task_id': task['id']})
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

    @staticmethod
    def metrics(run: dict) -> dict:
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
