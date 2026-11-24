"""Opt-in small real-model smoke test; isolated data, no publishing or automatic approvals.

Run explicitly: python -m tests.live_interaction
Uses at most 19 API calls (one chat + a run capped at 18).
"""
import json
from dataclasses import replace
from pathlib import Path

from evotask.config import Config
from evotask.engine import Engine
from evotask.interaction import chinese_prose
from evotask.providers import APIProvider
from evotask.storage import Store, uid
from evotask.teams import Teams


def main():
    config = replace(Config.load(), max_model_calls=18, max_steps=12, max_task_steps=8,
                     active_timeout=240, api_timeout=60)
    provider = APIProvider(config)
    root = Path('test-results/interaction') / uid('live')
    store = Store(root)
    report = {'provider': config.provider, 'model': config.model, 'test_data': 'fictional onboarding note'}
    try:
        roles = {slot: {'name': name, 'instructions': f'You are the {name}. Communicate in English.'}
                 for slot, name in [('coordinator', 'Project manager'), ('researcher', 'UX researcher'),
                                    ('coder', 'Technical writer'), ('verifier', 'Independent reviewer')]}
        team = Teams(store).create('Language policy smoke test', roles)
        result = provider.complete('chat', {'agent': 'coder', 'team': team, 'role_profile': roles['coder'],
                                           'history': [{'sender': 'human', 'body': 'Please introduce yourself briefly in English.'}]})
        chinese_prose(result['reply'], '真实模型聊天')
        report['chat'] = result['reply']
        print('Chat: Chinese reply received.', flush=True)
        engine = Engine(store, config, provider)
        goal = ('Prepare a short onboarding note for a fictional internal prototype, saved as onboarding.md. '
                'The audience is essential but not chosen: ask me to choose between nontechnical users and developers '
                'before writing, with brief choices. Then write a welcome, three usage steps and a limitations note. '
                'No research, code, deployment, web lookup or human acceptance checks are needed for this fictional test. '
                'I request English output.')
        run = engine.advance(engine.new(goal, team_id=team['id']))
        report['first_status'] = run['status']
        report['question'] = run.get('pending')
        assert run['pending'] and run['pending']['kind'] in {'planning', 'clarification'}, run.get('failure')
        assert 2 <= len(run['pending']['options']) <= 3
        print('Question: ' + run['pending']['question'], flush=True)
        # Answer only the fictional audience preference; never approve an action or acceptance check.
        run = engine.resume(run['id'], run['pending']['id'], run['pending']['options'][0])
        report.update(run_id=run['id'], status=run['status'], model_calls=run['model_calls'], failure=run['failure'])
        report['artifacts'] = [{k: a[k] for k in ('id', 'path', 'sha256', 'verification')} for a in run['artifacts']]
        assert run['status'] == 'succeeded', (run['status'], run.get('failure'), run.get('pending'))
        artifact = next(a for a in run['artifacts'] if a['path'] == 'onboarding.md' and a['verification'] == 'verified')
        content = store.artifact_bytes(artifact).decode('utf-8')
        chinese_prose(content, '真实模型交付物')
        report['deliverable'] = content
        report['passed'] = True
        print(f"PASS: English roles/input -> Chinese question, chat and artifact; {run['model_calls']} run calls.", flush=True)
    finally:
        (root / 'smoke-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Evidence: ' + str(root / 'smoke-result.json'), flush=True)
        store.close()


if __name__ == '__main__':
    main()
