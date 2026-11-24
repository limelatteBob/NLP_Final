from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from ..config import Config
from ..conversations import team_messages
from ..outputs import describe_artifacts, grouped_artifacts
from ..retries import existing_retry, create_retry
from ..collaboration import valid_source_url
from ..documents import extract_document, file_digest
from ..engine import Engine, TERMINAL
from ..providers import make_provider
from ..retrieval import KnowledgeBase
from ..sandbox import DockerSandbox
from ..storage import AGENTS, Store, atomic_json, safe_path, uid
from ..tools import CAPABILITIES
from ..teams import Teams
from ..exports import EvaluationExportError, evaluation_zip, team_audit, zip_chunks
from .accounts import Accounts, ACCOUNT_COOKIE, SESSION_SECONDS, token_digest, username
from .knowledge_import import already_imported, copy_knowledge


HERE = Path(__file__).resolve().parent
Mode = Literal["demo", "live"]
Agent = Literal["coordinator", "researcher", "coder", "verifier"]
Channel = Literal["group", "coordinator", "researcher", "coder", "verifier"]
DEMO_GOAL = "研究 cosine similarity 的原理和边界条件，委派证据摘录，并生成带有来源引用的 Markdown 报告。"
COOKIE = "evotask_session"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextFile(Payload):
    name: str = Field(min_length=1, max_length=200)
    content: str


class RunInput(Payload):
    goal: str = Field(default="", max_length=12000)
    memory: bool = True
    review: bool = True
    files: list[TextFile] = Field(default_factory=list)
    upload_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] | None = None
    team_id: str | None = None
    source_run_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list, max_length=60)


class TeamInput(Payload):
    name: str = Field(min_length=1, max_length=80)
    roles: dict


class RetryInput(Payload):
    request_key: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


class ReplyInput(Payload):
    request_id: str = Field(min_length=1, max_length=80)
    reply: str = Field(min_length=1, max_length=8000)
    team_id: str | None = None


class DocumentInput(TextFile):
    scope: Literal["shared", "coordinator", "researcher", "coder", "verifier"] = "shared"
    base_id: str = 'kb_default'
    source_url: str = Field(default='', max_length=2048)
    description: str = Field(default='', max_length=2000)


class BaseInput(Payload):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=1000)


class BaseDeleteInput(Payload):
    delete_documents: bool = False
    expected_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class DocumentSettings(Payload):
    base_id: str
    scope: Literal['shared', 'coordinator', 'researcher', 'coder', 'verifier']
    source_url: str = Field(default='', max_length=2048)
    description: str = Field(default='', max_length=2000)
    content: str | None = None
    expected_revision: str | None = Field(default=None, max_length=64)


class ChatInput(Payload):
    channel: Channel = "group"
    message: str = Field(min_length=1, max_length=8000)
    team_id: str | None = None


class LoginInput(Payload):
    token: str = Field(min_length=1, max_length=512)


class AccountLogin(Payload):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class AccountRegister(AccountLogin):
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(default='', max_length=60)


class PasswordChange(Payload):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


def validate_name(name: str) -> None:
    if not name or len(name) > 200 or '/' in name or '\\' in name:
        raise HTTPException(422, '请使用不含路径、长度不超过 200 字符的文件名。')
    safe_path(Path.cwd(), name)


def validate_file(file: TextFile) -> bytes:
    validate_name(file.name)
    return file.content.encode('utf-8')


def upload_folder(root: Path, upload_id: str) -> Path:
    if not re.fullmatch(r'upload_[a-f0-9]{12}', upload_id):
        raise HTTPException(404, '上传文件不存在。')
    return safe_path(root, 'uploads/' + upload_id)


def remove_upload(root: Path, upload_id: str) -> None:
    folder = upload_folder(root, upload_id)
    if folder.is_dir():
        # Only known direct files in a validated session-local folder, no recursion.
        for child in folder.iterdir():
            checked = safe_path(root, child.relative_to(root).as_posix())
            if checked.is_file():
                checked.unlink()
        folder.rmdir()


def purge_deleted_knowledge(store: Store) -> int:
    """Retry committed deletions using validated, workspace-local paths only."""
    for row in store.db.execute('SELECT * FROM knowledge_file_cleanup').fetchall():
        doc = json.loads(row['document'])
        try:
            if doc.get('upload_id'):
                folder = upload_folder(store.root, doc['upload_id'])
                metadata = folder / 'metadata.json'
                if metadata.exists():
                    info = json.loads(metadata.read_text(encoding='utf-8'))
                    if info.get('purpose') != 'knowledge' or info.get('document_id') != doc['id']:
                        raise ValueError('Upload no longer matches deleted knowledge document')
                remove_upload(store.root, doc['upload_id'])
            else:
                # Legacy Web copies only; never follow a CLI document's original path.
                prefix = '' if doc.get('base_id', 'kb_default') == 'kb_default' else doc['base_id'] + '/'
                candidate = safe_path(store.root, f"knowledge-uploads/{prefix}{doc['scope']}/{doc['name']}")
                candidate.unlink(missing_ok=True)
        except (OSError, ValueError, HTTPException):
            continue
        with store.db:
            store.db.execute('DELETE FROM knowledge_file_cleanup WHERE id=?', (row['id'],))
    return store.db.execute('SELECT COUNT(*) FROM knowledge_file_cleanup').fetchone()[0]


