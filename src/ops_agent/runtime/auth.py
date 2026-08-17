from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..config import Settings


def principal_from_bearer(token: str, settings: Settings) -> dict[str, str]:
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(status_code=401, detail="JWT support is not installed") from exc
    options = {"require": ["sub"]}
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer or None,
            audience=settings.jwt_audience or None,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    role = str(payload.get("role") or payload.get("user_role") or "")
    tenant_id = str(payload.get("tenant_id") or payload.get("tid") or "")
    user_id = str(payload.get("sub") or payload.get("user_id") or "")
    if not tenant_id or not user_id or not role:
        raise HTTPException(
            status_code=401,
            detail="bearer token missing tenant_id, sub, or role",
        )
    return {"tenant_id": tenant_id, "user_id": user_id, "role": role}
