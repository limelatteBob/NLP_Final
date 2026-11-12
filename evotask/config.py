from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

from .environment import read_env


@dataclass(frozen=True)
class Config:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "SET_TO_COURSE_MODEL"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_timeout: int = 60
    max_response_tokens: int = 4096
    thinking_mode: str = "default"
    max_model_calls: int = 80
    max_steps: int = 100
    max_task_steps: int = 20
    max_tasks: int = 16
    max_delegate_depth: int = 3
    active_timeout: int = 900
    tool_timeout: int = 30
    docker_image: str = "python:3.11-slim"
    search_provider: str = "wikipedia"
    search_key_env: str = "BRAVE_API_KEY"
    github_remote: str = ""
    github_token_env: str = "EVOTASK_GITHUB_TOKEN"
    max_file_bytes: int = 262144
    max_output_bytes: int = 65536

    @classmethod
    def load(cls, path: str | None = None, env_path: str | Path | None = ".env") -> "Config":
        explicit = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
        if not isinstance(explicit, dict) or not explicit.keys() <= {f.name for f in fields(cls)}:
            raise ValueError("JSON config contains unsupported fields; store credentials only in .env/environment")
        file_values = read_env(env_path)
        process_values = dict(os.environ)
        defaults = cls()
        data = {}
        aliases = {"base_url": ("BASE_URL", "DEEPSEEK_BASE_URL"), "model": ("MODEL", "DEEPSEEK_MODEL")}
        # Explicit JSON > process environment > dotenv > dataclass defaults.
        for source in (file_values, process_values):
            for field in fields(cls):
                for name in (*aliases.get(field.name, ()), "EVOTASK_" + field.name.upper()):
                    if name not in source:
                        continue
                    data[field.name] = source[name]
        data.update(explicit)
        for field in fields(cls):
            if type(getattr(defaults, field.name)) is int and isinstance(data.get(field.name), str):
                try:
                    data[field.name] = int(data[field.name])
                except ValueError:
                    raise ValueError(f"EVOTASK_{field.name.upper()} must be a positive integer; value omitted") from None
        if "api_key_env" not in data:
            preferred = "HKGAI_API_KEY" if data.get("provider", defaults.provider) == "hkgai" else "DEEPSEEK_API_KEY"
            # Legacy API_KEY remains supported; a process-level alias outranks a file value.
            data["api_key_env"] = next((name for source in (process_values, file_values)
                                       for name in (preferred, "API_KEY") if source.get(name)), preferred)
        cfg = cls(**data)
        cfg.validate()
        # Do not populate unrelated variables such as PATH from the dotenv file.
        for name in (cfg.api_key_env, cfg.search_key_env, cfg.github_token_env):
            if name in file_values:
                os.environ.setdefault(name, file_values[name])
        return cfg

    def validate(self) -> None:
        if self.provider not in {"deepseek", "hkgai", "demo"}:
            raise ValueError("Only deepseek, hkgai and explicitly labelled demo are supported")
        endpoint = urlparse(self.base_url)
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
            raise ValueError("Model endpoint must be HTTPS without embedded credentials")
        if endpoint.query or endpoint.fragment:
            raise ValueError("Model endpoint must not contain a query or fragment")
        if self.provider == "deepseek" and endpoint.hostname != "api.deepseek.com":
            raise ValueError("DeepSeek mode only sends requests to api.deepseek.com")
        if self.provider == "hkgai" and any(
            endpoint.hostname == host or endpoint.hostname.endswith("." + host)
            for host in ("openai.com", "anthropic.com", "deepseek.com")
        ):
            raise ValueError("Set HKGAI to the course-provided HKGAI endpoint")
        for name in ("api_timeout", "max_response_tokens", "max_model_calls", "max_steps", "max_task_steps", "max_tasks",
                     "max_delegate_depth", "active_timeout", "tool_timeout", "max_file_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.search_provider not in {"wikipedia", "brave"}:
            raise ValueError("search_provider must be wikipedia or brave")
        if self.thinking_mode not in {"default", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be default, enabled or disabled")
        for name in ("api_key_env", "search_key_env", "github_token_env"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", getattr(self, name)):
                raise ValueError(f"{name} must name an environment variable, not contain a credential")

    def snapshot(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True).encode()).hexdigest()
