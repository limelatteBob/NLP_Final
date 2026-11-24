"""Portable evaluation evidence built from persisted records and immutable artifacts."""
from __future__ import annotations

import html
import json
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone

from .engine import Engine
from .storage import AGENTS, Store, digest, safe_path
from .teams import Teams


COVERAGE = {
    'persisted_team_group_messages': 'all',
    'persisted_team_runs_events_task_histories': 'all',
    'archived_artifact_versions': 'all, including failed, unverified and superseded versions',
    'raw_model_api_transcript': False,
    'private_or_other_team_conversations': False,
    'original_uploads_and_full_knowledge_bases': False,
    'note': 'Persisted evidence only. Previously unrecorded or truncated content cannot be reconstructed.',
}


class EvaluationExportError(ValueError):
    """Refuse an incomplete or unsafe bundle before starting the download."""


def team_audit(store: Store, team_id: str, mode: str) -> dict:
    teams = Teams(store)
    store.bind_team(team_id)
    # One SQLite read snapshot; exporting must not reserve a job or pause a running task.
    with store.db:
        store.db.execute('BEGIN')
        team = teams.get(team_id)
        runs = [r for r in reversed(store.runs()) if r.get('team_id') == team_id]
        messages = [dict(row) for row in store.db.execute(
            'SELECT * FROM messages WHERE channel=? ORDER BY seq', (store.scoped_channel('group'),))]
        result = {'team': team, 'mode': mode, 'exported': time.time(), 'messages': messages,
                  'runs': [{'run': r, 'metrics': Engine.metrics(r), 'events': store.events(r['id'])} for r in runs]}
    result['memory'] = {agent: store.memory(agent) for agent in AGENTS}
    return result


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def utc(value) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else '未记录'


def literal(value: str, language: str = 'text') -> str:
    # User text may itself contain fenced code blocks; preserve it without breaking the transcript.
    fence = '`' * max(3, 1 + max((len(s) for s in re.findall(r'`+', value)), default=0))
    return f'{fence}{language}\n{value}\n{fence}\n'


def label(value) -> str:
    return html.escape(str(value)).replace('\r', ' ').replace('\n', ' ')


def dialogue_markdown(audit: dict) -> str:
    team = audit['team']
    lines = [f"# 团队评测记录：{label(team['name'])}", '',
             f"导出时间（UTC）：{utc(audit['exported'])}；模式：{audit['mode']}。", '',
             '本文件完整呈现本次快照中已持久化的群聊、事件和任务历史，不另行截断正文。',
             '它不是逐次 LLM API 原始转录；不含其他团队或私聊。历史上未记录或已截断的内容无法追补。',
             '运行中、失败或待人工状态均按原记录保留；verified 仅表示通过声明的检查。', '',
             '## 团队与当前角色', '', literal(json_text(team), 'json'),
             '## 群聊与运行事件时间线', '',
             '按已记录时间排序。事件与群聊可能描述同一次操作；它们是不同的原始记录，并非额外执行。', '']
    timeline = []
    for message in audit['messages']:
        timeline.append((message['created'], 0, message['seq'], 'message', message, None))
    for entry in audit['runs']:
        for event in entry['events']:
            timeline.append((event['created'], 1, event['seq'], 'event', event, entry['run']))
    for stamp, _, seq, kind, record, run in sorted(timeline, key=lambda item: item[:3]):
        roles = (run.get('team_snapshot') or team)['roles'] if run else team['roles']
        sender = record.get('sender', record.get('agent', 'system'))
        name = '用户' if sender == 'human' else roles.get(sender, {}).get('name', sender)
        lines.extend([f"### {utc(stamp)} · {label(name)}（{label(sender)}）", ''])
        if kind == 'message':
            lines.extend([f'群聊记录 #{seq}', '', literal(record['body'])])
        else:
            lines.extend([f"运行 `{run['id']}` · 事件 #{seq} · {label(record['kind'])}", '',
                          literal(json_text(record['data']), 'json')])
    if not timeline:
        lines.extend(['暂无群聊或事件。', ''])
    lines.extend(['## 每次运行与任务历史', '',
                  '任务历史按各自保存的顺序列出；没有单独时间戳的步骤不推测其跨任务先后关系。', ''])
    for entry in audit['runs']:
        run = entry['run']
        lines.extend([f"### 运行 {run['id']} · {label(run['status'])}", '', '目标：', '', literal(run['goal'])])
        details = {key: run.get(key) for key in ('created', 'ended', 'failure', 'pending', 'inflight',
                   'clarifications', 'team_snapshot', 'rerun_of', 'inherited_inputs', 'input_manifest', 'revisions')}
        lines.extend(['状态、角色快照、澄清与输入来源：', '', literal(json_text(details), 'json'),
                      '运行指标：', '', literal(json_text(entry['metrics']), 'json')])
        for task in run['tasks']:
            lines.extend([f"#### 子任务 {task['id']} · {label(task['title'])}", '',
                          literal(json_text({k: v for k, v in task.items() if k != 'history'}), 'json')])
            for number, step in enumerate(task.get('history', []), 1):
                lines.extend([f'任务内记录 {number}：', '', literal(json_text(step), 'json')])
        lines.extend(['归档文件及原始验证状态（实际字节见 outputs/，路径映射见 manifest.json）：', '',
                      literal(json_text(run['artifacts']), 'json')])
    lines.extend(['## 团队经验快照', '', literal(json_text(audit['memory']), 'json')])
    return '\n'.join(lines)


