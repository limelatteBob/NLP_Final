"""Version-bound test expectation review and evidence-based diagnosis safeguards.

Model reviews are hypotheses, never execution evidence or permission to rewrite
frozen tests. Legacy runs retain their recorded protocol.
"""
import ast
import json
from .storage import digest, safe_path


class ExecutionPreflightError(ValueError):
    pass


class TestExpectationError(ValueError):
    pass


def enabled(engine, run):
    return run.get('execution_schema', 0) >= 4 and engine.config.provider != 'demo'


def signature(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def rules(run):
    return {'goal': run['goal'], 'requirements': run.get('requirements', []),
            'clarifications': run.get('clarifications', []),
            'context_version': run.get('observed_context_version', run.get('requirements_context_version'))}


def require_file(tools, run, task, relative, *, python=False, bounded=True):
    root = tools.store.workspace(task['owner'], run['id'])
    path = tools._file(root, relative)
    if not path.is_file():
        bindings = [{'source_task': b['task_id'], 'original': b['artifact_path'], 'read_path': b['path']}
                    for b in task.get('input_bindings', [])][:20]
        raise ExecutionPreflightError(
            f'File not ready: {relative}. No code was executed. Create your local script with write_file first; '
            'read existing authorized inputs using these exact paths instead of asking the user to upload them again: '
            + json.dumps(bindings, ensure_ascii=False) + '; available_files=' + json.dumps(tools.files(root)))
    if bounded and path.stat().st_size > tools.config.max_file_bytes:
        raise ExecutionPreflightError('File exceeds execution/read limit: ' + relative)
    if python:
        if path.suffix != '.py':
            raise ExecutionPreflightError('Python execution requires a .py script: ' + relative)
        try:
            ast.parse(path.read_text(encoding='utf-8'), filename=relative)
        except SyntaxError as exc:
            raise ExecutionPreflightError(
                f'Python syntax error in {relative}, line {exc.lineno}: {exc.msg}. '
                'Fix this script before Docker execution; do not modify upstream business code for this error.') from None
    return path


def repair_ready(task):
    feedback = task.get('revision_feedback') or {}
    return bool(task['role'] == 'verifier' and task.get('revision_tests') or
                feedback.get('target_task') == task['id'] and feedback.get('frozen_tests'))


def readiness(tools, run, task):
    root = tools.store.workspace(task['owner'], run['id'])
    scripts = []
    for check in task['checks']:
        if check['kind'] == 'python':
            scripts.append({'path': check['path'], 'exists': tools._file(root, check['path']).is_file(),
                            'next_step_if_missing': 'write_file, then python_run'})
    return {'python_checks': scripts, 'verify_revision_ready': repair_ready(task),
            'authorized_inputs': task.get('input_bindings', []),
            'guidance': 'Read authorized input paths, create the test, then execute. An absent test is not absent user data.'}


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError('Review requires bounded nonempty ' + label)
    return value


def _ids(value, run):
    known = {r['id'] for r in run.get('requirements', [])}
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or v not in known for v in value):
        raise ValueError('Review must reference existing requirement IDs')
    return value


def assertion_batches(content):
    """Coverage inventory, not execution: require every visible assertion to be reviewed."""
    nodes = [n for n in ast.walk(ast.parse(content)) if isinstance(n, ast.Assert) or
             isinstance(n, ast.Call) and (
                 isinstance(n.func, ast.Attribute) and (n.func.attr.startswith('assert') or n.func.attr == 'raises') or
                 isinstance(n.func, ast.Name) and n.func.id.startswith('assert'))]
    nodes.sort(key=lambda n: (n.lineno, n.col_offset))
    items = [{'id': f'A{i+1}', 'line': node.lineno, 'source': ast.get_source_segment(content, node)}
             for i, node in enumerate(nodes)]
    return [items[i:i+6] for i in range(0, len(items), 6)] or [[]]


def review_target_sources(root, targets):
    """Give the reviewer bounded source evidence for the exact targets in the manifest."""
    remaining = 24_000
    sources = []
    for relative, sha256 in targets.items():
        with safe_path(root, relative).open('rb') as stream:
            data = stream.read(remaining + 1)
        complete = len(data) <= remaining
        if complete and digest(data) != sha256:
            raise ExecutionPreflightError('Verification target changed before test review: ' + relative)
        excerpt = data[:remaining]
        sources.append({'path': relative, 'sha256': sha256, 'complete': complete,
                        'content': excerpt.decode('utf-8', errors='replace')})
        remaining -= len(excerpt)
    return sources


def review_test(engine, run, task, check, path, targets, *, target_root=None):
    if not enabled(engine, run) or any(t['path'] == check['path'] for t in task.get('revision_tests', [])):
        return  # Frozen/approved scripts keep the existing exact-version approval path.
    # Read complete bounded test bytes; never silently certify a truncated script.
    content = path.read_text(encoding='utf-8')
    base = {'rules': rules(run), 'check': check, 'script': digest(path.read_bytes()), 'targets': targets}
    sources = review_target_sources(target_root, targets) if target_root is not None else []
    if sources:
        base['target_source_review'] = 1  # Reviews made without the actual source cannot be reused.
    covered = set()
    for batch in assertion_batches(content):
        key = signature(dict(base, assertion_batch=batch))
        reviewed = _review_test_batch(engine, run, task, check, content, key, batch, sources)
        covered.update(r for case in reviewed['cases'] for r in case['requirement_ids'])
    if not set(check.get('requirement_ids', [])) <= covered:
        raise TestExpectationError('Test expectation precheck: uncovered requirements: ' +
            ', '.join(sorted(set(check.get('requirement_ids', [])) - covered)) +
            '. Add checks for these declared behaviors; no Docker execution or business revision occurred.')


