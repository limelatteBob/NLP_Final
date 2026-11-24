"""Fresh-run retries. The caller holds the workspace writer lock for creation."""
import copy
import json
import shutil
import time
from pathlib import Path
from .engine import Engine
from .storage import AGENTS, safe_path, uid, digest


def visible_runs(runs):
    """Present the latest attempt in the original task's position; keep audit rows."""
    by_id = {r['id']: r for r in runs}
    replaced = set()
    for run in runs:
        parent = by_id.get(run.get('retry_of') or run.get('rerun_of'))
        if parent and parent.get('team_id') == run.get('team_id'):
            replaced.add(parent['id'])
    def position(run):
        seen = set()
        while run['id'] not in seen:
            seen.add(run['id'])
            parent = by_id.get(run.get('retry_of') or run.get('rerun_of'))
            if not parent or parent.get('team_id') != run.get('team_id'):
                break
            run = parent
        return run.get('created', 0)
    return sorted((r for r in runs if r['id'] not in replaced), key=position, reverse=True)


def existing_retry(store, source, key):
    row=store.db.execute('SELECT run_id FROM retry_requests WHERE source_id=? AND request_key=?',(source['id'],key)).fetchone()
    if row: return store.load(row[0])
    children=[r for r in store.runs() if (r.get('retry_of') or r.get('rerun_of'))==source['id'] and r.get('team_id') == source.get('team_id')]
    if children:
        child=children[0]
        seen = {source['id']}
        while child.get('replaced_by') and child['id'] not in seen:
            seen.add(child['id'])
            child = store.load(child['replaced_by'])
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO retry_requests VALUES (?,?,?)',(source['id'],key,child['id']))
        return child
    return None


def create_retry(store, config, source, key, *, allow_cancelled=False):
    if source['status'] not in ({'failed', 'cancelled'} if allow_cancelled else {'failed'}): raise ValueError('只能重新运行已经失败的任务')
    if len(store.runs())>=200: raise ValueError('当前空间已达到 200 次任务上限')
    inputs = source.get('initial_inputs', [])
    if len(inputs) < len(source.get('uploaded_files', [])) or any(
            item.get('input_path', item.get('path')) not in inputs for item in source.get('input_manifest', [])):
        raise ValueError('原任务输入快照不完整，请重新上传后创建任务')
    paths=[]; names=[]; snapshots=[]
    root=store.workspace('coordinator',source['id'])
    for relative in source.get('initial_inputs',[]):
        path=safe_path(root,relative)
        if not path.is_file(): raise ValueError('原任务输入资料缺失，请重新上传后创建任务')
        paths.append(path)
        metadata = next((i for i in source.get('input_manifest', []) if i.get('input_path', i.get('path')) == relative), None)
        content = path.read_bytes()
        if metadata and metadata.get('sha256') and digest(content) != metadata['sha256']:
            raise ValueError('Original input snapshot has changed')
        names.append(metadata.get('name', Path(relative).name) if metadata else Path(relative).name.split('_', 1)[-1])
        snapshots.append((dict(copy.deepcopy(metadata or {}), input_path=relative, sha256=digest(content)), content))
    for upload in source.get('uploaded_files',[]):
        folder=safe_path(store.root,'uploads/'+upload['id'])
        if not (folder/'metadata.json').is_file() or not (folder/('original'+Path(upload['name']).suffix)).is_file():
            raise ValueError('原任务附件原件缺失，请重新上传后创建任务')
    engine=Engine(store,config,None)
    engine.tools.kb.validate_bases(source.get('knowledge_base_ids'))
    identifier=uid('run')
    try:
        child=engine.new(source['goal'],[] if source.get('team_id') or source.get('inherited_inputs') else paths,source['memory_enabled'],source.get('require_review',False),
                         input_names=names,knowledge_base_ids=copy.deepcopy(source.get('knowledge_base_ids')),
                         persist=False,run_id=identifier,
                         team_id=source.get('team_id'), team_snapshot=copy.deepcopy(source.get('team_snapshot')),
                         pinned_inputs=snapshots if source.get('team_id') or source.get('inherited_inputs') else None)
        child.update(retry_of=source['id'],retry_root=source.get('retry_root',source['id']),
                     uploaded_files=copy.deepcopy(source.get('uploaded_files',[])),
                     clarifications=copy.deepcopy(source.get('clarifications',[])))
        for task in source['tasks']:
            for item in task.get('history',[]):
                reply=item.get('result',{}).get('human_reply')
                if reply and item.get('action',{}).get('type')=='ask':
                    child['clarifications'].append({'question':item['action'].get('question',''),'answer':reply})
        parent=copy.deepcopy(source)
        parent.setdefault('retry_children',[]).append(child['id'])
        parent['replaced_by'] = child['id']
        child['attempt_number'] = source.get('attempt_number', 1) + 1
        child['display_created'] = source.get('display_created', source['created'])
        now=time.time(); child['updated']=now; parent['updated']=now
        with store.db:
            store.db.execute('INSERT INTO runs VALUES (?,?)',(child['id'],json.dumps(child,ensure_ascii=False)))
            store.db.execute('UPDATE runs SET data=? WHERE id=?',(json.dumps(parent,ensure_ascii=False),parent['id']))
            store.db.execute('INSERT INTO retry_requests VALUES (?,?,?)',(source['id'],key,child['id']))
            for rid,kind,data in ((source['id'],'run_retried',{'new_run_id':child['id']}),(child['id'],'run_created',{'retry_of':source['id']})):
                store.db.execute('INSERT INTO events(run_id,agent,kind,data,created) VALUES (?,?,?,?,?)',
                                 (rid,'coordinator',kind,json.dumps(data),now))
        return child
    except Exception:
        # Only remove newly allocated, uncommitted directories, never source data.
        if not store.db.execute('SELECT 1 FROM runs WHERE id=?',(identifier,)).fetchone():
            for agent in AGENTS:
                folder=(store.root/'agents'/agent/'work'/identifier).resolve()
                if folder.is_relative_to(store.root.resolve()) and folder.name==identifier and folder.is_dir():
                    shutil.rmtree(folder)
        raise