README = """# EvoTask 团队评测包

建议将本包解压后，把 dialogue.md、manifest.json 和相关 outputs/ 文件一起提供给评审模型。
audit.json 保留机器可读的原始审计结构，适合追查细节；本导出功能不调用付费模型或自动评分。

## 文件说明

- dialogue.md：已保存群聊与事件的时间线、角色、目标、澄清、任务动作与工具结果、失败与人工状态。
- audit.json：完整持久化审计快照，包括运行记录、事件、指标与团队经验。
- manifest.json：运行状态、各版本的原路径、包内路径、SHA-256、字节数和验证状态。
- outputs/运行ID/产物ID/原路径：所有归档版本的原始字节，不覆盖同名文件、不只挑选成功结果。

manifest.artifacts 的 current_task_output 表示任务当前选定的交付版本，不表示整题已经验收。
应结合 run_status、task_status 和 verification 判断。缺少归档或哈希不符时会中止导出，
不会生成一个默默缺失文件的评测包。manifest.files 的哈希覆盖包内其余文件，清单本身不自我哈希。
"""


def evaluation_zip(store: Store, audit: dict):
    """Return a rewound temporary ZIP; the caller owns and must close the stream."""
    stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    manifest = {'format': 'evotask-evaluation-bundle', 'version': 1, 'team_id': audit['team']['id'],
                'mode': audit['mode'], 'exported_at_utc': utc(audit['exported']), 'coverage': COVERAGE,
                'runs': [], 'artifacts': [], 'files': []}
    names = set()
    try:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def write(name: str, content: bytes, kind: str):
                if name.casefold() in names:
                    raise EvaluationExportError('归档路径冲突，无法生成完整评测包。')
                names.add(name.casefold())
                archive.writestr(name, content)
                manifest['files'].append({'path': name, 'kind': kind, 'bytes': len(content), 'sha256': digest(content)})

            write('README.md', README.encode('utf-8'), 'guide')
            write('dialogue.md', dialogue_markdown(audit).encode('utf-8'), 'dialogue')
            write('audit.json', json_text(audit).encode('utf-8'), 'audit')
            for entry in audit['runs']:
                run = entry['run']
                if not re.fullmatch(r'run_[a-f0-9]{12}', run['id']):
                    raise EvaluationExportError('运行 ID 无效，无法生成评测包。')
                manifest['runs'].append({key: run.get(key) for key in ('id', 'goal', 'status', 'created', 'ended',
                                        'failure', 'rerun_of')} | {'metrics': entry['metrics'], 'pending': run['pending']})
                tasks = {task['id']: task for task in run['tasks']}
                for artifact in run['artifacts']:
                    try:
                        if (not re.fullmatch(r'art_[a-f0-9]{12}', artifact['id'])
                                or not re.fullmatch(r'[a-f0-9]{64}', artifact['sha256'])):
                            raise ValueError('Invalid artifact metadata')
                        original = artifact['path'].replace('\\', '/')
                        safe_path(store.root, original)
                        safe_path(store.root, 'blobs/' + artifact['sha256'])
                        content = store.artifact_bytes(artifact)
                        if len(content) != artifact['bytes']:
                            raise ValueError('Size mismatch')
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        raise EvaluationExportError(f"产物 {artifact.get('id', '(unknown)')} 缺失、路径无效或校验失败，已中止导出。") from exc
                    name = f"outputs/{run['id']}/{artifact['id']}/{original}"
                    write(name, content, 'artifact')
                    task = tasks.get(artifact['task_id'], {})
                    manifest['artifacts'].append({'run_id': run['id'], 'run_status': run['status'],
                        'artifact_id': artifact['id'], 'task_id': artifact['task_id'], 'task_status': task.get('status'),
                        'owner': artifact['owner'], 'original_path': artifact['path'], 'archive_path': name,
                        'sha256': artifact['sha256'], 'bytes': len(content), 'verification': artifact['verification'],
                        'verification_scope': artifact.get('verification_scope'),
                        'current_task_output': artifact['id'] in task.get('artifact_ids', [])})
            archive.writestr('manifest.json', json_text(manifest).encode('utf-8'))
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def zip_chunks(stream):
    try:
        while chunk := stream.read(65536):
            yield chunk
    finally:
        stream.close()
