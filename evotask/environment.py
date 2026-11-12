"""Minimal dotenv reader: UTF-8, no shell execution, expansion or multiline values."""
from __future__ import annotations

import re
from pathlib import Path


def read_env(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    source = Path(path)
    if not source.exists():
        return {}
    values = {}
    for number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        key, separator, raw = text.partition("=")
        key, raw = key.strip(), raw.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment at line {number}; values omitted")
        if key in values:
            raise ValueError(f"Duplicate .env variable at line {number}; values omitted")
        if raw.startswith(('"', "'")):
            end = raw.find(raw[0], 1)
            if end < 0 or (raw[end + 1:].strip() and not raw[end + 1:].lstrip().startswith("#")):
                raise ValueError(f"Invalid quoted .env value at line {number}; values omitted")
            value = raw[1:end]
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        values[key] = value
    return values
