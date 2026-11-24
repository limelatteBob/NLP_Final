"""Audited corrections of declarative browser/section checks; changes require exact human approval."""
import copy
import difflib
import json
from .storage import digest

def signature(check):
    return digest(json.dumps(check,ensure_ascii=False,sort_keys=True).encode())

def propose(engine,run,task,action):
    from .engine import validate_checks
    index=action.get('check_index'); check=action.get('check');reason=action.get('reason')
    if task['role']!='verifier' or type(index) is not int or not 0<=index<len(task['checks']):
        raise ValueError('Declarative correction needs verifier and valid check_index')
    original=task['checks'][index]
    if original['kind'] not in {'browser','sections','contains'} or not isinstance(reason,str) or not reason.strip():
        raise ValueError('Only browser/sections/contains checks may be corrected with a reason')
    validate_checks([check])
    for field in ('kind','path','source_task','targets','requirement_ids'):
        if original.get(field)!=check.get(field):raise ValueError('Cannot change check ownership, kind or requirement coverage: '+field)
    if signature(original)==signature(check):raise ValueError('Proposed check has no changes')
    if task.get('check_revision_proposals',0)>=3:raise ValueError('Declarative correction proposal budget exhausted')
    task['check_revision_proposals']=task.get('check_revision_proposals',0)+1
    old=json.dumps(original,ensure_ascii=False,indent=2);new=json.dumps(check,ensure_ascii=False,indent=2)
    prepared={'check_index':index,'old_check':copy.deepcopy(original),'old_hash':signature(original),'check':check,'reason':reason,
              'diff':''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='original-check',tofile='proposed-check'))}
    engine.wait(run,task,'check_revision','验收检查与实际界面或需求不一致。是否批准以下检查修订？修订后必须重新验证。',action,prepared)

def apply(engine,run,task,pending,reply):
    if reply.lower() not in {'yes','no'}:raise ValueError('Check revision reply must be yes or no')
    p=pending['prepared'];index=p['check_index']
    if signature(task['checks'][index])!=p['old_hash']:raise ValueError('Check changed after proposal; stale approval rejected')
    if reply.lower()=='yes':task['checks'][index]=copy.deepcopy(p['check'])
    record={'approved':reply.lower()=='yes','old_check':p['old_check'],'new_check':task['checks'][index],'reason':p['reason'],'request_id':pending['id']}
    task['history'].append({'event':'check_revision_reviewed',**record})
    engine.emit(run,'human','check_revision_reviewed',dict(record,task_id=task['id']))
    task['error_streak']=0