def _review_test_batch(engine, run, task, check, content, key, batch, sources):
    cache = task.setdefault('test_expectation_reviews', {})
    review = cache.get(key)
    if review is None:
        review = engine.model(run, 'review_test', {
            'agent': task['owner'], 'goal': run['goal'], 'check': check, 'test_path': check['path'], 'test_script': content,
            'assertion_batch': batch, 'target_sources': sources,
            'input_bindings': task.get('input_bindings', []),
            'instruction': 'Use the requirements for expected behavior. Use target_sources to confirm API, data paths, and test setup.'})
        if review.get('verdict') not in {'pass', 'revise', 'uncertain'}:
            raise ValueError('Test review verdict must be pass/revise/uncertain')
        _text(review.get('reason'), 'reason')
        cases = review.get('cases')
        if not isinstance(cases, list) or not 1 <= len(cases) <= 24:
            raise ValueError('Test review needs 1–24 concrete cases')
        covered = set()
        for case in cases:
            if not isinstance(case, dict): raise ValueError('Test review case must be an object')
            _ids(case.get('requirement_ids'), run)
            ids = case.get('assertion_ids', [])
            if batch and (not isinstance(ids, list) or not ids or
                          any(not isinstance(i, str) or i not in {a['id'] for a in batch} for i in ids)):
                raise TestExpectationError('Test review must cite the supplied assertion IDs, not invented or omitted IDs')
            covered.update(ids)
            for field in ('input', 'expected', 'asserted', 'reason'):
                _text(case.get(field), field)
            if type(case.get('consistent')) is not bool:
                raise ValueError('Test case consistency must be boolean')
        if review['verdict'] == 'pass' and not all(c['consistent'] for c in cases):
            # A summary cannot overrule its own concrete counterexample. Keep both for audit
            # and send the complete case back so the agent can actually repair its test.
            review = dict(review, reported_verdict='pass', verdict='revise')
        if review['verdict'] == 'pass' and not {a['id'] for a in batch} <= covered:
            raise TestExpectationError('Passing test review omitted assertions: ' +
                                      ', '.join(sorted({a['id'] for a in batch} - covered)))
        if review['verdict'] == 'revise' and all(c['consistent'] for c in cases):
            raise ValueError('Rejected test review identifies no concrete incorrect assertion, setup or required coverage gap')
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[key] = review
        engine.emit(run, task['owner'], 'test_expectation_reviewed',
                    {'task_id': task['id'], 'path': check['path'], 'key': key, 'review': review})
        engine.store.save(run)
    if review['verdict'] != 'pass':
        issues = [case for case in review['cases'] if not case['consistent']][:3]
        details = '\n'.join(
            f"{', '.join(case.get('assertion_ids', []))}: {case['reason'][:300]}" for case in issues)
        raise TestExpectationError(f"Test expectation precheck ({review['verdict']}): {review['reason'][:400]}\n"
            'No Docker check ran. Correct the specific unfrozen test issue before rerunning. Preserve valid assertions and coverage. '
            'If a requirement is genuinely ambiguous, ask one specific question.\n' + details)
    return review


def diagnosis_key(run, task):
    return signature({'rules': rules(run), 'failure': task.get('last_verification')})


def reviewed_kind(run, task):
    saved = task.get('reviewed_diagnosis') or {}
    return saved.get('decision', {}).get('failure_kind') if saved.get('key') == diagnosis_key(run, task) else None


def review_diagnosis(engine, run, task, decision, files, evidence):
    if not enabled(engine, run):
        return decision
    review = engine.model(run, 'review_diagnosis', {
        'agent': task['owner'], 'goal': run['goal'], 'candidate': decision, 'failure': evidence, 'files': files,
        'input_bindings': task.get('input_bindings', []),
        'instruction': 'Independently derive expected output, then classify; the proposed diagnosis may be wrong.'})
    kind = review.get('failure_kind')
    if kind not in {'business', 'test', 'path', 'environment', 'evidence', 'uncertain'}:
        raise ValueError('Invalid diagnosis review kind')
    _text(review.get('reason'), 'diagnosis reason')
    if kind in {'business', 'test'}:
        _ids(review.get('requirement_ids'), run)
        case = review.get('case')
        if not isinstance(case, dict): raise ValueError('Diagnosis review needs concrete case evidence')
        for name in ('input', 'expected', 'actual', 'asserted', 'requirement_quote'):
            _text(case.get(name), name)
        corpus = [run['goal']] + [r['text'] for r in run.get('requirements', [])]
        corpus += [str(c.get('answer', '')) for c in run.get('clarifications', [])]
        if not any(case['requirement_quote'] in text for text in corpus):
            raise ValueError('Diagnosis quote is not present in the actual requirements')
        actual_ok, test_ok = case.get('implementation_matches_requirement'), case.get('test_matches_requirement')
        expected_flags = (False, True) if kind == 'business' else (True, False)
        if type(actual_ok) is not bool or type(test_ok) is not bool or (actual_ok, test_ok) != expected_flags:
            raise ValueError('Diagnosis contradicts its expected/actual evidence; clarification required')
        if kind == 'business':
            owners = {b['task_id'] for b in task.get('input_bindings', [])}
            if review.get('target_task') not in owners:
                raise ValueError('Business diagnosis must identify an authorized source task')
    task['reviewed_diagnosis'] = {'key': diagnosis_key(run, task), 'decision': review}
    engine.emit(run, task['owner'], 'diagnosis_reviewed',
                {'task_id': task['id'], 'original_kind': decision.get('failure_kind'), 'decision': review})
    engine.store.save(run)
    return review
