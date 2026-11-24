"""Opt-in live assessment driver. Posts original task text through the normal Web UI.

No canned task answers: inspect status, then pass a reply written for the actual question.
Credentials and browser state stay under ignored test-results/, never in source control.
"""
import argparse
import hashlib
import json
import re
import secrets
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from tests.browser_teams import fill_team


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'test-results' / 'mid-test' / 'live'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create', 'start', 'status', 'reply', 'approve', 'reject', 'export', 'package', 'rerun', 'chat', 'repair'])
    parser.add_argument('--scenario', type=int, choices=[1, 2, 3], required=True)
    parser.add_argument('--task', type=int, choices=[1, 2], default=1)
    parser.add_argument('--reply-file', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    state_file, registry_file = OUT / 'browser-state.json', OUT / 'teams.json'
    registry = json.loads(registry_file.read_text()) if registry_file.exists() else {}
    folder = next((ROOT / 'Mid_Test').glob(f'scenario_{args.scenario}_*'))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(storage_state=str(state_file) if state_file.exists() else None,
                                      viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        page = context.new_page()
        try:
            page.goto(args.url + '/#chat')
            if not state_file.exists():
                creds = {'username': 'midtest_' + secrets.token_hex(4), 'password': secrets.token_urlsafe(24)}
                (OUT / 'local-login.json').write_text(json.dumps(creds), encoding='utf-8')
                page.locator('[data-action=account]').first.click()
                page.locator('[data-action=account-register-tab]').click()
                page.locator('#account-username').fill(creds['username'])
                page.locator('#account-password').fill(creds['password'])
                page.locator('#account-confirm').fill(creds['password'])
                page.get_by_role('button', name='创建账号并登录').click()
                expect(page.locator('#account-auth-form')).to_have_count(0)
                if page.locator('[data-action=close-modal]:visible').count():
                    page.locator('[data-action=close-modal]:visible').first.click()
            page.locator('#mode-select').select_option('live')
            if args.command == 'create':
                if str(args.scenario) in registry:
                    raise ValueError('Team already exists; reuse it for both tasks')
                text = (folder / '01_role_initialization.md').read_text(encoding='utf-8')
                names = re.findall(r'^## (.*?) \(', text, flags=re.M)
                duties = re.findall(r'```text\s*\n(.*?)\n```', text, flags=re.S)
                mapping = ['coordinator', 'coder', 'verifier', 'researcher'] if args.scenario == 1 else ['coordinator', 'researcher', 'verifier', 'coder']
                roles = {slot: {'name': name, 'instructions': duty} for slot, name, duty in zip(mapping, names, duties)}
                page.goto(args.url + '/#chat')
                fill_team(page, f'Mid Test {args.scenario} · {text.splitlines()[0].split(": ", 1)[1]}', roles)
                team_id = page.url.rsplit('/', 1)[1]
                registry[str(args.scenario)] = team_id
                registry_file.write_text(json.dumps(registry, indent=2), encoding='utf-8')
                print('Created through UI:', team_id)
            else:
                team_id = registry[str(args.scenario)]
                page.goto(args.url + '/#team/' + team_id)
                expect(page.locator('#team-message')).to_be_visible()
                if args.command == 'start':
                    text = next(folder.glob(f'0{args.task + 1}_task_{args.task}.md')).read_text(encoding='utf-8')
                    goal = re.search(r'```text\s*\n(.*?)\n```', text, flags=re.S)[1]
                    if args.scenario == 1 and args.task == 2:
                        page.get_by_role('button', name='基于这些文件发起新任务').first.click()
                        page.locator('#team-next-goal').fill(goal)
                        page.get_by_role('button', name='创建并启动新任务').click()
                        expect(page.locator('#team-inherit-form')).to_have_count(0)
                    else:
                        page.locator('#team-message').fill(goal)
                        page.get_by_role('button', name='发起任务', exact=True).click()
                        expect(page.locator('#team-message')).to_have_value('', timeout=150000)
                    print('Posted original Task', args.task, 'through UI')
                elif args.command == 'repair':
                    page.get_by_role('link', name='查看任务详情 →').first.click()
                    page.get_by_role('button', name='修复并复验', exact=True).click()
                    page.get_by_role('button', name='开始修复并复验').click()
                    expect(page.get_by_role('button', name='开始修复并复验')).to_have_count(0)
                elif args.command == 'rerun':
                    page.get_by_role('button', name='重新运行', exact=True).first.click()
                    page.get_by_role('button', name='开始重新运行', exact=True).click()
                    expect(page.get_by_role('button', name='开始重新运行', exact=True)).to_have_count(0)
                elif args.command == 'chat':
                    if not args.reply_file: raise ValueError('Supply contextual chat text')
                    page.locator('#team-message').fill(args.reply_file.read_text(encoding='utf-8'))
                    page.get_by_role('button', name='发送消息', exact=True).click()
                    expect(page.locator('#team-message')).to_have_value('', timeout=150000)
                elif args.command == 'reply':
                    if not args.reply_file: raise ValueError('Supply a reply file for the actual pending question')
                    card = page.locator('[data-team-run]').first
                    card.locator('[data-team-reply] textarea').fill(args.reply_file.read_text(encoding='utf-8'))
                    card.get_by_role('button', name='回复并继续').click()
                    print('Submitted contextual reply through UI')
                elif args.command == 'approve':
                    page.locator('[data-team-run]').first.get_by_role('button', name='确认通过', exact=True).click()
                    print('Approved reviewed deliverable through UI')
                elif args.command == 'reject':
                    page.locator('[data-team-run]').first.get_by_role('button', name='拒绝', exact=True).click()
                    print('Rejected deliverable through UI; review comments retained in group chat')
                elif args.command == 'package':
                    destination = OUT / f'scenario-{args.scenario}'; destination.mkdir(exist_ok=True)
                    with page.expect_download() as download:
                        page.get_by_role('link', name='导出评测包 ZIP', exact=True).click()
                    package = destination / 'evaluation.zip'; download.value.save_as(package)
                    with zipfile.ZipFile(package) as archive:
                        manifest = json.loads(archive.read('manifest.json'))
                        assert set(archive.namelist()) == {f['path'] for f in manifest['files']} | {'manifest.json'}
                        for file in manifest['files']:
                            assert hashlib.sha256(archive.read(file['path'])).hexdigest() == file['sha256']
                    print('Packaged', len(manifest['runs']), 'runs and', len(manifest['artifacts']), 'artifact versions to', package)
                elif args.command == 'export':
                    destination = OUT / f'scenario-{args.scenario}'; destination.mkdir(exist_ok=True)
                    with page.expect_download() as download:
                        page.get_by_role('link', name='导出完整团队记录').click()
                    download.value.save_as(destination / 'audit.json')
                    audit = json.loads((destination / 'audit.json').read_text(encoding='utf-8'))
                    for entry in audit['runs']:
                        run = entry['run']
                        for a in run['artifacts']:
                            response = page.request.get(args.url + f"/api/runs/{run['id']}/artifacts/{a['id']}?mode=live&download=true")
                            assert response.ok, f"Artifact download failed: {a['id']} ({response.status})"
                            content = response.body()
                            assert hashlib.sha256(content).hexdigest() == a['sha256'], 'Downloaded artifact hash mismatch'
                            target = destination / run['id'] / 'archive' / a['id'] / a['path']
                            if not target.resolve().is_relative_to(destination.resolve()): raise ValueError('Unsafe artifact path')
                            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
                            if a['verification'] == 'verified':
                                latest = destination / run['id'] / a['path']
                                latest.parent.mkdir(parents=True, exist_ok=True); latest.write_bytes(content)
                    page.screenshot(path=str(destination / 'team.png'), full_page=True)
                    print('Exported', len(audit['runs']), 'runs to', destination)
                data = page.request.get(args.url + '/api/teams/' + team_id + '?mode=live').json()
                print('Run history:', [(r['id'], r['status']) for r in data['runs']])
                for run in data['runs'][:1]:
                    pending = {k: run['pending'][k] for k in ('id', 'task_id', 'kind', 'question')} if run['pending'] else None
                    print(json.dumps({'id': run['id'], 'status': run['status'], 'calls': run['model_calls'],
                        'executing': run['executing'], 'pending': pending, 'failure': run['failure'],
                        'tasks': run['tasks'], 'artifacts': [{'id': a['id'], 'path': a['path'], 'verification': a['verification']} for a in run['artifacts']]}, ensure_ascii=False))
        finally:
            context.storage_state(path=str(state_file))
            browser.close()


if __name__ == '__main__':
    main()
