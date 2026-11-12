from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .gittools import GitPublisher, github_target
from .network import fetch_text, search_web
from .retrieval import KnowledgeBase
from .sandbox import DockerSandbox
from .storage import Store, TEXT_EXTENSIONS, digest, safe_path, uid


COMMON = {"list_files", "read_file", "write_file", "update_file", "delete_file", "retrieve_documents"}
CAPABILITIES = {
    "coordinator": COMMON,
    "researcher": COMMON | {"web_search", "fetch_url"},
    "coder": COMMON | {"python_run", "git_publish"},
    "verifier": COMMON | {"python_run"},
}
SPECS = {
    "list_files": "{} -> list own workspace files",
    "read_file": "{path, offset?: character offset, limit?: 1..16000 characters} -> UTF-8 text page; continue with next_offset when truncated=true",
    "write_file": "{path, content: UTF-8 text} -> create/replace own .md/.txt/.py/.json/.csv/.toml file",
    "update_file": "{path, old: exact unique text, new: replacement} -> replace once",
    "delete_file": "{path} -> approval required, version pinned deletion",
    "retrieve_documents": "{query, limit?: 1..8} -> authorized passages with citation IDs",
    "web_search": "{query} -> internet search; default Wikipedia only, Brave configurable",
    "fetch_url": "{url: public HTTPS URL} -> untrusted page text, no redirects/PDF/Office",
    "python_run": "{path: .py file} -> execute in isolated Docker, no host fallback; standalone assertions for tests",
    "git_publish": "{paths: list of own output files, branch?: evotask/name} -> preview, approve, commit and push pinned snapshots",
}


