"""Re-run frozen independent tests against an author's current files in a disposable Docker workspace."""
from pathlib import Path
import tempfile
from .storage import safe_path, digest, TEXT_EXTENSIONS

def verify_revision(store, sandbox, run, task):
    feedback=task.get('revision_feedback') or {}
    if task['role']=='verifier' and task.get('revision_tests'):
        root=store.workspace(task['owner'],run['id']);results=[]
        for test in task['revision_tests']:
            if digest(safe_path(root,test['path']).read_bytes())!=test['sha256']:
                raise ValueError('Frozen test changed')
            result=sandbox.verify(root,test['path'])
            results.append({'path':test['path'],'test_sha256':test['sha256'],'result':result})
        return {'success':all(r['result']['success'] for r in results),'tests':results,'mappings':task.get('input_bindings',[]),
                'output':'\n'.join(r['result']['output'] for r in results)}
    if feedback.get('target_task')!=task['id'] or not feedback.get('frozen_tests'):
        raise ValueError('No frozen repair package for this task')
    reviewer=next(t for t in run['tasks'] if t['id']==feedback['from_task'])
    artifacts={a['id']:a for a in run['artifacts']}
    author_root=store.workspace(task['owner'],run['id'])
    results=[]; mappings=[]
    with tempfile.TemporaryDirectory(prefix='evotask-repair-') as directory:
        root=Path(directory)
        # Copy only explicitly authorized archived QA inputs; replace the target's aliases with current author bytes.
        for binding in reviewer.get('input_bindings',[]):
            artifact=artifacts[binding['artifact_id']]
            content=store.artifact_bytes(artifact)
            if binding['task_id']==task['id']:
                candidate=safe_path(author_root,binding['artifact_path'])
                if not candidate.is_file():raise ValueError('Repair target missing: '+binding['artifact_path'])
                content=candidate.read_bytes()
                mappings.append({'author_path':binding['artifact_path'],'test_input':binding['path'],'sha256':digest(content)})
            destination=safe_path(root,binding['path']);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(content)
        # Supporting QA text fixtures are immutable snapshots in this temporary sandbox.
        qa_root=store.workspace(reviewer['owner'],run['id'])
        for p in qa_root.rglob('*'):
            if not p.is_file() or p.suffix.lower() not in TEXT_EXTENSIONS or 'inputs' in p.relative_to(qa_root).parts:continue
            relative=p.relative_to(qa_root).as_posix();destination=safe_path(root,relative)
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(p.read_bytes())
        for frozen in feedback['frozen_tests']:
            content=store.artifact_bytes(artifacts[frozen['artifact_id']])
            if digest(content)!=frozen['sha256']:raise ValueError('Frozen test hash mismatch')
            destination=safe_path(root,frozen['path']);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(content)
            result=sandbox.verify(root,frozen['path'])
            results.append({'path':frozen['path'],'test_sha256':frozen['sha256'],'result':result})
    return {'success':bool(results) and all(r['result']['success'] for r in results),'tests':results,'mappings':mappings,
            'output':'\n'.join(r['result']['output'] for r in results)}
