from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path, PurePosixPath


AGENTS = ("coordinator", "researcher", "coder", "verifier")
TEXT_EXTENSIONS = {".md", ".txt", ".py", ".json", ".csv", ".toml", ".html", ".css", ".js"}


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def safe_path(root: Path, relative: str) -> Path:
    """Reject traversal, device names, alternate streams and symlinks, even internal ones."""
    if not isinstance(relative, str) or not relative or len(relative) > 240:
        raise ValueError("Invalid workspace path")
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    if PurePosixPath(normalized).is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") or ":" in part
        or part.endswith((" ", ".")) or re.search(r'[<>"|?*\x00-\x1f]', part)
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise ValueError("Path is outside the allowed workspace namespace")
    root = root.resolve()
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("Symlinks/junctions are not allowed")
        if candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Windows reparse points are not allowed")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Path escapes workspace")
    return candidate


class Store:
    def __init__(self, root: Path):
        self.team_id = None
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, agent TEXT, kind TEXT, data TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS retry_requests(source_id TEXT NOT NULL, request_key TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(source_id, request_key));
        """)
        self.db.commit()
        for agent in AGENTS:
            (self.root / "agents" / agent / "work").mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def lock(self):
        """OS-managed writer lock; automatically released on process termination."""
        with (self.root / "writer.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another EvoTask process is modifying this state directory") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, run: dict) -> None:
        run["updated"] = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)",
                            (run["id"], json.dumps(run, ensure_ascii=False)))

    def load(self, run_id: str) -> dict:
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id}")
        return json.loads(row[0])

    def runs(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM runs ORDER BY rowid DESC")]

    def event(self, run_id: str, agent: str, kind: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO events(run_id,agent,kind,data,created) VALUES (?,?,?,?,?)",
                            (run_id, agent, kind, json.dumps(data, ensure_ascii=False), time.time()))

    def events(self, run_id: str) -> list[dict]:
        return [dict(row) | {"data": json.loads(row["data"])} for row in self.db.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]

    def message(self, channel: str, sender: str, body: str) -> None:
        channel = self.scoped_channel(channel)
        with self.db:
            self.db.execute("INSERT INTO messages(channel,sender,body,created) VALUES (?,?,?,?)",
                            (channel, sender, body, time.time()))

    def messages(self, channel: str, reader: str = "human", limit: int = 20, *, metadata: bool = False) -> list[dict]:
        if reader != "human" and channel != "group" and channel != f"private:{reader}":
            raise PermissionError("Private conversation is not visible to this agent")
<<<<<<< HEAD
        columns = "seq,sender,body,created" if metadata else "sender,body"
        rows = self.db.execute(f"SELECT {columns} FROM messages WHERE channel=? ORDER BY seq DESC LIMIT ?",
                               (channel, limit)).fetchall()
=======
        rows = self.db.execute("SELECT sender,body FROM messages WHERE channel=? ORDER BY seq DESC LIMIT ?",
                               (self.scoped_channel(channel), limit)).fetchall()
>>>>>>> b8fb6de (增加自定义agent功能)
        return [dict(row) for row in reversed(rows)]

    def workspace(self, agent: str, run_id: str) -> Path:
        if agent not in AGENTS or not re.fullmatch(r"run_[a-f0-9]{12}", run_id):
            raise ValueError("Invalid agent or run ID")
        path = self.root / "agents" / agent / "work" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def bind_team(self, team_id: str | None) -> None:
        if team_id is not None and not re.fullmatch(r'team_[a-f0-9]{12}', team_id):
            raise ValueError('Invalid team ID')
        self.team_id = team_id

    def scoped_channel(self, channel: str) -> str:
        return f'team:{self.team_id}:{channel}' if self.team_id else channel

    def memory_path(self, agent: str) -> Path:
        if agent not in AGENTS:
            raise ValueError("Unknown agent")
        root = self.root / 'teams' / self.team_id if self.team_id else self.root
        return root / 'agents' / agent / 'memory.json'

    def memory(self, agent: str) -> list[dict]:
        path = self.memory_path(agent)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def remember(self, agent: str, entry: dict) -> None:
        entries = self.memory(agent)
        if not any(item["id"] == entry["id"] for item in entries):
            entries.append(entry)
            atomic_json(self.memory_path(agent), entries[-200:])

    def archive(self, run: dict, agent: str, relative: str, task_id: str, origin: str = 'archive') -> dict:
        path = safe_path(self.workspace(agent, run["id"]), relative)
        if path.suffix.lower() not in TEXT_EXTENSIONS or not path.is_file():
            raise ValueError("Only regular source/plain-text artifacts can be exported")
        if path.stat().st_size > run["config"]["max_file_bytes"]:
            raise ValueError("Artifact exceeds size limit")
        content = path.read_bytes()
        content.decode("utf-8")
        sha = digest(content)
        blob = self.root / "blobs" / sha
        blob.parent.mkdir(exist_ok=True)
        if not blob.exists():
            blob.write_bytes(content)
        entry = {"id": uid("art"), "task_id": task_id, "owner": agent, "path": relative,
                 "sha256": sha, "bytes": len(content), "verification": "unverified",
                 "origin": origin, "created": time.time(), "sequence": len(run["artifacts"]) + 1}
        run["artifacts"].append(entry)
        return entry

    def artifact_bytes(self, artifact: dict) -> bytes:
        data = (self.root / "blobs" / artifact["sha256"]).read_bytes()
        if digest(data) != artifact["sha256"]:
            raise ValueError("Artifact integrity check failed")
        return data

    def handoff(self, run: dict, recipient: str, artifact: dict) -> str:
        target_name = f"inputs/{artifact['task_id']}/{artifact['id']}_{Path(artifact['path']).name}"
        target = safe_path(self.workspace(recipient, run["id"]), target_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.artifact_bytes(artifact))
        self.event(run["id"], recipient, "artifact_handoff", {"artifact_id": artifact["id"], "path": target_name})
        return target_name

    def snapshot(self, run: dict, task: dict) -> list[dict]:
        """Keep bounded partial artifacts even when a task did not call finish."""
        result = []
        root = self.workspace(task["owner"], run["id"])
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "inputs"
                       and not (Path(directory) / d).is_symlink()]
            for name in sorted(files):
                if len(result) >= 30:
                    return result
                try:
                    relative = (Path(directory) / name).relative_to(root).as_posix()
                    result.append(self.archive(run, task["owner"], relative, task["id"], origin="failure_snapshot"))
                except (OSError, ValueError, UnicodeError):
                    continue
        return result
