"""Read-only conversation view over actual chat messages and execution events.

Execution cards are controller-authored status summaries, not invented model dialogue.
Existing messages, events and private-channel visibility remain unchanged.
"""
import json
from functools import partial
from pathlib import Path

from .interaction import task_text

EVENT_KINDS = (
    'run_created', 'plan_created', 'task_assigned', 'delegated', 'artifact_handoff',
    'task_succeeded', 'attempt_failed', 'waiting_human', 'human_reply',
    'revision_requested', 'run_finished', 'run_cancelled',
)


def team_messages(store, run_id=None, limit=200, team_id=None):
    placeholders = ','.join('?' for _ in EVENT_KINDS)
    clause = " AND run_id IN (SELECT id FROM runs WHERE json_extract(data, '$.team_id') IS ?)"
    clause += ' AND run_id=?' if run_id else ''
    args = (*EVENT_KINDS, team_id, run_id) if run_id else (*EVENT_KINDS, team_id)
    events = [dict(row) for row in store.db.execute(
        f'SELECT * FROM events WHERE kind IN ({placeholders}){clause} ORDER BY seq DESC LIMIT 800', args)]
    events.reverse()
    # Fetch operational messages around the selected event interval to enrich legacy events.
    earliest = min((e['created'] for e in events), default=0)
    raw = [dict(row) for row in store.db.execute(
        "SELECT seq,sender,body,created FROM messages WHERE channel=? AND created>=? ORDER BY seq DESC LIMIT 1600", (store.scoped_channel('group'), earliest - 5,))] if events else []
    runs = {}; result = []; consumed = set()

    def load(identifier):
        if identifier not in runs:
            runs[identifier] = store.load(identifier)
        return runs[identifier]

    for event in events:
        run = load(event['run_id'])
        text = partial(task_text, run)
        data = json.loads(event['data'])
        tasks = {t['id']: t for t in run['tasks']}
        task_id = data.get('task_id') or data.get('to_task') or data.get('target_task')
        task = tasks.get(task_id, {})
        title = data.get('title') or task.get('title') or text('子任务', 'Subtask')
        sender = event['agent'] or 'coordinator'
        kind = event['kind']
        part = 0

        def add(body, author=sender, recipient=None, label=None, card_kind=kind):
            nonlocal part
            part += 1
            result.append({'id': f"event:{event['seq']}:{part}", 'sender': author,
                           'recipient': recipient, 'body': body, 'created': event['created'],
                           'source': 'execution', 'kind': card_kind, 'label': label or text('执行动态', 'Task update'),
                           'run_id': run['id'], 'run_goal': run['goal'], 'task_id': task_id,
                           'task_title': title if task_id else None, '_order': (event['seq'], part)})

        def legacy(prefix=None, exact=None, author=sender):
            # Only consume an operational message correlated to this same event and sender.
            for message in sorted(raw, key=lambda item: abs(item['created'] - event['created'])):
                if message['seq'] in consumed or message['sender'] != author:
                    continue
                if abs(message['created'] - event['created']) > 5:
                    continue
                if message['body'] == exact or prefix and message['body'].startswith(prefix):
                    consumed.add(message['seq'])
                    return message['body']
            return None

        if kind == 'run_created':
            legacy(exact=run['goal'], author='human')
            add(run['goal'], author='human', label=text('任务目标', 'Task goal'))
        elif kind == 'plan_created':
            planned = data.get('tasks', [])
            lines = [f"{n}. {t['title']}" for n, t in enumerate(planned, 1)]
            add(text('已拆分任务，按依赖关系安排执行：\n', 'Planned tasks in dependency order:\n') + '\n'.join(lines),
                author='coordinator', label=text('协作计划', 'Task plan'))
        elif kind == 'task_assigned':
            legacy(exact=text(f"接受任务 {task_id}: {title}", f"Accepted task {task_id}: {title}"))
            objective = data.get('objective') or task.get('objective', '')
            add(text(f"请负责「{title}」。", f"Please take on ‘{title}’.") + ('\n' + objective if objective else ''),
                author='coordinator', recipient=sender, label=text('分配任务', 'Assignment'), card_kind='assignment')
            add(text(f"收到，开始处理「{title}」。", f"Accepted ‘{title}’ and starting work."),
                recipient='coordinator', label=text('已接收任务', 'Task accepted'), card_kind='acknowledgement')
        elif kind == 'delegated':
            add(text(f"需要协助处理「{title}」，已提交子任务委派。", f"Delegated ‘{title}’ for assistance."),
                recipient=data.get('role'), label=text('委派协作', 'Delegation'))
        elif kind == 'artifact_handoff':
            artifact = next((a for a in run['artifacts'] if a['id'] == data.get('artifact_id')), None)
            if artifact:
                task_id = artifact['task_id']
                title = tasks.get(task_id, {}).get('title', '资料交接')
                filename = Path(artifact['path']).name
                add(text(f"已交接「{filename}」，可继续后续工作。", f"Handed off ‘{filename}’ for follow-up work."),
                    author=artifact['owner'], recipient=sender, label=text('资料交接', 'File handoff'))
        elif kind == 'task_succeeded':
            prefix = text(f"任务 {task_id} 通过声明的检查：", f"Task {task_id} passed the declared checks: ")
            previous = legacy(prefix=prefix)
            summary = data.get('summary') or (previous[len(prefix):] if previous else '')
            add(text(f"「{title}」已通过声明的检查。", f"‘{title}’ passed the declared checks.") + ('\n' + summary if summary else ''),
                recipient='coordinator', label=text('完成反馈', 'Task completed'))
        elif kind == 'attempt_failed':
            legacy(prefix=text(f"任务 {task_id} 尝试失败：", f"Task {task_id} attempt failed: "))
            error = data.get('observed_error', text('请查看任务记录', 'See the task record'))
            add(text(f"「{title}」执行遇到问题：\n", f"‘{title}’ encountered a problem:\n") + error[:1500],
                recipient='coordinator', label=text('问题反馈', 'Task problem'))
        elif kind == 'waiting_human':
            add(data.get('question', text('需要你的确认', 'Your input is needed')),
                recipient='human', label=text('等待确认', 'Waiting for input'))
        elif kind == 'human_reply':
            add(data.get('reply', ''), author='human', recipient='coordinator', label=text('用户回复', 'User reply'))
        elif kind == 'revision_requested':
            number, target = data.get('round'), data.get('target_task')
            legacy(prefix=text(f"第 {number} 轮返修：退回 {target}。", f"Revision round {number}: returned {target} to the author."))
            add(text('请根据验证反馈修订：\n', 'Please revise based on verification feedback:\n') + data.get('reason', ''),
                recipient=task.get('owner'), label=text('返修反馈', 'Revision feedback'))
        elif kind == 'run_cancelled':
            add(text('已请求停止任务。', 'Stop requested.'), author='human', label=text('停止任务', 'Stop task'))
        elif kind == 'run_finished':
            status = {'succeeded': text('本次任务已完成，交付文件可在任务详情查看。', 'Task completed. See task details for the deliverables.'),
                      'failed': text('本次任务未完成，请查看问题记录。', 'Task failed. See the task record for details.'),
                      'cancelled': text('本次任务已停止。', 'Task stopped.')}
            add(status.get(data.get('status'), text('本次执行已结束。', 'Run ended.')),
                author='coordinator', label=text('任务状态', 'Run status'))

    if not run_id:
        # Keep ordinary conversations, including older ones when there are few/no events.
        recent = store.db.execute("SELECT seq,sender,body,created FROM messages WHERE channel=? ORDER BY seq DESC LIMIT ?", (store.scoped_channel('group'), limit,))
        for row in recent:
            message = dict(row)
            if message['seq'] not in consumed:
                result.append({'id': f"message:{message['seq']}", 'source': 'chat', 'kind': 'chat',
                               **message, '_order': (message['seq'], 0)})
    result.sort(key=lambda item: (item['created'], item['_order']))
    for item in result:
        item.pop('_order')
    return result[-limit:]
