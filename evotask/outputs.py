"""Output contracts and read-only artifact presentation; internal paths never change."""
import re
from pathlib import Path
from .storage import safe_path


def validate_outputs(value):
    if value is None:
        return []
    if not isinstance(value,list) or len(value)>15:
        raise ValueError('outputs must contain at most 15 declarations')
    paths=set(); result=[]
    for item in value:
        if not isinstance(item,dict): raise ValueError('Invalid output declaration')
        path=item.get('path',''); safe_path(Path.cwd(),path)
        if path.replace('\\','/').startswith('inputs/') or path in paths:
            raise ValueError('Output path must be unique and owned by this task')
        if item.get('kind') not in {'deliverable','process'}: raise ValueError('Output kind must be deliverable or process')
        if not isinstance(item.get('title'),str) or not 1<=len(item['title'])<=120: raise ValueError('Output title required')
        if type(item.get('order')) is not int or not 1<=item['order']<=999: raise ValueError('Output order must be 1..999')
        summary=item.get('summary','')
        if not isinstance(summary,str) or len(summary)>500: raise ValueError('Output summary too long')
        paths.add(path); result.append({k:item[k] for k in ('path','kind','title','order')}|{'summary':summary})
    return result



def validate_output_contract(tasks):
    """Require explicit intent on a newly generated real-model plan."""
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Plan must contain tasks with output declarations')
    declarations = []
    for task in tasks:
        if not isinstance(task, dict) or 'outputs' not in task or task['outputs'] is None:
            raise ValueError('Every planned task must declare outputs')
        declarations.extend(validate_outputs(task['outputs']))
    if not any(output['kind'] == 'deliverable' for output in declarations):
        raise ValueError('Plan must declare at least one deliverable')


def describe_artifacts(run, events=()):
    tasks={t['id']:t for t in run['tasks']}
    times={}
    failures=set()
    for event in events:
        data=event.get('data',{})
        for identifier in data.get('artifact_ids',[]) + data.get('artifacts',[]):
            if isinstance(identifier,str):
                times.setdefault(identifier,event.get('created'))
                if event['kind']=='attempt_failed': failures.add(identifier)
    pending=(run.get('pending') or {}).get('prepared') or {}
    pending_ids={a['id'] for a in pending.get('artifacts',[])}
    chains={}; records=[]
    for sequence,original in enumerate(run['artifacts'],1):
        a=dict(original); task=tasks.get(a['task_id'],{})
        chain=chains.setdefault((a['task_id'],a['path']),[])
        version=1 if not chain else chain[-1]['version']+(chain[-1]['sha256']!=a['sha256'])
        declared=next((o for o in task.get('outputs',[]) if o['path']==a['path']),None)
        origin=a.get('origin','failure_snapshot' if a['id'] in failures else 'legacy')
        purpose=declared['kind'] if declared else ('process' if run.get('artifact_schema')==2 or origin=='failure_snapshot' else 'unknown')
        a.update(version=version,sequence=a.get('sequence',sequence),created=a.get('created',times.get(a['id'])),
                 purpose=purpose,origin=origin,title=declared['title'] if declared else Path(a['path']).name,
                 summary=declared.get('summary','') if declared else '',delivery_order=declared['order'] if declared else 999,
                 task_title=task.get('title',a['task_id']),is_current=False,duplicate_of=None)
        a['_authoritative']=a['id'] in task.get('artifact_ids',[]) or a['id'] in pending_ids
        chain.append(a); records.append(a)
    for chain in chains.values():
        candidates=[a for a in chain if a['_authoritative'] and a['verification'] not in {'invalid','superseded'}]
        if not candidates: candidates=[a for a in chain if a['verification']=='verified']
        if not candidates: candidates=[a for a in chain if a['origin']!='failure_snapshot' and a['verification'] not in {'invalid','superseded'}]
        current=candidates[-1] if candidates else chain[-1]
        groups={}
        for a in chain: groups.setdefault(a['version'],[]).append(a)
        for group in groups.values():
            representative=max(group,key=lambda a:(a['id']==current['id'],a['verification']=='verified',a['sequence']))
            for a in group:
                a['is_current']=a['version']==current['version']
                if a is not representative: a['duplicate_of']=representative['id']
                if a['origin']=='failure_snapshot' and a['verification']!='verified': section='process'
                elif not a['is_current'] or a['verification']=='superseded': section='history'
                elif a['purpose']=='unknown': section='unknown'
                else: section='deliverable' if a['purpose']=='deliverable' else 'process'
                a['section']=section
                if a['verification']=='superseded': state='历史版本 · 已被替代'
                elif a['verification']=='invalid' or a['origin']=='failure_snapshot' and a['verification']!='verified': state='失败过程文件 · 未通过验收'
                elif run['status'] in {'failed','cancelled'}: state='部分结果 · 整项任务未完成'
                elif a['id'] in pending_ids or run['status']=='waiting_human': state='待确认'
                elif a['verification']=='verified' and run['status']=='succeeded': state='已通过声明的检查'
                else: state='待验收'
                a['status_label']=state
    # IDs remain API identities; readable filenames use titles and small collision suffixes.
    task_numbers = {task_id: n for n, task_id in enumerate(tasks, 1)}
    bases = {}
    for a in records:
        del a['_authoritative']
        ext = Path(a['path']).suffix
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', a['title']).strip(' .') or '文件'
        if ext and title.lower().endswith(ext.lower()):
            title = title[:-len(ext)] or '文件'
        title = title[:48]
        while len(title.encode('utf-8')) > 144:
            title = title[:-1]
        prefix = f"{a['delivery_order']:02d}_" if a['purpose'] == 'deliverable' else ''
        base = f"{prefix}{title}_v{a['version']}"
        bases[a['id']] = (base, ext)
    collisions = {}
    for a in records:
        base, ext = bases[a['id']]
        collisions.setdefault((base + ext).casefold(), set()).add((a['task_id'], a['path']))
    used = set()
    # Give the visible representative the shortest name, even if its duplicate was archived first.
    for a in sorted(records, key=lambda item: bool(item['duplicate_of'])):
        base, ext = bases[a['id']]
        if len(collisions[(base + ext).casefold()]) > 1:
            base += f"_任务{task_numbers.get(a['task_id'], 0)}"
        name = base + ext
        copy_number = 2
        while name.casefold() in used:
            name = f"{base}_副本{copy_number}{ext}"
            copy_number += 1
        used.add(name.casefold())
        a['download_name'] = name
        a['display_name'] = name
    return records


def grouped_artifacts(records):
    groups={key:[] for key in ('deliverable','process','history','unknown')}
    for a in records:
        if not a['duplicate_of']: groups[a['section']].append(a)
    groups['deliverable'].sort(key=lambda a:(a['delivery_order'],a['sequence']))
    groups['history'].sort(key=lambda a:a['sequence'],reverse=True)
    return groups
