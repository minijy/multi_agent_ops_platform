from __future__ import annotations

import random
import re
import time
from typing import Any, Callable


class ModelProviderError(RuntimeError):
    """Upstream model failure that must remain a normal exception.

    Frozen dataclasses cannot accept ``__traceback__`` assignment, and LangGraph
    attaches traceback in a context manager. Keep this a regular exception.
    """

    def __init__(
        self,
        *,
        provider: str,
        code: str,
        user_message: str,
        status_code: int = 503,
        retry_after_seconds: int = 5,
        automatic_retry: bool = False,
    ) -> None:
        super().__init__(user_message)
        self.provider = provider
        self.code = code
        self.user_message = user_message
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.automatic_retry = automatic_retry

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.user_message,
            "provider": self.provider,
            "retry_after_seconds": self.retry_after_seconds,
        }


def _error_code(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("code") is not None:
            return str(error["code"])
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            payload = response.json()
            error = payload.get("error", payload)
            if isinstance(error, dict) and error.get("code") is not None:
                return str(error["code"])
        except Exception:
            pass
    match = re.search(r"""["']?code["']?\s*:\s*["']?(\d+)""", str(exc))
    return match.group(1) if match else "upstream_error"


def _retry_after(exc: Exception, default: int) -> int:
    response = getattr(exc, "response", None)
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return max(1, int(float(raw)))
            except ValueError:
                pass
    return default


def classify_zhipu_error(
    exc: Exception, *, rate_limit_cooldown_seconds: int = 30
) -> ModelProviderError:
    code = _error_code(exc)
    status = int(getattr(exc, "status_code", 503) or 503)
    if code == "1302":
        return ModelProviderError(
            provider="zhipu",
            code=code,
            user_message="请求过于频繁，已触发模型速率限制，请稍后再试。",
            status_code=429,
            retry_after_seconds=_retry_after(
                exc, rate_limit_cooldown_seconds
            ),
            automatic_retry=False,
        )
    if code == "1305":
        return ModelProviderError(
            provider="zhipu",
            code=code,
            user_message="免费模型当前访问量较大，正在排队，请稍后再试。",
            status_code=429,
            retry_after_seconds=_retry_after(exc, 5),
            automatic_retry=True,
        )
    if code == "1308":
        return ModelProviderError(
            provider="zhipu",
            code=code,
            user_message="当前模型的周期使用额度已耗尽，请在额度重置后重试。",
            status_code=429,
            retry_after_seconds=_retry_after(exc, 300),
            automatic_retry=False,
        )
    if code == "1113":
        return ModelProviderError(
            provider="zhipu",
            code=code,
            user_message="模型账户当前没有可用余额或资源包。",
            status_code=429,
            retry_after_seconds=300,
            automatic_retry=False,
        )
    return ModelProviderError(
        provider="zhipu",
        code=code,
        user_message="模型服务暂时不可用，请稍后重试。",
        status_code=status if status >= 400 else 503,
        retry_after_seconds=_retry_after(exc, 5),
        automatic_retry=status >= 500,
    )


def invoke_zhipu_with_backoff(
    create: Callable[..., Any],
    options: dict[str, Any],
    *,
    max_retries: int,
    backoff_base_seconds: float,
    rate_limit_cooldown_seconds: int,
) -> Any:
    from zai.core._errors import APIStatusError

    for attempt in range(max_retries + 1):
        try:
            return create(**options)
        except APIStatusError as exc:
            classified = classify_zhipu_error(
                exc,
                rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
            )
            if not classified.automatic_retry or attempt >= max_retries:
                raise classified from exc
            ceiling = backoff_base_seconds * (2**attempt)
            time.sleep(random.uniform(0, max(0.0, ceiling)))
    raise AssertionError("unreachable")
