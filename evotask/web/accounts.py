"""Local account identities and revocable, opaque login sessions.

Only token digests and salted password hashes are persisted. Connections belong
to one operation/thread; workspace authorization never trusts a client user ID.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import HTTPException


ACCOUNT_COOKIE = 'evotask_account'
SESSION_SECONDS = 30 * 86400
_KDF_SLOTS = threading.BoundedSemaphore(2)


def username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,39}', value):
        raise HTTPException(422, '用户名需为 3–40 位英文字母、数字、点、下划线或短横线，并以字母或数字开头。')
    return value


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    if not _KDF_SLOTS.acquire(blocking=False):
        raise HTTPException(429, '登录服务繁忙，请稍后重试。')
    try:
        derived = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                                 n=2**17, r=8, p=1, maxmem=256 * 1024**2, dklen=32)
    finally:
        _KDF_SLOTS.release()
    return f'scrypt$131072$8$1${salt}${derived.hex()}'


def password_matches(password: str, encoded: str | None) -> bool:
    # Unknown users still perform the same expensive derivation.
    salt = encoded.split('$')[4] if encoded else '0' * 32
    actual = password_hash(password, salt)
    return hmac.compare_digest(actual, encoded or '')


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, root: Path):
        self.path = root / 'accounts.sqlite3'
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL, created INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_sessions (
                    digest TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
                    created INTEGER NOT NULL, seen INTEGER NOT NULL, expires INTEGER NOT NULL,
                    device TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS sessions_by_user ON account_sessions(user_id);
                CREATE TABLE IF NOT EXISTS auth_attempts (bucket TEXT NOT NULL, at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_by_bucket ON auth_attempts(bucket,at);
            ''')

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def throttle(self, address: str, identity: str = '') -> None:
        now = int(time.time())
        buckets = [('ip:' + token_digest(address), 20)]
        if identity:
            buckets.append(('name:' + token_digest(identity), 10))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_attempts WHERE at<=?', (now - 60,))
            for bucket, limit in buckets:
                count = db.execute('SELECT COUNT(*) FROM auth_attempts WHERE bucket=?', (bucket,)).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, '尝试过于频繁，请一分钟后重试。', headers={'Retry-After': '60'})
            db.executemany('INSERT INTO auth_attempts VALUES(?,?)', [(b, now) for b, _ in buckets])

    @staticmethod
    def public(row) -> dict:
        return {key: row[key] for key in ('id', 'username', 'display_name', 'created')}

    def register(self, name: str, display_name: str, password: str) -> dict:
        name = username(name)
        if not 12 <= len(password) <= 128:
            raise HTTPException(422, '密码长度需为 12–128 个字符。')
        encoded = password_hash(password)
        user = dict(id='user_' + secrets.token_hex(16), username=name,
                    display_name=display_name.strip() or name, created=int(time.time()))
        try:
            with self.connection() as db:
                db.execute('INSERT INTO users VALUES(?,?,?,?,?)',
                           (user['id'], name, user['display_name'], encoded, user['created']))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该用户名无法注册，请尝试其他名称或登录已有账号。') from None
        return user

    def login(self, name: str, password: str, device: str) -> str:
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (name.strip().lower(),)).fetchone()
        if not password_matches(password, row['password_hash'] if row else None):
            raise HTTPException(401, '用户名或密码不正确。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT password_hash FROM users WHERE id=?', (row['id'],)).fetchone()
            if not current or not hmac.compare_digest(current['password_hash'], row['password_hash']):
                raise HTTPException(401, '账号信息已更新，请使用新密码重新登录。')
            return self.issue(row['id'], device, db)

    def issue(self, user_id: str, device: str, db=None) -> str:
        if db is None:
            with self.connection() as connection:
                return self.issue(user_id, device, connection)
        now, token = int(time.time()), secrets.token_urlsafe(32)
        db.execute('DELETE FROM account_sessions WHERE expires<=?', (now,))
        db.execute('INSERT INTO account_sessions VALUES(?,?,?,?,?,?,?)',
                   (token_digest(token), 'device_' + secrets.token_hex(12), user_id,
                    now, now, now + SESSION_SECONDS, device[:200]))
        return token

    def resolve(self, token: str) -> dict | None:
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        now, digest = int(time.time()), token_digest(token)
        with self.connection() as db:
            row = db.execute('''SELECT u.*,s.id AS session_id,s.expires FROM users u
                JOIN account_sessions s ON s.user_id=u.id WHERE s.digest=? AND s.expires>?''', (digest, now)).fetchone()
            if row is None:
                return None
            db.execute('UPDATE account_sessions SET seen=? WHERE digest=? AND seen<?', (now, digest, now - 300))
        return self.public(row) | {'session_id': row['session_id'], 'expires': row['expires']}

    def sessions(self, user_id: str, current: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute('''SELECT id,created,seen,expires,device FROM account_sessions
                WHERE user_id=? AND expires>? ORDER BY seen DESC''', (user_id, int(time.time()))).fetchall()
        return [dict(row) | {'current': row['id'] == current} for row in rows]

    def revoke(self, token: str) -> None:
        with self.connection() as db:
            db.execute('DELETE FROM account_sessions WHERE digest=?', (token_digest(token),))

    def revoke_others(self, user_id: str, current: str) -> int:
        with self.connection() as db:
            return db.execute('DELETE FROM account_sessions WHERE user_id=? AND id!=?', (user_id, current)).rowcount

    def change_password(self, user_id: str, old: str, new: str, device: str) -> str:
        if not 12 <= len(new) <= 128:
            raise HTTPException(422, '新密码长度需为 12–128 个字符。')
        with self.connection() as db:
            row = db.execute('SELECT password_hash FROM users WHERE id=?', (user_id,)).fetchone()
        if not row or not password_matches(old, row['password_hash']):
            raise HTTPException(401, '当前密码不正确。')
        encoded = password_hash(new)
        with self.connection() as db:
            changed = db.execute('UPDATE users SET password_hash=? WHERE id=? AND password_hash=?',
                                 (encoded, user_id, row['password_hash'])).rowcount
            if not changed:
                raise HTTPException(409, '账号信息已更新，请重新登录。')
            db.execute('DELETE FROM account_sessions WHERE user_id=?', (user_id,))
            return self.issue(user_id, device, db)
