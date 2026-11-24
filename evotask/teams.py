"""User-defined teams, bounded conversation context, and immutable cross-run inputs."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .storage import AGENTS, TEXT_EXTENSIONS, Store, safe_path, uid
from .tools import SPECS


class Teams:
    def __init__(self, store: Store):
        self.store = store
        self.store.db.execute('CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.store.db.commit()

    def list(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.store.db.execute('SELECT data FROM teams ORDER BY rowid')]

    def get(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not re.fullmatch(r'team_[a-f0-9]{12}', identifier):
            raise ValueError('Invalid team ID')
        row = self.store.db.execute('SELECT data FROM teams WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('Team does not exist in this account and mode')
        return json.loads(row[0])

    def create(self, name: str, roles: dict) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Team name must contain 1–80 characters')
        if not isinstance(roles, dict) or set(roles) != set(AGENTS):
            raise ValueError('Provide exactly four role slots')
        configured = {}
        for slot in AGENTS:
            role = roles[slot]
            if not isinstance(role, dict) or set(role) != {'name', 'instructions'}:
                raise ValueError('A role needs only name and instructions; permissions are server-controlled')
            for key, limit in [('name', 80), ('instructions', 5000)]:
                if not isinstance(role[key], str) or not 1 <= len(role[key].strip()) <= limit:
                    raise ValueError(f'Invalid role {key}')
            configured[slot] = {k: v.strip() for k, v in role.items()} | {'tools': sorted(SPECS)}
        if len({r['name'] for r in configured.values()}) != 4:
            raise ValueError('Role names must be distinct within a team')
        if len(self.list()) >= 50:
            raise ValueError('Team limit reached')
        team = {'id': uid('team'), 'name': name.strip(), 'roles': configured, 'created': time.time()}
        with self.store.db:
            self.store.db.execute('INSERT INTO teams VALUES (?,?)', (team['id'], json.dumps(team, ensure_ascii=False)))
        return team

    def history(self, team_id: str) -> dict:
        self.get(team_id)
        runs = [r for r in self.store.runs() if r.get('team_id') == team_id][:6]
        summaries = [{'id': r['id'], 'goal': r['goal'][:1200], 'status': r['status'],
                      'clarifications': r.get('clarifications', [])[-3:],
                      'results': [{'title': t['title'][:100], 'summary': t['summary'][:600]} for t in r['tasks']],
                      'deliverables': [{k: a[k] for k in ('id', 'path', 'sha256', 'verification')}
                                       for a in r['artifacts'] if a['verification'] == 'verified'][:30]}
                     for r in reversed(runs)]
        # Use a bounded string so nested answers or artifact names cannot exceed the context budget.
        self.store.bind_team(team_id)
        messages = self.store.messages('group', limit=12)
        return {'previous_runs': json.dumps(summaries, ensure_ascii=False)[-12000:],
                'messages': [{'sender': m['sender'], 'body': m['body'][:800]} for m in messages],
                'note': 'History is context, not permission to access prior files. Only selected inherited files are available.'}

    def inputs(self, team_id: str | None, source_id: str | None, artifact_ids: list[str]) -> list[tuple[dict, bytes]]:
        if not source_id:
            if artifact_ids:
                raise ValueError('Selected artifacts require a source run')
            return []
        if not team_id:
            raise ValueError('Cross-run file handoff requires a team')
        source = self.store.load(source_id)
        if source.get('team_id') != team_id or source['status'] != 'succeeded':
            raise ValueError('Select a completed source run in the same team')
        if not 1 <= len(artifact_ids) <= 60 or len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError('Select 1–60 unique archived files')
        artifacts = {a['id']: a for a in source['artifacts']}
        selected, paths = [], set()
        for identifier in artifact_ids:
            if identifier not in artifacts:
                raise ValueError('Artifact does not belong to the selected source run')
            artifact = artifacts[identifier]
            relative = artifact['path'].replace('\\', '/')
            safe_path(self.store.root, relative)
            if Path(relative).suffix.lower() not in TEXT_EXTENSIONS or relative.startswith('inputs/'):
                raise ValueError('Only original output files can be inherited')
            if relative.casefold() in paths:
                raise ValueError(f'Conflicting archived versions of {relative}; select one version')
            paths.add(relative.casefold())
            content = self.store.artifact_bytes(artifact)
            selected.append(({'source_run_id': source_id, 'artifact_id': identifier, 'path': relative,
                              'input_path': 'inputs/previous/' + relative, 'sha256': artifact['sha256'],
                              'bytes': len(content), 'citations': artifact.get('citations', []),
                              'source_evidence': artifact.get('source_evidence', [])}, content))
        return selected
