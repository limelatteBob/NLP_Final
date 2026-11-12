from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

from .config import Config
from .storage import Store


def github_target(remote: str, branch: str) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", remote):
        raise ValueError("Configure an explicit HTTPS github.com repository without credentials")
    if not re.fullmatch(r"evotask/[a-zA-Z0-9_-]+", branch):
        raise ValueError("Publishing is restricted to a dedicated evotask/<name> branch")


class GitPublisher:
    """Host Git only touches controller-owned repositories, never untrusted .git/config/hooks."""
    def __init__(self, store: Store, config: Config):
        self.store, self.config = store, config

    def _git(self, repo: Path, *args: str, auth: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "EvoTask Agent",
                    "GIT_AUTHOR_EMAIL": "evotask@example.invalid", "GIT_COMMITTER_NAME": "EvoTask Agent",
                    "GIT_COMMITTER_EMAIL": "evotask@example.invalid"})
        if auth and (token := os.environ.get(self.config.github_token_env)):
            basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + basic})
        result = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
                                 "-c", "credential.helper=", *args], cwd=repo, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=self.config.tool_timeout)
        if check and result.returncode:
            # Git transport output can contain credentials; do not expose stderr.
            raise RuntimeError(f"Git {args[0]} failed with exit {result.returncode}; inspect repository access/configuration")
        return result

    def publish(self, run: dict, prepared: dict) -> dict:
        remote, branch = prepared["remote"], prepared["branch"]
        github_target(remote, branch)
        repo = self.store.root / "publish" / prepared["operation_id"]
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            self._git(repo, "init")
            for item in prepared["artifacts"]:
                target = repo / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.store.artifact_bytes(item))
            self._git(repo, "add", "--", *[a["path"] for a in prepared["artifacts"]])
            self._git(repo, "commit", "-m", f"EvoTask artifacts for {run['id']}")
        commit = self._git(repo, "rev-parse", "HEAD").stdout.strip()
        before = self._git(repo, "ls-remote", remote, "refs/heads/" + branch, auth=True).stdout.strip()
        if before and before.split()[0] != commit:
            raise ValueError("Target branch already exists with different content; choose a new branch")
        if not before:
            self._git(repo, "push", remote, f"HEAD:refs/heads/{branch}", auth=True)
        after = self._git(repo, "ls-remote", remote, "refs/heads/" + branch, auth=True).stdout.strip()
        if not after or after.split()[0] != commit:
            raise RuntimeError("Remote verification failed; push outcome uncertain, inspect before retrying")
        return {"commit": commit, "remote": remote, "branch": branch, "remote_verified": True}