def ingest_upload(store: Store, upload_id: str, name: str, purpose: str, scope: str,
                  base_id: str = 'kb_default', source_url: str = '', description: str = '') -> dict:
    source_url = valid_source_url(source_url)
    folder = upload_folder(store.root, upload_id)
    source = folder / ('original' + Path(name).suffix)
    normalized = folder / 'text.txt'
    info = {'id': upload_id, 'name': name, 'scope': scope, 'purpose': purpose,
            'bytes': source.stat().st_size, 'sha256': file_digest(source),
            **extract_document(source, name, normalized)}
    if purpose == 'knowledge':
        kb = KnowledgeBase(store)
        purge_deleted_knowledge(store)
        kb.validate_bases([base_id])
        previous = next((d for d in kb.summaries() if d['name'] == name and d['scope'] == scope and d['base_id'] == base_id), None)
        if not normalized.exists():
            normalized.write_text('', encoding='utf-8')
        metadata = {key: info[key] for key in ('bytes', 'status', 'detail', 'text_bytes')}
        if info['status'] == 'ready':
            info['status'] = metadata['status'] = 'indexed'
        metadata['upload_id'] = upload_id
        metadata.update(source_url=source_url, description=description)
        info.update(base_id=base_id, source_url=source_url, description=description)
        identity = store.root / 'knowledge-uploads' / scope / name if base_id == 'kb_default' else store.root / 'knowledge-uploads' / base_id / scope / name
        doc = kb.add(normalized, scope, name=name, identity=str(identity), base_id=base_id,
                     version=info['sha256'], metadata=metadata, document_id=previous['id'] if previous else uid('doc'))
        info['document_id'] = doc['id']
        if previous and previous.get('upload_id'):
            remove_upload(store.root, previous['upload_id'])
    atomic_json(folder / 'metadata.json', info)
    return info