class Tools:
    def __init__(self, store: Store, config: Config, sandbox=None):
        self.store, self.config = store, config
        self.kb = KnowledgeBase(store)
        self.sandbox = sandbox or DockerSandbox(config)
        self.git = GitPublisher(store, config)

    def specs(self, agent: str) -> dict:
        return {name: SPECS[name] for name in sorted(CAPABILITIES[agent])}

    def validate(self, agent: str, name: str, args: dict) -> None:
        if name not in CAPABILITIES[agent]:
            raise PermissionError(f"{agent} is not authorized for {name}")
        if not isinstance(args, dict):
            raise ValueError("Tool args must be an object")
        fields = {"list_files": (set(), set()), "read_file": ({"path"}, {"path", "offset", "limit"}),
                  "write_file": ({"path", "content"}, {"path", "content"}),
                  "update_file": ({"path", "old", "new"}, {"path", "old", "new"}),
                  "delete_file": ({"path"}, {"path"}), "retrieve_documents": ({"query"}, {"query", "limit"}),
                  "web_search": ({"query"}, {"query"}), "fetch_url": ({"url"}, {"url"}),
                  "python_run": ({"path"}, {"path"}), "git_publish": ({"paths"}, {"paths", "branch"})}
        required, allowed = fields[name]
        if not required <= args.keys() or not args.keys() <= allowed:
            raise ValueError(f"Invalid arguments for {name}; required={sorted(required)}, allowed={sorted(allowed)}")

    def files(self, root: Path) -> list[str]:
        found = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()]
            for name in sorted(files):
                relative = (Path(directory) / name).relative_to(root).as_posix()
                try:
                    path = safe_path(root, relative)
                    if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS:
                        found.append(relative)
                except ValueError:
                    continue
                if len(found) >= 100:
                    return found
        return found

    def _file(self, root: Path, relative: str, writing: bool = False) -> Path:
        path = safe_path(root, relative)
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            raise ValueError("Only text documents, source code and internal structured text are allowed")
        if writing and relative.replace("\\", "/").split("/")[0] == "inputs":
            raise PermissionError("Authorized input snapshots are read-only to file tools")
        return path

    def prepare(self, run: dict, task: dict, name: str, args: dict) -> dict | None:
        agent = task["owner"]
        self.validate(agent, name, args)
        root = self.store.workspace(agent, run["id"])
        if name == "delete_file":
            path = self._file(root, args["path"], writing=True)
            if not path.is_file() or path.stat().st_size > self.config.max_file_bytes:
                raise ValueError("Deletion target missing or too large")
            return {"path": args["path"], "sha256": digest(path.read_bytes()), "operation_id": uid("op")}
        if name == "git_publish":
            paths = args["paths"]
            if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or len(set(paths)) != len(paths):
                raise ValueError("Publish 1–20 unique output paths")
            remote, branch = self.config.github_remote, args.get("branch", "evotask/" + run["id"])
            github_target(remote, branch)
            artifacts = []
            for relative in paths:
                self._file(root, relative, writing=True)
                artifacts.append(self.store.archive(run, agent, relative, task["id"]))
            return {"remote": remote, "branch": branch, "artifacts": artifacts, "operation_id": uid("op")}
        return None

    def execute(self, run: dict, task: dict, name: str, args: dict, prepared: dict | None = None) -> dict:
        agent = task["owner"]
        self.validate(agent, name, args)
        root = self.store.workspace(agent, run["id"])
        if name == "list_files":
            return {"files": self.files(root)}
        if name in {"read_file", "write_file", "update_file", "delete_file", "python_run"}:
            path = self._file(root, args["path"], writing=name in {"write_file", "update_file", "delete_file"})
            if name in {'write_file', 'update_file', 'delete_file'} and any(
                    self._file(root, test['path']) == path for test in task.get('revision_tests', [])):
                raise PermissionError('Revision verification scripts are frozen; repair the upstream output, not the assertions')
            if name == "read_file":
                offset, limit = args.get('offset', 0), args.get('limit', 16000)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
                    raise ValueError('offset must be nonnegative; limit must be 1–16000 characters')
                with path.open(encoding='utf-8') as stream:
                    remaining = offset
                    while remaining:
                        skipped = stream.read(min(remaining, 65536))
                        if not skipped:
                            break
                        remaining -= len(skipped)
                    content = stream.read(limit)
                    truncated = bool(stream.read(1))
                return {'path': args['path'], 'content': content, 'offset': offset,
                        'truncated': truncated, 'next_offset': offset + len(content) if truncated else None}
            if name in {"write_file", "update_file"}:
                if name == "write_file":
                    content = args["content"]
                else:
                    if path.stat().st_size > self.config.max_file_bytes:
                        raise ValueError("File exceeds update limit")
                    current = path.read_text(encoding="utf-8")
                    if not isinstance(args["old"], str) or not args["old"] or current.count(args["old"]) != 1:
                        raise ValueError("old text must match exactly once")
                    content = current.replace(args["old"], args["new"], 1)
                if not isinstance(content, str) or len(content.encode()) > self.config.max_file_bytes:
                    raise ValueError("Content must be UTF-8 text within file limit")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                return {"path": args["path"], "bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
            if name == "delete_file":
                if not prepared or prepared["path"] != args["path"] or digest(path.read_bytes()) != prepared["sha256"]:
                    raise PermissionError("Deletion needs approval for this exact file version")
                backup = self.store.archive(run, agent, args["path"], task["id"])
                path.unlink()
                return {"deleted": args["path"], "archived_artifact": backup["id"]}
            return self.sandbox.execute(root, args["path"])
        if name == "retrieve_documents":
            return {"passages": self.kb.search(args["query"], agent, args.get("limit", 4), run.get('knowledge_base_ids'))}
        if name == "web_search":
            return search_web(args["query"], self.config)
        if name == "fetch_url":
            return fetch_text(args["url"], self.config)
        if name == "git_publish":
            if not prepared:
                raise PermissionError("Git publishing requires an approved immutable preview")
            return self.git.publish(run, prepared)
        raise ValueError("Unknown tool")
