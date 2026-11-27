"""Local account and session storage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .schemas import AuthData, UserInfo


class R1ServiceError(Exception):
    """Expected, safe-to-expose service failure at the HTTP boundary."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = status_code if code is None else code
        self.headers = dict(headers or {})

@dataclass(frozen=True, slots=True)
class _LocalUser:
    user_id: int
    username: str
    nickname: str
    password_hash: str
    role: str = "USER"

    def info(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "username": self.username,
            "nickname": self.nickname,
            "avatar": None,
            "role": self.role,
        }

class LocalAuthService:
    """Local accounts with process-local sessions and optional on-disk identity."""

    _ITERATIONS = 210_000
    _TOKEN_TTL_SECONDS = 24 * 60 * 60
    _MAX_FAILURES = 8
    _FAILURE_WINDOW_SECONDS = 10 * 60

    def __init__(self, accounts: Path | None = None) -> None:
        self.accounts = accounts
        self._users: dict[str, _LocalUser] = {}
        self._tokens: dict[str, tuple[int, float]] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._next_id = 1

    def register(self, username: str, password: str, nickname: str | None) -> AuthData:
        normalized = username.strip()
        if normalized in self._users:
            raise R1ServiceError("该账号已存在", status_code=409)
        if not nickname or not nickname.strip():
            normalized_nickname = f"用户{self._next_id}"
        else:
            normalized_nickname = nickname.strip()
        user = _LocalUser(
            user_id=self._next_id,
            username=normalized,
            nickname=normalized_nickname,
            password_hash=self._hash_password(password),
        )
        if self.accounts is not None:
            with closing(self._account_db()) as db, db:
                try:
                    uid = db.execute(
                        "INSERT INTO accounts(username,nickname,password_hash) VALUES(?,?,?)",
                        (normalized, normalized_nickname, user.password_hash),
                    ).lastrowid
                except sqlite3.IntegrityError as exc:
                    raise R1ServiceError("该账号已存在", status_code=409) from exc
                user = _LocalUser(uid, normalized, normalized_nickname, user.password_hash)
        self._next_id += 1
        self._users[normalized] = user
        return AuthData(
            userInfo=UserInfo.model_validate(user.info()),
            token=None,
        )

    def login(self, username: str, password: str) -> AuthData:
        normalized = username.strip()
        now = time.monotonic()
        failure_count, expires = self._failures.get(normalized, (0, 0.0))
        if expires <= now:
            self._failures.pop(normalized, None)
            failure_count = 0
        if failure_count >= self._MAX_FAILURES:
            raise R1ServiceError("登录尝试过于频繁，请稍后再试", status_code=429)
        user = self._users.get(normalized)
        if user is None and self.accounts is not None:
            with closing(self._account_db()) as db:
                row = db.execute(
                    "SELECT id,username,nickname,password_hash FROM accounts WHERE username=?",
                    (normalized,),
                ).fetchone()
            if row:
                user = self._users[normalized] = _LocalUser(*row)
        if user is None or not self._matches(password, user.password_hash):
            self._failures[normalized] = (
                failure_count + 1,
                now + self._FAILURE_WINDOW_SECONDS,
            )
            raise R1ServiceError("账号或密码错误", status_code=401)
        self._failures.pop(normalized, None)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (user.user_id, now + self._TOKEN_TTL_SECONDS)
        return AuthData(
            userInfo=UserInfo.model_validate(user.info()),
            token=token,
        )

    def require(self, authorization: str | None) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise R1ServiceError("请先登录", status_code=401)
        token = authorization[7:].strip()
        if not token:
            raise R1ServiceError("无效的登录凭证", status_code=401)
        session = self._tokens.get(token)
        if session is None:
            raise R1ServiceError("登录状态已失效", status_code=401)
        user_id, expires = session
        if expires <= time.monotonic():
            self._tokens.pop(token, None)
            raise R1ServiceError("登录状态已失效", status_code=401)
        for user in self._users.values():
            if user.user_id == user_id:
                return user.info()
        self._tokens.pop(token, None)
        raise R1ServiceError("登录状态已失效", status_code=401)

    def logout(self, authorization: str | None) -> None:
        if authorization and authorization.startswith("Bearer "):
            self._tokens.pop(authorization[7:].strip(), None)

    def _account_db(self):
        db = sqlite3.connect(self.accounts, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, "
                   "username TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, password_hash TEXT NOT NULL)")
        return db

    @classmethod
    def _hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls._ITERATIONS, dklen=32
        )
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        return f"pbkdf2${cls._ITERATIONS}${encode(salt)}${encode(digest)}"

    @classmethod
    def _matches(cls, password: str, stored: str) -> bool:
        try:
            prefix, iterations, encoded_salt, encoded_digest = stored.split("$", 3)
            if prefix != "pbkdf2":
                return False
            padding = "=" * (-len(encoded_salt) % 4)
            salt = base64.urlsafe_b64decode(encoded_salt + padding)
            padding = "=" * (-len(encoded_digest) % 4)
            expected = base64.urlsafe_b64decode(encoded_digest + padding)
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations), dklen=32
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False
