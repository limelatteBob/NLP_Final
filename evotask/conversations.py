"""Read-only conversation view over actual chat messages and execution events.

Execution cards are controller-authored status summaries, not invented model dialogue.
Existing messages, events and private-channel visibility remain unchanged.
"""
import json
from pathlib import Path

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
        data = json.loads(event['data'])
        tasks = {t['id']: t for t in run['tasks']}
        task_id = data.get('task_id') or data.get('to_task') or data.get('target_task')
        task = tasks.get(task_id, {})
        title = data.get('title') or task.get('title') or '子任务'
        sender = event['agent'] or 'coordinator'
        kind = event['kind']
        part = 0

        def add(body, author=sender, recipient=None, label=None, card_kind=kind):
            nonlocal part
            part += 1
            result.append({'id': f"event:{event['seq']}:{part}", 'sender': author,
                           'recipient': recipient, 'body': body, 'created': event['created'],
                           'source': 'execution', 'kind': card_kind, 'label': label or '执行动态',
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
            add(run['goal'], author='human', label='任务目标')
        elif kind == 'plan_created':
            planned = data.get('tasks', [])
            lines = [f"{n}. {t['title']}" for n, t in enumerate(planned, 1)]
            add('已拆分任务，按依赖关系安排执行：\n' + '\n'.join(lines), author='coordinator', label='协作计划')
        elif kind == 'task_assigned':
            legacy(exact=f"接受任务 {task_id}: {title}")
            objective = data.get('objective') or task.get('objective', '')
            add(f"请负责「{title}」。" + ('\n' + objective if objective else ''),
                author='coordinator', recipient=sender, label='分配任务', card_kind='assignment')
            add(f"收到，开始处理「{title}」。", recipient='coordinator', label='已接收任务', card_kind='acknowledgement')
        elif kind == 'delegated':
            add(f"需要协助处理「{title}」，已提交子任务委派。", recipient=data.get('role'), label='委派协作')
        elif kind == 'artifact_handoff':
            artifact = next((a for a in run['artifacts'] if a['id'] == data.get('artifact_id')), None)
            if artifact:
                task_id = artifact['task_id']
                title = tasks.get(task_id, {}).get('title', '资料交接')
                add(f"已交接「{Path(artifact['path']).name}」，可继续后续工作。",
                    author=artifact['owner'], recipient=sender, label='资料交接')
        elif kind == 'task_succeeded':
            prefix = f"任务 {task_id} 通过声明的检查："
            previous = legacy(prefix=prefix)
            summary = data.get('summary') or (previous[len(prefix):] if previous else '')
            add(f"「{title}」已通过声明的检查。" + ('\n' + summary if summary else ''),
                recipient='coordinator', label='完成反馈')
        elif kind == 'attempt_failed':
            legacy(prefix=f"任务 {task_id} 尝试失败：")
            add(f"「{title}」执行遇到问题：\n{data.get('observed_error', '请查看任务记录')[:1500]}",
                recipient='coordinator', label='问题反馈')
        elif kind == 'waiting_human':
            add(data.get('question', '需要你的确认'), recipient='human', label='等待确认')
        elif kind == 'human_reply':
            add(data.get('reply', ''), author='human', recipient='coordinator', label='用户回复')
        elif kind == 'revision_requested':
            legacy(prefix=f"第 {data.get('round')} 轮返修：退回 {data.get('target_task')}。")
            add('请根据验证反馈修订：\n' + data.get('reason', ''), recipient=task.get('owner'), label='返修反馈')
        elif kind == 'run_cancelled':
            add('已请求停止任务。', author='human', label='停止任务')
        elif kind == 'run_finished':
            status = {'succeeded': '本次任务已完成，交付文件可在任务详情查看。',
                      'failed': '本次任务未完成，请查看问题记录。', 'cancelled': '本次任务已停止。'}
            add(status.get(data.get('status'), '本次执行已结束。'), author='coordinator', label='任务状态')

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
