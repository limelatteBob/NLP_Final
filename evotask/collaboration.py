"""Controller-owned contracts, evidence and revision helpers."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def normalize_action(value: dict) -> dict:
    # Accept the common API envelope, but never guess between conflicting actions.
    if 'type' not in value and set(value) <= {'purpose', 'action'} and isinstance(value.get('action'), dict):
        if value.get('purpose', 'act') != 'act':
            raise ValueError('Action envelope has the wrong purpose')
        return value['action']
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
