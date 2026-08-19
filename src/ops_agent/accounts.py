from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

from .config import Settings


class AccountError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise AccountError("weak_password", "密码至少需要 10 个字符。")
    if not any(char.islower() for char in password):
        raise AccountError("weak_password", "密码必须包含小写字母。")
    if not any(char.isupper() for char in password):
        raise AccountError("weak_password", "密码必须包含大写字母。")
    if not any(char.isdigit() for char in password):
        raise AccountError("weak_password", "密码必须包含数字。")


def generate_temporary_password() -> str:
    return f"Ark-{secrets.token_urlsafe(9)}-7a"


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts(
                    tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,
                    role TEXT NOT NULL,password_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,last_login_at TEXT,password_changed_at TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS account_sessions(
                    session_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,expires_at TEXT NOT NULL,revoked_at TEXT,
                    created_at TEXT NOT NULL,last_used_at TEXT NOT NULL,
                    FOREIGN KEY(tenant_id,user_id) REFERENCES accounts(tenant_id,user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_account_sessions_owner
                    ON account_sessions(tenant_id,user_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        return {
            "tenant_id": row["tenant_id"], "user_id": row["user_id"],
            "display_name": row["display_name"], "role": row["role"],
            "enabled": bool(row["enabled"]),
            "must_change_password": bool(row["must_change_password"]),
            "locked_until": row["locked_until"], "last_login_at": row["last_login_at"],
            "password_changed_at": row["password_changed_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def count(self, tenant_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0])

    def get(self, tenant_id: str, user_id: str, *, private: bool = False) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
            ).fetchone()
        if not row:
            return None
        result = self._public(row)
        if private:
            result.update(password_hash=row["password_hash"], failed_attempts=row["failed_attempts"])
        return result

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts WHERE tenant_id=? ORDER BY display_name,user_id", (tenant_id,)
            ).fetchall()
        return [self._public(row) for row in rows]

    def put(self, tenant_id: str, user_id: str, display_name: str, role: str, password: str,
            enabled: bool, must_change_password: bool) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        encoded = _hash_password(password)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO accounts(tenant_id,user_id,display_name,role,password_hash,enabled,
                   must_change_password,created_at,updated_at,password_changed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                   display_name=excluded.display_name,role=excluded.role,password_hash=excluded.password_hash,
                   enabled=excluded.enabled,must_change_password=excluded.must_change_password,
                   failed_attempts=0,locked_until=NULL,updated_at=excluded.updated_at,
                   password_changed_at=excluded.password_changed_at""",
                (tenant_id, user_id, display_name, role, encoded, int(enabled),
                 int(must_change_password), now, now, now),
            )
        return self.get(tenant_id, user_id) or {}

    def update_profile(self, tenant_id: str, user_id: str, display_name: str, role: str,
                       enabled: bool) -> dict[str, Any] | None:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE accounts SET display_name=?,role=?,enabled=?,updated_at=?
                   WHERE tenant_id=? AND user_id=?""",
                (display_name, role, int(enabled), datetime.now(UTC).isoformat(), tenant_id, user_id),
            ).rowcount
        return self.get(tenant_id, user_id) if changed else None

    def record_login_failure(self, tenant_id: str, user_id: str, max_attempts: int,
                             lock_minutes: int) -> None:
        account = self.get(tenant_id, user_id, private=True)
        if not account:
            return
        attempts = int(account["failed_attempts"]) + 1
        locked_until = (
            (datetime.now(UTC) + timedelta(minutes=lock_minutes)).isoformat()
            if attempts >= max_attempts else None
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE accounts SET failed_attempts=?,locked_until=?,updated_at=? WHERE tenant_id=? AND user_id=?",
                (attempts, locked_until, datetime.now(UTC).isoformat(), tenant_id, user_id),
            )

    def record_login_success(self, tenant_id: str, user_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """UPDATE accounts SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=?
                   WHERE tenant_id=? AND user_id=?""", (now, now, tenant_id, user_id)
            )

    def change_password(self, tenant_id: str, user_id: str, password: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """UPDATE accounts SET password_hash=?,must_change_password=0,failed_attempts=0,
                   locked_until=NULL,password_changed_at=?,updated_at=? WHERE tenant_id=? AND user_id=?""",
                (_hash_password(password), now, now, tenant_id, user_id),
            )

    def delete(self, tenant_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM accounts WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
            ).rowcount > 0

    def create_session(self, tenant_id: str, user_id: str, token_hash: str,
                       expires_at: str) -> str:
        session_id = secrets.token_hex(16)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO account_sessions(session_id,tenant_id,user_id,token_hash,expires_at,
                   created_at,last_used_at) VALUES(?,?,?,?,?,?,?)""",
                (session_id, tenant_id, user_id, token_hash, expires_at, now, now),
            )
        return session_id

    def consume_session(self, token_hash: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM account_sessions WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if not row or row["revoked_at"] or datetime.fromisoformat(row["expires_at"]) <= now:
                return None
            connection.execute(
                "UPDATE account_sessions SET revoked_at=?,last_used_at=? WHERE session_id=?",
                (now.isoformat(), now.isoformat(), row["session_id"]),
            )
        return dict(row)

    def revoke_session(self, token_hash: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "UPDATE account_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (datetime.now(UTC).isoformat(), token_hash),
            ).rowcount > 0

    def revoke_user_sessions(self, tenant_id: str, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE account_sessions SET revoked_at=? WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL",
                (datetime.now(UTC).isoformat(), tenant_id, user_id),
            )


class PostgresAccountStore(AccountStore):
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        statements = (
            """CREATE TABLE IF NOT EXISTS ops_accounts(
                tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,
                role TEXT NOT NULL,password_hash TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE,
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
                failed_attempts INTEGER NOT NULL DEFAULT 0,locked_until TIMESTAMPTZ,
                last_login_at TIMESTAMPTZ,password_changed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,user_id))""",
            """CREATE TABLE IF NOT EXISTS ops_account_sessions(
                session_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,expires_at TIMESTAMPTZ NOT NULL,
                revoked_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL,last_used_at TIMESTAMPTZ NOT NULL,
                FOREIGN KEY(tenant_id,user_id) REFERENCES ops_accounts(tenant_id,user_id) ON DELETE CASCADE)""",
            """CREATE INDEX IF NOT EXISTS ix_ops_account_sessions_owner
                ON ops_account_sessions(tenant_id,user_id)""",
        )
        with self._connect() as connection:
            for statement in statements:
                connection.execute(statement)

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        def timestamp(name: str) -> str | None:
            value = row[name]
            return value.isoformat() if value is not None else None

        return {
            "tenant_id": row["tenant_id"], "user_id": row["user_id"],
            "display_name": row["display_name"], "role": row["role"],
            "enabled": bool(row["enabled"]),
            "must_change_password": bool(row["must_change_password"]),
            "locked_until": timestamp("locked_until"), "last_login_at": timestamp("last_login_at"),
            "password_changed_at": timestamp("password_changed_at"),
            "created_at": timestamp("created_at"), "updated_at": timestamp("updated_at"),
        }

    def count(self, tenant_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM ops_accounts WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()
        return int(row["count"])

    def get(self, tenant_id: str, user_id: str, *, private: bool = False) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_accounts WHERE tenant_id=%s AND user_id=%s",
                (tenant_id, user_id),
            ).fetchone()
        if not row:
            return None
        result = self._public(row)
        if private:
            result.update(password_hash=row["password_hash"], failed_attempts=row["failed_attempts"])
        return result

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ops_accounts WHERE tenant_id=%s ORDER BY display_name,user_id",
                (tenant_id,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def put(self, tenant_id: str, user_id: str, display_name: str, role: str, password: str,
            enabled: bool, must_change_password: bool) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO ops_accounts(tenant_id,user_id,display_name,role,password_hash,enabled,
                   must_change_password,created_at,updated_at,password_changed_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                   display_name=EXCLUDED.display_name,role=EXCLUDED.role,password_hash=EXCLUDED.password_hash,
                   enabled=EXCLUDED.enabled,must_change_password=EXCLUDED.must_change_password,
                   failed_attempts=0,locked_until=NULL,updated_at=EXCLUDED.updated_at,
                   password_changed_at=EXCLUDED.password_changed_at""",
                (tenant_id, user_id, display_name, role, _hash_password(password), enabled,
                 must_change_password, now, now, now),
            )
        return self.get(tenant_id, user_id) or {}

    def update_profile(self, tenant_id: str, user_id: str, display_name: str, role: str,
                       enabled: bool) -> dict[str, Any] | None:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE ops_accounts SET display_name=%s,role=%s,enabled=%s,updated_at=%s
                   WHERE tenant_id=%s AND user_id=%s""",
                (display_name, role, enabled, datetime.now(UTC), tenant_id, user_id),
            ).rowcount
        return self.get(tenant_id, user_id) if changed else None

    def record_login_failure(self, tenant_id: str, user_id: str, max_attempts: int,
                             lock_minutes: int) -> None:
        account = self.get(tenant_id, user_id, private=True)
        if not account:
            return
        attempts = int(account["failed_attempts"]) + 1
        locked_until = datetime.now(UTC) + timedelta(minutes=lock_minutes) if attempts >= max_attempts else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE ops_accounts SET failed_attempts=%s,locked_until=%s,updated_at=%s WHERE tenant_id=%s AND user_id=%s",
                (attempts, locked_until, datetime.now(UTC), tenant_id, user_id),
            )

    def record_login_success(self, tenant_id: str, user_id: str) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """UPDATE ops_accounts SET failed_attempts=0,locked_until=NULL,last_login_at=%s,updated_at=%s
                   WHERE tenant_id=%s AND user_id=%s""", (now, now, tenant_id, user_id)
            )

    def change_password(self, tenant_id: str, user_id: str, password: str) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """UPDATE ops_accounts SET password_hash=%s,must_change_password=FALSE,failed_attempts=0,
                   locked_until=NULL,password_changed_at=%s,updated_at=%s WHERE tenant_id=%s AND user_id=%s""",
                (_hash_password(password), now, now, tenant_id, user_id),
            )

    def delete(self, tenant_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM ops_accounts WHERE tenant_id=%s AND user_id=%s", (tenant_id, user_id)
            ).rowcount > 0

    def create_session(self, tenant_id: str, user_id: str, token_hash: str,
                       expires_at: str) -> str:
        session_id = secrets.token_hex(16)
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO ops_account_sessions(session_id,tenant_id,user_id,token_hash,expires_at,
                   created_at,last_used_at) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (session_id, tenant_id, user_id, token_hash, datetime.fromisoformat(expires_at), now, now),
            )
        return session_id

    def consume_session(self, token_hash: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_account_sessions WHERE token_hash=%s FOR UPDATE", (token_hash,)
            ).fetchone()
            if not row or row["revoked_at"] or row["expires_at"] <= now:
                return None
            connection.execute(
                "UPDATE ops_account_sessions SET revoked_at=%s,last_used_at=%s WHERE session_id=%s",
                (now, now, row["session_id"]),
            )
        return dict(row)

    def revoke_session(self, token_hash: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "UPDATE ops_account_sessions SET revoked_at=%s WHERE token_hash=%s AND revoked_at IS NULL",
                (datetime.now(UTC), token_hash),
            ).rowcount > 0

    def revoke_user_sessions(self, tenant_id: str, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE ops_account_sessions SET revoked_at=%s WHERE tenant_id=%s AND user_id=%s AND revoked_at IS NULL",
                (datetime.now(UTC), tenant_id, user_id),
            )


class AccountService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = (
            PostgresAccountStore(settings.postgres_dsn)
            if settings.control_plane_backend == "postgres"
            else AccountStore(settings.platform_db_path)
        )
        self.secret = self._load_secret(settings)

    @staticmethod
    def _load_secret(settings: Settings) -> str:
        if settings.jwt_secret:
            return settings.jwt_secret
        path = settings.platform_db_path.with_suffix(".auth-secret")
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        secret = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        os.chmod(path, 0o600)
        return secret

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def configured(self, tenant_id: str) -> bool:
        return self.store.count(tenant_id) > 0

    def account(self, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        return self.store.get(tenant_id, user_id)

    def list_accounts(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.store.list(tenant_id)

    def register(self, tenant_id: str, user_id: str, display_name: str,
                 password: str) -> dict[str, Any]:
        validate_password(password)
        if self.store.get(tenant_id, user_id):
            raise AccountError("account_exists", "该用户 ID 已注册。", status_code=409)
        if self.configured(tenant_id):
            raise AccountError(
                "registration_closed",
                "该租户已完成初始化，请联系管理员创建账户并获取临时密码。",
                status_code=403,
            )
        return self.store.put(
            tenant_id, user_id, display_name, "admin", password, True, False
        )

    def provision(self, tenant_id: str, user_id: str, display_name: str, role: str,
                  enabled: bool, password: str | None, generate: bool) -> tuple[dict[str, Any], str | None]:
        existing = self.store.get(tenant_id, user_id)
        temporary = generate_temporary_password() if generate else password
        if temporary:
            validate_password(temporary)
            account = self.store.put(
                tenant_id, user_id, display_name, role, temporary, enabled, True
            )
            self.store.revoke_user_sessions(tenant_id, user_id)
            return account, temporary if generate else None
        if not existing:
            raise AccountError("temporary_password_required", "创建账户时必须设置或生成临时密码。")
        return self.store.update_profile(tenant_id, user_id, display_name, role, enabled) or existing, None

    def authenticate(self, tenant_id: str, user_id: str, password: str) -> dict[str, Any]:
        account = self.store.get(tenant_id, user_id, private=True)
        generic = AccountError("invalid_credentials", "用户 ID 或密码错误。", status_code=401)
        if not account:
            hashlib.scrypt(password.encode(), salt=b"0" * 16, n=2**14, r=8, p=1, dklen=32)
            raise generic
        if not account["enabled"]:
            raise AccountError("account_disabled", "账户已停用，请联系管理员。", status_code=403)
        locked_until = account.get("locked_until")
        if locked_until and datetime.fromisoformat(locked_until) > datetime.now(UTC):
            raise AccountError("account_locked", "登录失败次数过多，账户已临时锁定。", status_code=423)
        if not _verify_password(password, account["password_hash"]):
            self.store.record_login_failure(
                tenant_id, user_id, self.settings.account_max_login_attempts,
                self.settings.account_lock_minutes,
            )
            raise generic
        self.store.record_login_success(tenant_id, user_id)
        return self.store.get(tenant_id, user_id) or {}

    def issue_tokens(self, account: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=self.settings.account_access_token_minutes)
        payload: dict[str, Any] = {
            "sub": account["user_id"], "tenant_id": account["tenant_id"],
            "role": account["role"], "type": "access", "iat": now, "exp": expires,
        }
        if self.settings.jwt_issuer:
            payload["iss"] = self.settings.jwt_issuer
        if self.settings.jwt_audience:
            payload["aud"] = self.settings.jwt_audience
        access_token = jwt.encode(payload, self.secret, algorithm="HS256")
        refresh_token = secrets.token_urlsafe(48)
        refresh_expires = now + timedelta(days=self.settings.account_refresh_token_days)
        self.store.create_session(
            account["tenant_id"], account["user_id"], self._token_hash(refresh_token),
            refresh_expires.isoformat(),
        )
        return {
            "access_token": access_token, "refresh_token": refresh_token,
            "token_type": "bearer", "expires_in": int((expires - now).total_seconds()),
            "account": account,
        }

    def principal(self, token: str) -> dict[str, str]:
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=["HS256"],
                issuer=self.settings.jwt_issuer or None,
                audience=self.settings.jwt_audience or None,
                options={"require": ["sub", "tenant_id", "role"]},
            )
        except jwt.PyJWTError as exc:
            raise AccountError("invalid_access_token", "登录已过期，请重新登录。", status_code=401) from exc
        if payload.get("type") not in {None, "access"}:
            raise AccountError("invalid_access_token", "令牌类型无效。", status_code=401)
        account = self.store.get(str(payload["tenant_id"]), str(payload["sub"]))
        if not account or not account["enabled"]:
            raise AccountError("account_unavailable", "账户不存在或已停用。", status_code=401)
        return {"tenant_id": account["tenant_id"], "user_id": account["user_id"], "role": account["role"]}

    def refresh(self, token: str) -> dict[str, Any]:
        session = self.store.consume_session(self._token_hash(token))
        if not session:
            raise AccountError("invalid_refresh_token", "刷新令牌无效或已过期。", status_code=401)
        account = self.store.get(session["tenant_id"], session["user_id"])
        if not account or not account["enabled"]:
            raise AccountError("account_unavailable", "账户不存在或已停用。", status_code=401)
        return self.issue_tokens(account)

    def change_password(self, tenant_id: str, user_id: str, current: str, new: str) -> dict[str, Any]:
        account = self.store.get(tenant_id, user_id, private=True)
        if not account or not _verify_password(current, account["password_hash"]):
            raise AccountError("current_password_invalid", "当前密码错误。", status_code=400)
        validate_password(new)
        if _verify_password(new, account["password_hash"]):
            raise AccountError("password_reused", "新密码不能与当前密码相同。")
        self.store.change_password(tenant_id, user_id, new)
        self.store.revoke_user_sessions(tenant_id, user_id)
        return self.store.get(tenant_id, user_id) or {}

    def reset_password(self, tenant_id: str, user_id: str, password: str | None,
                       generate: bool) -> tuple[dict[str, Any], str | None]:
        account = self.store.get(tenant_id, user_id)
        if not account:
            raise AccountError("account_not_found", "账户不存在。", status_code=404)
        temporary = generate_temporary_password() if generate else password
        if not temporary:
            raise AccountError("temporary_password_required", "请设置或生成临时密码。")
        validate_password(temporary)
        updated = self.store.put(
            tenant_id, user_id, account["display_name"], account["role"], temporary,
            account["enabled"], True,
        )
        self.store.revoke_user_sessions(tenant_id, user_id)
        return updated, temporary if generate else None


def create_account_service(settings: Settings) -> AccountService:
    return AccountService(settings)
