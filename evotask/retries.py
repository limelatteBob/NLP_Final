"""Fresh-run retries. The caller holds the workspace writer lock for creation."""
import copy
import json
import shutil
import time
from pathlib import Path
from .engine import Engine
from .storage import AGENTS, safe_path, uid


def existing_retry(store, source, key):
    row=store.db.execute('SELECT run_id FROM retry_requests WHERE source_id=? AND request_key=?',(source['id'],key)).fetchone()
    if row: return store.load(row[0])
    children=[r for r in store.runs() if r.get('retry_of')==source['id'] and r['status'] not in {'succeeded','failed','cancelled'}]
    if children:
        child=children[0]
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO retry_requests VALUES (?,?,?)',(source['id'],key,child['id']))
        return child
    return None


def create_retry(store, config, source, key):
    if source['status']!='failed': raise ValueError('只能重新运行已经失败的任务')
    if len(store.runs())>=200: raise ValueError('当前空间已达到 200 次任务上限')
    inputs = source.get('initial_inputs', [])
    if len(inputs) < len(source.get('uploaded_files', [])) or any(
            item['path'] not in inputs for item in source.get('input_manifest', [])):
        raise ValueError('原任务输入快照不完整，请重新上传后创建任务')
    paths=[]; names=[]
    root=store.workspace('coordinator',source['id'])
    for relative in source.get('initial_inputs',[]):
        path=safe_path(root,relative)
        if not path.is_file(): raise ValueError('原任务输入资料缺失，请重新上传后创建任务')
        paths.append(path)
        names.append(next((i['name'] for i in source.get('input_manifest',[]) if i['path']==relative),Path(relative).name.split('_',1)[-1]))
    for upload in source.get('uploaded_files',[]):
        folder=safe_path(store.root,'uploads/'+upload['id'])
        if not (folder/'metadata.json').is_file() or not (folder/('original'+Path(upload['name']).suffix)).is_file():
            raise ValueError('原任务附件原件缺失，请重新上传后创建任务')
    engine=Engine(store,config,None)
    engine.tools.kb.validate_bases(source.get('knowledge_base_ids'))
    identifier=uid('run')
    try:
        child=engine.new(source['goal'],paths,source['memory_enabled'],source.get('require_review',False),
                         input_names=names,knowledge_base_ids=copy.deepcopy(source.get('knowledge_base_ids')),
                         persist=False,run_id=identifier)
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
