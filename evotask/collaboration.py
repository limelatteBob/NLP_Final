"""Controller-owned contracts, evidence and revision helpers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


ACTION_TYPES = {'tool', 'ask', 'delegate', 'finish', 'fail', 'request_revision'}
TOOL_ARGUMENTS = {
    'list_files': (set(), set()),
    'read_file': ({'path'}, {'path', 'offset', 'limit'}),
    'write_file': ({'path', 'content'}, {'path', 'content'}),
    'update_file': ({'path', 'old', 'new'}, {'path', 'old', 'new'}),
    'delete_file': ({'path'}, {'path'}),
    'retrieve_documents': ({'query'}, {'query', 'limit'}),
    'web_search': ({'query'}, {'query'}), 'fetch_url': ({'url'}, {'url'}),
    'python_run': ({'path'}, {'path'}),
    'git_publish': ({'paths'}, {'paths', 'branch'}),
}
ACTION_HELP = ('Return exactly one action with type tool, ask, delegate, finish, fail or request_revision. '
               'For a file write use {"type":"tool","name":"write_file",'
               '"args":{"path":"output.md","content":"..."}}. '
               'Use only tools from context; inspect previous results before retrying.')


class ActionProtocolError(ValueError):
    """Malformed model decision; no tool has been executed."""


def normalize_action(value: dict) -> dict:
    """Adapt unambiguous tool shorthand, without granting any tool permissions."""
    def invalid(reason):
        raise ActionProtocolError(f'{reason}. {ACTION_HELP}')

    if not isinstance(value, dict):
        invalid('Agent action must be a JSON object')
    if 'action' in value:
        if not (set(value) <= {'purpose', 'action'} and isinstance(value['action'], dict)):
            invalid('Conflicting action envelope')
        if value.get('purpose', 'act') != 'act':
            invalid('Action envelope has the wrong purpose')
        value = value['action']
        if 'action' in value:
            invalid('Nested action envelopes are not supported')
    kind = value.get('type')
    if not isinstance(kind, str):
        invalid('Agent action needs a string type')
    if kind in TOOL_ARGUMENTS:
        required, allowed = TOOL_ARGUMENTS[kind]
        if set(value) == {'type', 'args'}:
            args = value['args']
        else:
            args = {k: v for k, v in value.items() if k != 'type'}
        if not isinstance(args, dict) or not required <= args.keys() or not args.keys() <= allowed:
            invalid(f'Invalid or conflicting arguments for {kind}; required={sorted(required)}, allowed={sorted(allowed)}')
        return {'type': 'tool', 'name': kind, 'args': args}
    if kind not in ACTION_TYPES:
        invalid(f'Invalid agent action type: {kind[:80]!r}')
    if kind == 'tool' and (not set(value) <= {'type', 'name', 'args'}
                           or not isinstance(value.get('name'), str)
                           or not isinstance(value.get('args', {}), dict)):
        invalid('Tool action needs name and args, with no extra action fields')
    return value


def ancestors(tasks: list[dict], identifier: str) -> set[str]:
    by_id = {t['id']: t for t in tasks}
    found, pending = set(), list(by_id[identifier]['depends_on'])
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(by_id[current]['depends_on'])
    return found


def descendants(tasks: list[dict], identifier: str) -> set[str]:
    return {t['id'] for t in tasks if identifier in ancestors(tasks, t['id'])}


def valid_source_url(value: str) -> str:
    if not value:
        return ''
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('Source URL must be an HTTP(S) URL without whitespace')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError('Source URL must be an HTTP(S) URL without credentials') from None
    return value


def urls_in(text: str) -> list[str]:
    urls = []
    for match in re.findall(r'https?://[^\s<>"\[\]]+', text):
        value = match.rstrip(').,;:!?，。；：！？）')
        try:
            if valid_source_url(value) not in urls:
                urls.append(value)
        except ValueError:
            pass
    return urls