class Jobs:
    """Bounded jobs; one writer per browser workspace, a Store per thread."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evotask")
        self.mutex = threading.Lock()
        self.active: dict[str, dict] = {}

    def reserve(self, root: Path, run_id: str | None = None) -> threading.Event:
        with self.mutex:
            if str(root) in self.active:
                raise HTTPException(409, "当前空间正在执行任务或对话，请等待完成后再操作。")
            if len(self.active) >= 4:
                raise HTTPException(429, "服务正在处理其他任务，请稍后重试。")
            event = threading.Event()
            self.active[str(root)] = {"id": run_id, "cancel": event}
            return event

    def release(self, root: Path) -> None:
        with self.mutex:
            self.active.pop(str(root), None)

    def info(self, root: Path) -> dict:
        with self.mutex:
            item = self.active.get(str(root))
            return {"busy": item is not None, "run_id": item["id"] if item else None,
                    "cancelling": item["cancel"].is_set() if item else False}

    def stop(self, root: Path, run_id: str) -> bool:
        with self.mutex:
            item = self.active.get(str(root))
            if item and item["id"] == run_id:
                item["cancel"].set()
                return True
        return False

    def submit(self, root: Path, run_id: str, config: Config, event: threading.Event,
               reply: ReplyInput | None = None) -> None:
        with self.mutex:
            self.active[str(root)]["id"] = run_id

        def work():
            store = Store(root)
            try:
                with store.lock():
                    engine = Engine(store, config, make_provider(config), cancelled=event.is_set)
                    if reply:
                        if event.is_set():
                            engine.cancel(store.load(run_id))
                        else:
                            engine.resume(run_id, reply.request_id, reply.reply)
                    else:
                        engine.advance(store.load(run_id))
            except Exception:
                # A new retry must not remain falsely running after worker failure.
                run = store.load(run_id)
                if run.get('retry_of') and run['status'] not in TERMINAL:
                    run.update(status='failed', failure='执行服务中断；可以重新运行。', ended=time.time())
                    store.save(run)
                # Preserve evidence, never expose exception internals or credentials.
                store.event(run_id, "coordinator", "service_error", {
                    "message": "执行服务中断；状态已保留，可检查后继续运行。"})
            finally:
                store.close()
                self.release(root)

        try:
            self.pool.submit(work)
        except Exception:
            self.release(root)
            raise

    def shutdown(self) -> None:
        with self.mutex:
            for item in self.active.values():
                item["cancel"].set()
        self.pool.shutdown(wait=True, cancel_futures=False)


def create_app(config: Config | None = None, state_dir: Path | None = None,
               access_token: str = "", allowed_hosts: list[str] | None = None) -> FastAPI:
    config = config or Config.load()
    root = (state_dir or Path(".evotask-web")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key_path = root / "session.key"
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        secret = key_path.read_bytes()
    else:
        with os.fdopen(fd, "wb") as handle:
            secret = secrets.token_bytes(32)
            handle.write(secret)
    if len(secret) != 32:
        raise ValueError("Invalid Web session key; restore the state directory")
    jobs = Jobs()
    retry_lock = threading.Lock()
    accounts = Accounts(root)
    login_attempts: dict[str, list[float]] = {}
    login_lock = threading.Lock()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(jobs.shutdown)

    app = FastAPI(title="EvoTask API", version="0.4.1", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs
    app.state.accounts = accounts

    def mac(value: str) -> str:
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    def cookie_value(sid: str, authenticated: bool) -> str:
        # Binding the gate fingerprint invalidates sessions when the access token changes.
        gate = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        value = f"{sid}.{int(time.time()) + 30 * 86400}.{int(authenticated)}.{gate}"
        return value + "." + mac(value)

    def set_cookie(response, request, authenticated):
        response.set_cookie(COOKIE, cookie_value(request.state.sid, authenticated),
                            max_age=30 * 86400, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    def csrf_for(sid: str, token: str = '') -> str:
        return mac('csrf:' + sid + (':' + token_digest(token) if token else ''))

    @app.middleware("http")
    async def session_boundary(request: Request, call_next):
        sid, authenticated, fresh = secrets.token_hex(24), not access_token, True
        cookie = request.cookies.get(COOKIE, "")
        parts = cookie.split(".")
        if len(parts) == 5:
            identity, expiry, authorized, gate, signature = parts
            if (re.fullmatch(r"[a-f0-9]{48}", identity) and expiry.isdigit()
                    and len(expiry) <= 12 and int(expiry) > time.time()
                    and authorized in {"0", "1"} and gate == hashlib.sha256(access_token.encode()).hexdigest()[:16]
                    and hmac.compare_digest(mac(".".join(parts[:-1])), signature)):
                sid, authenticated, fresh = identity, authorized == "1" or not access_token, False
        request.state.sid, request.state.authenticated = sid, authenticated
        account_token = request.cookies.get(ACCOUNT_COOKIE, '') if authenticated else ''
        user = await asyncio.to_thread(accounts.resolve, account_token) if account_token else None
        request.state.user, request.state.account_token = user, account_token
        csrf = csrf_for(sid, account_token)
        is_api = request.url.path.startswith("/api/")
        expired = bool(account_token and not user)
        if is_api and expired and request.url.path != '/api/session':
            return JSONResponse({'detail': '账号登录已过期或已退出，请刷新页面重新登录。', 'code': 'account_expired'}, status_code=401)
        # Old tabs must not apply edits (or render fetched data) under a newly switched identity.
        if (is_api and request.url.path != '/api/session' and request.method == 'GET'
                and request.headers.get('x-evotask-csrf')
                and not hmac.compare_digest(request.headers['x-evotask-csrf'], csrf)):
            return JSONResponse({'detail': '登录身份已变化，请刷新页面。', 'code': 'identity_changed'}, status_code=403)
        if is_api and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = (request.url.scheme, request.url.netloc)
            if origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != expected:
                return JSONResponse({"detail": "请求来源无效。"}, status_code=403)
            if not hmac.compare_digest(request.headers.get("x-evotask-csrf", ""), csrf):
                return JSONResponse({"detail": "会话已过期，请刷新页面后重试。", 'code': 'identity_changed'}, status_code=403)
            file_upload = request.url.path == '/api/uploads' and request.method == 'POST'
            if not file_upload and "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "请求必须使用 application/json。"}, status_code=415)
            # Binary uploads flow directly to disk; do not buffer them in middleware.
            # Legacy JSON document/attachment endpoints also have no upload size cap.
            if not file_upload and request.url.path not in {'/api/knowledge', '/api/runs'}:
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 2_000_000:
                        return JSONResponse({"detail": "控制请求过大；文件请使用上传接口。"}, status_code=413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
        if is_api and request.url.path not in {"/api/session", "/api/session/login"} and not authenticated:
            return JSONResponse({"detail": "请输入工作台访问口令。"}, status_code=401)
        response = await call_next(request)
        if fresh and "set-cookie" not in response.headers:
            set_cookie(response, request, authenticated)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store" if is_api else "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'; object-src 'none'")
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"detail": str(exc)[:500]}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default includes submitted input; omit it, particularly login tokens.
        return JSONResponse({"detail": "请求字段格式不正确，请检查输入内容。",
                             "fields": [".".join(map(str, error["loc"])) for error in exc.errors()]}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def busy(request, exc):
        return JSONResponse({"detail": "操作未完成，请检查任务状态或系统设置后重试。"}, status_code=409)

    def workspace(request: Request, mode: Mode) -> Path:
        if request.state.user:
            return root / 'accounts' / request.state.user['id'] / mode
        return root / "sessions" / request.state.sid / mode

    @contextlib.contextmanager
    def storage(request: Request, mode: Mode):
        store = Store(workspace(request, mode))
        try:
            yield store
        finally:
            store.close()

    def run_config(mode: Mode) -> Config:
        return replace(config, provider="demo", model="deterministic-fixture") if mode == "demo" else config

    def get_run(store: Store, run_id: str) -> dict:
        if not re.fullmatch(r"run_[a-f0-9]{12}", run_id):
            raise HTTPException(404, "任务不存在。")
        try:
            return store.load(run_id)
        except ValueError:
            raise HTTPException(404, "任务不存在或不属于当前空间。") from None

    def public_run(run: dict, store: Store, detail=True) -> dict:
        fields = ("id", "goal", "status", "mode", "created", "updated", "ended", "failure",
                  "memory_enabled", "model_calls", "steps", "active_seconds", "human_wait_seconds")
        result = {k: run[k] for k in fields}
        result.update(team_id=run.get('team_id'), team_snapshot=run.get('team_snapshot'),
                      inherited_inputs=run.get('inherited_inputs', []), rerun_of=run.get('rerun_of'))
        result["metrics"] = Engine(store, Config(**run["config"]), None).metrics(run)
        presented = describe_artifacts(run, store.events(run['id']))
        groups = grouped_artifacts(presented)
        result['deliverable_count'] = len(groups['deliverable'])
        result['retry_of'] = run.get('retry_of')
        result['retry_root'] = run.get('retry_root')
        result['retry_children'] = run.get('retry_children', [])
        result['can_retry'] = run['status'] == 'failed' and jobs.info(store.root)['run_id'] != run['id']
        result["artifact_count"] = len(run["artifacts"])
        result["task_count"] = len(run["tasks"])
        result["completed_tasks"] = sum(t["status"] == "succeeded" for t in run["tasks"])
        info = jobs.info(store.root)
        result["executing"] = info["busy"] and info["run_id"] == run["id"]
        result["cancelling"] = result["executing"] and info["cancelling"]
        if detail:
            result.update(tasks=run["tasks"], artifacts=presented, artifact_groups=groups, pending=run["pending"],
                          initial_inputs=run["initial_inputs"], uploaded_files=run.get('uploaded_files', []),
                          knowledge_base_ids=run.get('knowledge_base_ids'), revisions=run.get('revisions', []),
                          can_retry_actions=bool(Engine(store, Config(**run['config']), None).retryable_action_tasks(run)),
                          can_repair=(run['status'] == 'failed' and run.get('revision_count', 0) < 2
                                      and any(t['status'] == 'failed' and t['role'] == 'verifier' for t in run['tasks'])))
        return result

    @app.get("/api/session")
    def session(request: Request):
        expired = bool(request.state.account_token and not request.state.user)
        if expired:
            # Never silently expose the pre-login guest workspace on logout/expiration.
            request.state.sid = secrets.token_hex(24)
            request.state.account_token = ''
        response = JSONResponse({"csrf": csrf_for(request.state.sid, request.state.account_token),
                "authenticated": request.state.authenticated, 'user': request.state.user,
                'account_expired': expired, "requires_login": bool(access_token), "version": "0.4.1"})
        if expired:
            response.delete_cookie(ACCOUNT_COOKIE, path='/')
            set_cookie(response, request, request.state.authenticated)
        return response

    @app.post("/api/session/login")
    def login(request: Request, body: LoginInput):
        address = request.client.host if request.client else "local"
        with login_lock:
            now = time.time()
            for key in list(login_attempts):
                login_attempts[key] = [t for t in login_attempts[key] if now - t < 60]
                if not login_attempts[key]:
                    del login_attempts[key]
            attempts = login_attempts.setdefault(address, [])
            if len(attempts) >= 10:
                raise HTTPException(429, "尝试过于频繁，请一分钟后重试。")
            attempts.append(now)
        if not access_token or not hmac.compare_digest(body.token.encode(), access_token.encode()):
            raise HTTPException(401, "访问口令不正确。")
        response = JSONResponse({"authenticated": True})
        set_cookie(response, request, True)
        return response

    def require_account(request: Request) -> dict:
        if not request.state.user:
            raise HTTPException(401, '请先登录账号。')
        return request.state.user

    def account_response(request: Request, token: str) -> JSONResponse:
        response = JSONResponse({'user': accounts.resolve(token), 'csrf': csrf_for(request.state.sid, token)})
        response.set_cookie(ACCOUNT_COOKIE, token, max_age=SESSION_SECONDS,
                            httponly=True, samesite='strict', secure=request.url.scheme == 'https', path='/')
        return response

    def account_attempt(request: Request, identity: str = ''):
        accounts.throttle(request.client.host if request.client else 'local', identity)

    @app.post('/api/account/register', status_code=201)
    def register_account(request: Request, body: AccountRegister):
        if request.state.user:
            raise HTTPException(409, '请先退出当前账号。')
        name = username(body.username)
        account_attempt(request, name)
        user = accounts.register(name, body.display_name, body.password)
        response = account_response(request, accounts.issue(user['id'], request.headers.get('user-agent', '')))
        response.status_code = 201
        return response

    @app.post('/api/account/login')
    def login_account(request: Request, body: AccountLogin):
        if request.state.user:
            raise HTTPException(409, '请先退出当前账号。')
        account_attempt(request, body.username.strip().lower())
        token = accounts.login(body.username, body.password, request.headers.get('user-agent', ''))
        return account_response(request, token)

    @app.post('/api/account/logout')
    def logout_account(request: Request):
        accounts.revoke(request.state.account_token)
        request.state.sid = secrets.token_hex(24)
        response = JSONResponse({'user': None, 'csrf': csrf_for(request.state.sid)})
        response.delete_cookie(ACCOUNT_COOKIE, path='/')
        set_cookie(response, request, request.state.authenticated)
        return response

    @app.get('/api/account/devices')
    def account_devices(request: Request):
        user = require_account(request)
        return accounts.sessions(user['id'], user['session_id'])

    @app.post('/api/account/logout-others')
    def logout_others(request: Request):
        user = require_account(request)
        return {'revoked': accounts.revoke_others(user['id'], user['session_id'])}

    @app.post('/api/account/password')
    def change_password(request: Request, body: PasswordChange):
        user = require_account(request)
        account_attempt(request, user['username'])
        token = accounts.change_password(user['id'], body.current_password, body.new_password,
                                         request.headers.get('user-agent', ''))
        return account_response(request, token)

    @app.get('/api/account/guest-knowledge')
    def guest_knowledge(request: Request, mode: Mode = 'demo'):
        require_account(request)
        guest = root / 'sessions' / request.state.sid / mode
        bases = []
        if (guest / 'state.sqlite3').exists():
            with contextlib.closing(Store(guest)) as store:
                bases = [b for b in KnowledgeBase(store).bases() if b['id'] != 'kb_default' or b['documents']]
        with storage(request, mode) as store:
            imported = already_imported(store, request.state.sid)
        return {'bases': bases, 'documents': sum(b['documents'] for b in bases), 'already_imported': imported}

    @app.post('/api/account/import-guest')
    def import_guest(request: Request, mode: Mode = 'demo'):
        require_account(request)
        target, guest = workspace(request, mode), root / 'sessions' / request.state.sid / mode
        jobs.reserve(target)
        try:
            jobs.reserve(guest)
            try:
                with storage(request, mode) as store, store.lock(), contextlib.closing(Store(guest)) as source, source.lock():
                    return copy_knowledge(source, store, request.state.sid)
            finally:
                jobs.release(guest)
        finally:
            jobs.release(target)

    @app.get("/api/workspace")
    def overview(request: Request, mode: Mode = "demo"):
        with storage(request, mode) as store:
            runs = [public_run(run, store, False) for run in store.runs()]
            docs = KnowledgeBase(store).summaries()
            return {"runs": runs, "teams": Teams(store).list(), "documents": len(docs), 'knowledge_bases': KnowledgeBase(store).bases(), "job": jobs.info(store.root),
                    "agents": [{"id": a, "tools": sorted(CAPABILITIES[a]),
                                "memory_count": len(store.memory(a))} for a in AGENTS]}

    @app.get("/api/system")
    def system():
        return {"provider": config.provider, "model": config.model,
                "api_key_present": bool(os.environ.get(config.api_key_env)),
                "model_configured": config.model != "SET_TO_COURSE_MODEL",
                "docker": DockerSandbox(config).doctor(), "git": bool(shutil.which("git")),
                "search_provider": config.search_provider, "max_model_calls": config.max_model_calls,
                "api_timeout": config.api_timeout, "live_verified": False,
                "note": "此检查不调用模型，不验证账户余额。"}

    @app.post("/api/runs", status_code=202)
    def create_run(request: Request, body: RunInput, mode: Mode = "demo"):
        cfg = run_config(mode)
        if mode == "live":
            if cfg.provider == "demo" or not os.environ.get(cfg.api_key_env) or cfg.model == "SET_TO_COURSE_MODEL":
                raise HTTPException(422, "真实模式需要服务端配置课程允许的模型和 API 密钥。")
            if not body.goal.strip():
                raise HTTPException(422, "请描述任务目标。")
        elif body.files or body.upload_ids:
            raise HTTPException(422, "离线演示使用固定资料；自定义附件请切换到真实模型模式。")
        data = [validate_file(file) for file in body.files]
        target = workspace(request, mode)
        event = jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                if len(store.runs()) >= 200:
                    raise HTTPException(429, "当前空间已达到 200 次任务的上限。")
                if mode == "demo":
                    KnowledgeBase(store).add(HERE / "data" / "similarity.md")
                paths, names, uploaded = [], [], []
                for upload_id in body.upload_ids:
                    folder = upload_folder(store.root, upload_id)
                    if not (folder / 'metadata.json').is_file():
                        raise HTTPException(404, '附件不存在或不属于当前空间。')
                    info = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
                    if info['purpose'] != 'task':
                        raise HTTPException(422, '请使用任务附件上传入口提供输入资料。')
                    uploaded.append(info)
                    names.append(info['name'][:100] + '.txt')
                    if info['status'] in {'ready', 'indexed'}:
                        paths.append(folder / 'text.txt')
                    else:
                        note = folder / 'unreadable.txt'
                        note.write_text(f"附件 {info['name']}：{info['detail']}\n未读取正文，不应据此编造内容。", encoding='utf-8')
                        paths.append(note)
                upload_id = uid('input')
                for index, (file, content) in enumerate(zip(body.files, data)):
                    path = safe_path(store.root, f"uploads/{upload_id}/{index}.txt")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    paths.append(path)
                    names.append(file.name[:100] + '.txt')
                goal = DEMO_GOAL if mode == "demo" and not body.team_id else body.goal.strip()
                run = Engine(store, cfg, None).new(goal, paths, body.memory, require_review=body.review, input_names=names,
                                                 knowledge_base_ids=body.knowledge_base_ids if mode == 'live' else None,
                                                 team_id=body.team_id, source_run_id=body.source_run_id, artifact_ids=body.artifact_ids)
                run['uploaded_files'] = uploaded
                store.save(run)
                result = public_run(run, store)
            jobs.submit(target, run["id"], cfg, event)
            result["executing"] = True
            return result
        except Exception:
            jobs.release(target)
            raise

    @app.get("/api/runs/{run_id}")
    def run_detail(request: Request, run_id: str, mode: Mode = "demo"):
        with storage(request, mode) as store:
            return public_run(get_run(store, run_id), store)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(request: Request, run_id: str, mode: Mode = "demo", after: int = 0):
        with storage(request, mode) as store:
            get_run(store, run_id)
        target = workspace(request, mode)

        async def events():
            cursor = max(0, after)
            previous = None
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                if request.state.account_token and not await asyncio.to_thread(accounts.resolve, request.state.account_token):
                    yield 'event: expired\ndata: {}\n\n'
                    break
                # A connection belongs to the current thread; none are held across awaits.
                with contextlib.closing(Store(target)) as store:
                    run = get_run(store, run_id)
                    rows = store.db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200",
                                            (run_id, cursor)).fetchall()
                    batch = [dict(row) | {"data": json.loads(row["data"])} for row in rows]
                    info = public_run(run, store)
                if batch:
                    cursor = batch[-1]["seq"]
                marker = (run["updated"], info["executing"], info["cancelling"])
                if batch or previous != marker:
                    yield "event: snapshot\ndata: " + json.dumps({"run": info, "events": batch, "cursor": cursor}, ensure_ascii=False) + "\n\n"
                    previous = marker
                if not info["executing"] and len(batch) < 200:
                    yield "event: idle\ndata: {}\n\n"
                    break
                if time.monotonic() - heartbeat > 12:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.45)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/events")
    def run_events(request: Request, run_id: str, mode: Mode = "demo"):
        with storage(request, mode) as store:
            get_run(store, run_id)
            return store.events(run_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(request: Request, run_id: str, body: ReplyInput, mode: Mode = "demo"):
        target = workspace(request, mode)
        event = jobs.reserve(target, run_id)
        try:
            with storage(request, mode) as store, store.lock():
                run = get_run(store, run_id)
                if body.team_id is not None and run.get('team_id') != body.team_id:
                    raise HTTPException(404, '任务不属于当前团队。')
                pending = run["pending"]
                if not pending or pending["id"] != body.request_id:
                    raise HTTPException(409, "该请求已处理或已失效，请刷新任务。")
                if pending["kind"] in {"acceptance", "tool_approval"} and body.reply not in {"yes", "no"}:
                    raise HTTPException(422, "审批只能回复 yes 或 no。")
                if not body.reply.strip():
                    raise HTTPException(422, "回复不能为空。")
            jobs.submit(target, run_id, Config(**run["config"]), event, body)
            return {"accepted": True}
        except Exception:
            jobs.release(target)
            raise

    @app.post('/api/runs/{run_id}/retry', status_code=202)
    def retry_run(request: Request, run_id: str, body: RetryInput, mode: Mode = 'demo'):
        with retry_lock, storage(request, mode) as store:
            source = get_run(store, run_id)
            found = existing_retry(store, source, body.request_key)
            if found:
                return public_run(found, store)
            if source['status'] != 'failed':
                raise HTTPException(409, '只能重新运行已经失败的任务。')
            if jobs.info(store.root)['run_id'] == run_id:
                raise HTTPException(409, '原任务尚未停止执行，请稍后重试。')
            if len(store.runs()) >= 200:
                raise HTTPException(429, '当前空间已达到 200 次任务上限。')
            cfg = run_config(mode)
            if mode == 'live' and (cfg.provider == 'demo' or not os.environ.get(cfg.api_key_env) or cfg.model == 'SET_TO_COURSE_MODEL'):
                raise HTTPException(422, '当前模型配置不可用，请先检查系统设置。')
            target = workspace(request, mode)
            event = jobs.reserve(target)
            child = None
            try:
                with store.lock():
                    source = get_run(store, run_id)
                    child = create_retry(store, cfg, source, body.request_key)
                result = public_run(child, store)
                result['executing'] = True
                jobs.submit(target, child['id'], cfg, event)
            except Exception:
                jobs.release(target)
                if child is not None:
                    child.update(status='failed', failure='任务已创建，但启动失败；可以重新运行。', ended=time.time())
                    store.save(child)
                    store.event(child['id'], 'coordinator', 'run_start_failed', {'reason':'Background scheduling failed'})
                    return JSONResponse({'detail':child['failure'], 'run_id':child['id']}, status_code=503)
                raise
            return result

    @app.post("/api/runs/{run_id}/continue", status_code=202)
    def continue_run(request: Request, run_id: str, mode: Mode = "demo"):
        target = workspace(request, mode)
        event = jobs.reserve(target, run_id)
        try:
            with storage(request, mode) as store, store.lock():
                run = get_run(store, run_id)
                if run["pending"] or run["status"] in TERMINAL:
                    raise HTTPException(409, "该任务无需恢复，或需要先处理人工请求。")
            jobs.submit(target, run_id, Config(**run["config"]), event)
            return {"accepted": True}
        except Exception:
            jobs.release(target)
            raise

    @app.post('/api/runs/{run_id}/retry-actions', status_code=202)
    def retry_actions(request: Request, run_id: str, mode: Mode = 'demo'):
        target = workspace(request, mode)
        event = jobs.reserve(target, run_id)
        try:
            with storage(request, mode) as store, store.lock():
                run = get_run(store, run_id)
                cfg = Config(**run['config'])
                engine = Engine(store, cfg, None)
                if not engine.retryable_action_tasks(run):
                    raise HTTPException(409, '没有可继续的动作格式错误，或剩余预算不足。')
                engine.retry_actions(run)
            jobs.submit(target, run_id, cfg, event)
            return {'accepted': True}
        except Exception:
            jobs.release(target)
            raise

    @app.post('/api/runs/{run_id}/repair', status_code=202)
    def repair_run(request: Request, run_id: str, mode: Mode = 'demo'):
        target = workspace(request, mode)
        event = jobs.reserve(target, run_id)
        try:
            with storage(request, mode) as store, store.lock():
                run = get_run(store, run_id)
                cfg = Config(**run['config'])
                Engine(store, cfg, None).retry_verification(run)
            jobs.submit(target, run_id, cfg, event)
            return {'accepted': True}
        except Exception:
            jobs.release(target)
            raise

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(request: Request, run_id: str, mode: Mode = "demo"):
        target = workspace(request, mode)
        with storage(request, mode) as store:
            run = get_run(store, run_id)
            if jobs.stop(target, run_id):
                return {"accepted": True, "detail": "将在当前模型请求或工具执行结束后停止。"}
            if run["status"] in TERMINAL:
                return {"accepted": True}
            jobs.reserve(target, run_id)
            try:
                with store.lock():
                    Engine(store, Config(**run["config"]), None).cancel(run)
            finally:
                jobs.release(target)
            return {"accepted": True}

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(request: Request, run_id: str, artifact_id: str, mode: Mode = "demo", download: bool = False):
        with storage(request, mode) as store:
            run = get_run(store, run_id)
            entry = next((a for a in describe_artifacts(run, store.events(run_id)) if a["id"] == artifact_id), None)
            if entry is None:
                raise HTTPException(404, "产物不存在。")
            content = store.artifact_bytes(entry)
            if not download:
                return {**entry, "content": content.decode("utf-8")}
            return Response(content, media_type="application/octet-stream", headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(entry["download_name"])})

    @app.get("/api/runs/{run_id}/report")
    def report(request: Request, run_id: str, mode: Mode = "demo"):
        with storage(request, mode) as store:
            run = get_run(store, run_id)
            # Serialize a fresh, read-only audit snapshot, including failures and checks.
            data = {"run": public_run(run, store), "events": store.events(run_id),
                    "verification": "Declared acceptance checks only; demo is not LLM performance."}
            return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get("/api/knowledge")
    def knowledge(request: Request, mode: Mode = "demo"):
        with storage(request, mode) as store:
            return KnowledgeBase(store).summaries()

    def mutate_knowledge(request, mode, action):
        target = workspace(request, mode)
        jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                kb = KnowledgeBase(store)
                purge_deleted_knowledge(store)
                return action(store, kb)
        finally:
            jobs.release(target)

    @app.get('/api/knowledge/bases')
    def bases(request: Request, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            return KnowledgeBase(store).bases()

    @app.post('/api/knowledge/bases', status_code=201)
    def create_base(request: Request, body: BaseInput, mode: Mode = 'demo'):
        return mutate_knowledge(request, mode, lambda store, kb: kb.save_base(body.name, body.description))

    @app.patch('/api/knowledge/bases/{base_id}')
    def update_base(request: Request, base_id: str, body: BaseInput, mode: Mode = 'demo'):
        return mutate_knowledge(request, mode, lambda store, kb: kb.save_base(body.name, body.description, base_id))

    @app.get('/api/knowledge/bases/{base_id}')
    def base_deletion_preview(request: Request, base_id: str, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            kb = KnowledgeBase(store)
            if base_id not in {b['id'] for b in kb.bases()}:
                raise HTTPException(404, '知识库不存在或不属于当前空间。')
            return kb.deletion_preview(base_id)

    @app.delete('/api/knowledge/bases/{base_id}')
    def delete_base(request: Request, base_id: str, body: BaseDeleteInput | None = None, mode: Mode = 'demo'):
        body = body or BaseDeleteInput()
        def change(store, kb):
            if base_id not in {b['id'] for b in kb.bases()}:
                raise HTTPException(404, '知识库不存在或已删除，请同步后重试。')
            preview = kb.deletion_preview(base_id)
            if body.delete_documents and not body.expected_revision:
                raise HTTPException(422, '请先查看知识库删除预览并确认。')
            if body.expected_revision and not hmac.compare_digest(body.expected_revision, preview['revision']):
                raise HTTPException(409, '知识库内容已变化，请关闭此确认窗口，同步后重新确认删除。')
            result = kb.remove_base(base_id, delete_documents=body.delete_documents)
            result['cleanup_pending'] = purge_deleted_knowledge(store)
            return result
        return mutate_knowledge(request, mode, change)

    @app.post('/api/uploads', status_code=201)
    async def upload(request: Request, name: str, purpose: Literal['knowledge', 'task'] = 'knowledge',
                     scope: Literal['shared', 'coordinator', 'researcher', 'coder', 'verifier'] = 'shared',
                     mode: Mode = 'demo', base_id: str = 'kb_default', source_url: str = '', description: str = ''):
        validate_name(name)
        valid_source_url(source_url)
        if len(description) > 2000:
            raise HTTPException(422, '文档说明请保持在 2000 字符以内。')
        with storage(request, mode) as store:
            KnowledgeBase(store).validate_bases([base_id])
        target, upload_id = workspace(request, mode), uid('upload')
        jobs.reserve(target)
        folder = upload_folder(target, upload_id)
        complete = False
        try:
            folder.mkdir(parents=True)
            with (folder / ('original' + Path(name).suffix)).open('wb') as output:
                async for chunk in request.stream():
                    await asyncio.to_thread(output.write, chunk)
            def process():
                with contextlib.closing(Store(target)) as store, store.lock():
                    return ingest_upload(store, upload_id, name, purpose, scope, base_id, source_url, description)
            work = asyncio.create_task(asyncio.to_thread(process))
            try:
                info = await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                complete = True
                raise
            complete = True
            return info
        finally:
            try:
                if not complete:
                    remove_upload(target, upload_id)
            finally:
                jobs.release(target)

    @app.get('/api/uploads/{upload_id}/download')
    def original_upload(request: Request, upload_id: str, mode: Mode = 'demo'):
        folder = upload_folder(workspace(request, mode), upload_id)
        if not (folder / 'metadata.json').is_file():
            raise HTTPException(404, '文件不存在或不属于当前空间。')
        info = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
        if info.get('purpose') == 'knowledge':
            with storage(request, mode) as store:
                KnowledgeBase(store)
                doc = store.db.execute('SELECT metadata FROM knowledge_documents WHERE id=?', (info.get('document_id'),)).fetchone()
                if not doc or json.loads(doc['metadata']).get('upload_id') != upload_id:
                    raise HTTPException(404, '资料已删除或已更新。')
        original = folder / ('original' + Path(info['name']).suffix)
        if not original.is_file() or file_digest(original) != info['sha256']:
            raise HTTPException(409, '原件完整性检查失败。')
        return FileResponse(original, media_type='application/octet-stream', filename=info['name'])

    @app.post("/api/knowledge", status_code=201)
    def add_document(request: Request, body: DocumentInput, mode: Mode = "demo"):
        content = validate_file(body)
        target = workspace(request, mode)
        jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                upload_id = uid('upload')
                folder = upload_folder(store.root, upload_id)
                folder.mkdir(parents=True)
                (folder / ('original' + Path(body.name).suffix)).write_bytes(content)
                try:
                    info = ingest_upload(store, upload_id, body.name, 'knowledge', body.scope, body.base_id, body.source_url, body.description)
                    return next(d for d in KnowledgeBase(store).summaries() if d['id'] == info['document_id'])
                except Exception:
                    remove_upload(store.root, upload_id)
                    raise
        finally:
            jobs.release(target)

    @app.delete("/api/knowledge/{document_id}")
    def remove_document(request: Request, document_id: str, mode: Mode = "demo"):
        target = workspace(request, mode)
        jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                kb = KnowledgeBase(store)
                doc = next((d for d in kb.summaries() if d["id"] == document_id), None)
                if not doc:
                    raise HTTPException(404, "文档不存在。")
                kb.remove(document_id)
                if doc.get('upload_id'):
                    remove_upload(store.root, doc['upload_id'])
                # Delete only the known, validated copy in this session's upload directory.
                if not doc.get('upload_id') and doc.get('base_id', 'kb_default') == 'kb_default':
                    candidate = safe_path(store.root, f"knowledge-uploads/{doc['scope']}/{doc['name']}")
                    candidate.unlink(missing_ok=True)
                return {"removed": True}
        finally:
            jobs.release(target)

    @app.get("/api/knowledge/search")
    def search(request: Request, query: str, agent: Agent = "researcher", mode: Mode = "demo", base_id: str | None = None):
        with storage(request, mode) as store:
            kb = KnowledgeBase(store)
            kb.validate_bases([base_id] if base_id else None)
            return kb.search(query, agent, base_ids=[base_id] if base_id else None)

    @app.get('/api/knowledge/{document_id}')
    def document_detail(request: Request, document_id: str, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            doc = next((d for d in KnowledgeBase(store).summaries() if d['id'] == document_id), None)
            if not doc:
                raise HTTPException(404, '文档不存在。')
            text = ''
            if doc.get('upload_id'):
                normalized = upload_folder(store.root, doc['upload_id']) / 'text.txt'
                if normalized.is_file():
                    with normalized.open(encoding='utf-8') as stream:
                        text = stream.read(32001)
            return doc | {'revision': document_revision(doc), 'text': text[:32000], 'truncated': len(text) > 32000,
                          'can_edit_text': bool(doc.get('upload_id')) and Path(doc['name']).suffix.lower() in {'.md', '.txt'} and len(text) <= 32000}

    def document_revision(doc: dict) -> str:
        return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @app.patch('/api/knowledge/{document_id}')
    def edit_document(request: Request, document_id: str, body: DocumentSettings, mode: Mode = 'demo'):
        def change(store, kb):
            doc = next((d for d in kb.summaries() if d['id'] == document_id), None)
            if not doc:
                raise HTTPException(404, '文档不存在。')
            if body.expected_revision and not hmac.compare_digest(body.expected_revision, document_revision(doc)):
                raise HTTPException(409, '资料已在其他页面或设备更新。请复制尚未保存的内容，关闭编辑并同步后重新打开。')
            if body.content is not None and (not doc.get('upload_id') or Path(doc['name']).suffix.lower() not in {'.md', '.txt'}):
                raise HTTPException(422, '此格式请重新上传文件以更新正文。')
            updated = kb.update_document(document_id, body.base_id, body.scope, body.source_url, body.description)
            if body.content is not None:
                upload_id = uid('upload')
                folder = upload_folder(store.root, upload_id)
                folder.mkdir(parents=True)
                try:
                    (folder / ('original' + Path(doc['name']).suffix)).write_text(body.content, encoding='utf-8')
                    ingest_upload(store, upload_id, doc['name'], 'knowledge', body.scope, body.base_id, body.source_url, body.description)
                except Exception:
                    remove_upload(store.root, upload_id)
                    raise
                updated = next(d for d in kb.summaries() if d['id'] == document_id)
            return updated
        return mutate_knowledge(request, mode, change)

    def get_team(store: Store, team_id: str) -> dict:
        try:
            return Teams(store).get(team_id)
        except ValueError:
            raise HTTPException(404, '团队不存在或不属于当前空间。') from None

    @app.get('/api/teams')
    def list_teams(request: Request, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            return Teams(store).list()

    @app.post('/api/teams', status_code=201)
    def create_team(request: Request, body: TeamInput, mode: Mode = 'demo'):
        target = workspace(request, mode)
        jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                return Teams(store).create(body.name, body.roles)
        finally:
            jobs.release(target)

    @app.get('/api/teams/{team_id}')
    def team_detail(request: Request, team_id: str, mode: Mode = 'demo', channel: Channel = 'group'):
        with storage(request, mode) as store:
            team = get_team(store, team_id)
            store.bind_team(team_id)
            runs = [r for r in store.runs() if r.get('team_id') == team_id]
            cards = []
            for run in runs[:30]:
                card = public_run(run, store, False)
                card.update(pending=run['pending'], artifacts=run['artifacts'],
                            tasks=[{k: t[k] for k in ('id', 'title', 'owner', 'status', 'summary')} for t in run['tasks']])
                cards.append(card)
            conversation = 'group' if channel == 'group' else 'private:' + channel
            return {'team': team, 'channel': channel, 'messages': store.messages(conversation, limit=100), 'runs': cards,
                    'memory': {a: store.memory(a) for a in AGENTS}, 'job': jobs.info(store.root)}

    @app.get('/api/teams/{team_id}/export')
    def export_team(request: Request, team_id: str, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            get_team(store, team_id)
            result = team_audit(store, team_id, mode)
            return JSONResponse(result, headers={'Content-Disposition': f'attachment; filename="{team_id}-audit.json"'})

    @app.get('/api/teams/{team_id}/evaluation.zip')
    def export_team_evaluation(request: Request, team_id: str, mode: Mode = 'demo'):
        with storage(request, mode) as store:
            get_team(store, team_id)
            try:
                bundle = evaluation_zip(store, team_audit(store, team_id, mode))
            except EvaluationExportError as exc:
                raise HTTPException(409, str(exc)) from None
        bundle.seek(0, 2)
        length = bundle.tell()
        bundle.seek(0)
        return StreamingResponse(zip_chunks(bundle), media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename="{team_id}-evaluation.zip"',
                     'Content-Length': str(length)}, background=BackgroundTask(bundle.close))

    @app.post('/api/runs/{run_id}/rerun', status_code=202)
    def rerun(request: Request, run_id: str, mode: Mode = 'demo'):
        target = workspace(request, mode)
        event = jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                old = get_run(store, run_id)
                if len(store.runs()) >= 200:
                    raise HTTPException(429, '当前空间已达到任务上限。')
                cfg = Config(**old['config'])
                run = Engine(store, cfg, None).rerun(old)
                result = public_run(run, store)
            jobs.submit(target, run['id'], cfg, event)
            return result
        except Exception:
            jobs.release(target)
            raise

    @app.get("/api/agents/{agent}/memory")
    def memory(request: Request, agent: Agent, mode: Mode = "demo", team_id: str | None = None):
        with storage(request, mode) as store:
            if team_id:
                get_team(store, team_id)
                store.bind_team(team_id)
            return store.memory(agent)

    @app.get("/api/chat")
<<<<<<< HEAD
    def chat_history(request: Request, channel: Channel = "group", mode: Mode = "demo", run_id: str | None = None):
        with storage(request, mode) as store:
            if run_id:
                get_run(store, run_id)
                if channel != 'group':
                    raise HTTPException(422, '任务筛选仅用于团队群聊。')
            if channel == 'group':
                return team_messages(store, run_id=run_id)
            return store.messages('private:' + channel, limit=100, metadata=True)
=======
    def chat_history(request: Request, channel: Channel = "group", mode: Mode = "demo", team_id: str | None = None):
        with storage(request, mode) as store:
            if team_id:
                get_team(store, team_id)
                store.bind_team(team_id)
            return store.messages("group" if channel == "group" else "private:" + channel, limit=100)
>>>>>>> b8fb6de (增加自定义agent功能)

    @app.post("/api/chat")
    def chat(request: Request, body: ChatInput, mode: Mode = "demo"):
        if not body.message.strip():
            raise HTTPException(422, "消息不能为空。")
        target = workspace(request, mode)
        jobs.reserve(target)
        try:
            with storage(request, mode) as store, store.lock():
                team = get_team(store, body.team_id) if body.team_id else None
                store.bind_team(body.team_id)
                channel = "group" if body.channel == "group" else "private:" + body.channel
                agent = "coordinator" if body.channel == "group" else body.channel
                store.message(channel, "human", body.message.strip())
                try:
                    from ..interaction import INTERACTION_POLICY, chinese_prose
                    config = run_config(mode)
                    provider = make_provider(config)
                    context = {
                        "agent": agent, "channel": channel, "history": store.messages(channel, agent),
                        'team': team, 'role_profile': team['roles'][agent] if team else None,
                        'interaction_policy': INTERACTION_POLICY}
                    for attempt in range(2):
                        result = provider.complete('chat', context)
                        reply = result.get('reply')
                        try:
                            if not isinstance(reply, str) or not reply.strip():
                                raise ValueError('回复不能为空。')
                            if config.provider != 'demo':
                                chinese_prose(reply, '对话回复')
                                if len(reply) > 600 or reply.count('?') + reply.count('？') > 1:
                                    raise ValueError('请用不超过 600 字的中文简短回复，每次最多问一个问题。')
                            break
                        except ValueError as exc:
                            if attempt == 1:
                                raise
                            context.update(validation_error=str(exc), previous_reply=reply)
                except (ValueError, RuntimeError, OSError) as exc:
                    # APIProvider returns sanitized errors, no upstream bodies/headers.
                    error = str(exc)[:500]
                    raise HTTPException(502, error) from None
                store.message(channel, agent, reply[:32000])
                return {"sender": agent, "body": reply[:32000]}
        finally:
            jobs.release(target)

    app.mount("/assets", StaticFiles(directory=HERE / "static"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(HERE / "static" / "index.html")

    return app
