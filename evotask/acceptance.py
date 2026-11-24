"""Plan contracts, evidence binding and observed failure classification."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .storage import safe_path, digest, TEXT_EXTENSIONS
from .collaboration import ancestors


def html_headings(content):
    from html.parser import HTMLParser
    class Headings(HTMLParser):
        def __init__(self):super().__init__();self.level=None;self.text=[];self.items=set()
        def handle_starttag(self,tag,attrs):
            if tag in {'h1','h2','h3','h4','h5','h6'}:self.level=tag;self.text=[]
        def handle_data(self,data):
            if self.level:self.text.append(data)
        def handle_endtag(self,tag):
            if tag==self.level:self.items.add(re.sub(r'\s+',' ',''.join(self.text).strip()).casefold());self.level=None
    parser=Headings();parser.feed(content);return parser.items

def requirements(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise ValueError('Plan requirements must contain 1–24 explicit user requirements')
    result = []
    for item in value:
        if (not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z][\w-]{0,39}', str(item.get('id','')))
                or not isinstance(item.get('text'), str) or not 1 <= len(item['text']) <= 2000
                or item.get('priority', 'must') not in {'must','optional'}):
            raise ValueError('Requirement needs id, text and priority=must|optional')
        result.append({'id':item['id'], 'text':item['text'], 'priority':item.get('priority','must')})
    if len({r['id'] for r in result}) != len(result):
        raise ValueError('Duplicate requirement IDs')
    return result


def validate_contract(tasks, required=None):
    by_id = {t['id']:t for t in tasks}
    covered = set()
    for task in tasks:
        declared = {o['path'] for o in task.get('outputs',[])}
        unsupported = [p for p in declared if Path(p).suffix.lower() not in TEXT_EXTENSIONS]
        if unsupported:
            raise ValueError(f"{task['id']}: unsupported archived output extensions {unsupported}; SQLite .db is runtime only. Declare portable JSON/CSV export plus schema/reconstruction code instead.")
        for check in task['checks']:
            ids = check.get('requirement_ids',[])
            if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):
                raise ValueError('requirement_ids must be a list of requirement IDs')
            covered.update(ids)
            if not isinstance(check.get('targets',[]),list) or any(not isinstance(t,dict) for t in check.get('targets',[])):
                raise ValueError('targets must be a list of task_id/path objects')
            if task['role']=='verifier' and check['kind']=='python' and task['depends_on'] and not check.get('targets'):
                raise ValueError('Independent Python checks need explicit upstream targets')
            if check['kind'] == 'human':
                continue
            source = check.get('source_task')
            if source:
                if source not in ancestors(tasks, task['id']):
                    raise ValueError(f"{task['id']}: source_task {source} must be an ancestor")
                if check['path'] not in {o['path'] for o in by_id[source].get('outputs',[])}:
                    raise ValueError(f"{task['id']}: {source}/{check['path']} is not a declared upstream output")
                if check['kind'] in {'python','review'}:
                    raise ValueError('Tests/review reports must be owned by the verifier; use targets for upstream files')
            elif task.get('outputs') and check['path'] not in declared:
                raise ValueError(f"{task['id']}: check path {check['path']} is not its declared output. Declare the test/report, or specify source_task for an upstream file.")
            for target in check.get('targets',[]):
                source = target.get('task_id')
                if source not in (ancestors(tasks, task['id']) | {task['id']}) or target.get('path') not in {o['path'] for o in by_id[source].get('outputs',[])}:
                    raise ValueError(f"{task['id']}: invalid verification target {target}")
            if task['role']=='verifier' and check['kind']=='contains' and check.get('text') in {'通过','成功','pass','passed','success'}:
                raise ValueError('Do not require a positive verdict literal; use a review JSON with verdict pass/fail/blocked')
    if required:
        known = {r['id'] for r in required}
        missing = {r['id'] for r in required if r['priority']=='must'} - covered
        if missing or not covered <= known:
            raise ValueError(f'Requirement coverage invalid: missing={sorted(missing)}, unknown={sorted(covered-known)}. Link checks using requirement_ids.')


def file_manifest(root, paths):
    result = {}
    for relative in paths:
        path = safe_path(root, relative)
        if not path.is_file():
            raise ValueError(f'Verification evidence file missing: {relative}')
        result[relative] = digest(path.read_bytes())
    return result


def fingerprint(category, message, script_hash=''):
    normalized = re.sub(r'art_[a-f0-9]+|run_[a-f0-9]+|\bline \d+\b', '<version>', message)
    return digest(json.dumps([category,normalized[:1800],script_hash],ensure_ascii=False).encode())


def classify_failure(message, result=None):
    result = result or {}
    if 'NoTestsExecuted' in message or any(f.get('type')=='NoTestsExecuted' for f in result.get('verification',{}).get('failures',[])):
        return 'test_script'
    if any(s in message for s in ('SyntaxError','IndentationError','NameError')):
        return 'test_or_code_syntax'
    if any(s in message for s in ('FileNotFoundError','ModuleNotFoundError','ImportError','missing ', 'existing workspace .py')):
        return 'input_or_path'
    if 'AssertionError' in message:
        return 'assertion_needs_diagnosis'
    if any(s in message for s in ('Browser runtime unavailable','sandbox_unavailable','timed out','timeout','Model API','ProviderUnavailable')):
        return 'environment'
    if any(s in message for s in ('ActionProtocolError','Invalid arguments','Invalid agent action','Model response truncated','JSON chat-completions protocol')):
        return 'protocol'
    if 'AssertionError' in message or 'Independent Python check failed' in message:
        return 'assertion_needs_diagnosis'
    if 'HTTP redirects' in message or 'HTTP Error' in message:
        return 'environment'
    if 'sources' in message or 'evidence' in message.lower():
        return 'evidence'
    return 'unclassified'


class ReviewBlocked(ValueError):
    def __init__(self, value):
        self.review=value
        super().__init__('Review blocked by missing evidence: '+json.dumps(value,ensure_ascii=False)[:1800])

def read_review(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('verdict') not in {'pass','fail','blocked'}:
        raise ValueError('Review JSON needs verdict=pass|fail|blocked')
    if not isinstance(value.get('findings'), list) or not isinstance(value.get('requirements'), list):
        raise ValueError('Review needs findings and requirements arrays, with evidence and limitations')
    if not value['requirements'] or any(not isinstance(r,dict) or not isinstance(r.get('id'),str)
        or r.get('status') not in {'pass','fail','blocked'} or not isinstance(r.get('evidence'),list)
        or (r.get('status')=='pass' and not r['evidence'])
        or any(not isinstance(e,dict) or not isinstance(e.get('path'),str) or not isinstance(e.get('sha256'),str) for e in r['evidence'])
        for r in value['requirements']):
        raise ValueError('Review needs nonempty requirement results with actual file/hash evidence for every pass')
    if len({r['id'] for r in value['requirements']}) != len(value['requirements']):
        raise ValueError('Duplicate review requirement IDs')
    if value['verdict']=='blocked':
        raise ReviewBlocked(value)
    if value['verdict'] != 'pass':
        raise ValueError('Review did not pass: ' + json.dumps(value,ensure_ascii=False)[:1800])
    if any(r.get('status') != 'pass' for r in value['requirements'] if r.get('priority','must')=='must'):
        raise ValueError('Review cannot pass with unmet mandatory requirements')
    return value
