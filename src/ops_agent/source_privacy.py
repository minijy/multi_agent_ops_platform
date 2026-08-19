from __future__ import annotations

import re
from typing import Any


# Physical storage identifiers are implementation details. They must never be
# shown to the model or an end user; tools expose these stable business labels.
PROFIT_WAREHOUSE_SOURCE = "领星利润分析数据（分析仓）"
LINGXING_LIVE_SOURCE = "领星利润报表（实时）"
AMAZON_FINANCE_SOURCE = "Amazon 财务数据"
KINGDEE_SOURCE = "金蝶云星空业务数据"


_PRIVATE_IDENTIFIERS: dict[str, str] = {
    "lingxing_profit_order_transactions": PROFIT_WAREHOUSE_SOURCE,
    "amazon_finance_transactions": AMAZON_FINANCE_SOURCE,
    "amazon_finance_amount_lines": AMAZON_FINANCE_SOURCE,
    "amazon_finance_items": AMAZON_FINANCE_SOURCE,
    "amazon_finance_transaction_identifiers": AMAZON_FINANCE_SOURCE,
}
_PRIVATE_IDENTIFIER_PATTERN = re.compile(
    "|".join(
        re.escape(item)
        for item in sorted(_PRIVATE_IDENTIFIERS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)


def sanitize_public_text(value: str | None) -> str:
    """Replace physical data identifiers with business-facing source names."""
    if not value:
        return ""
    return _PRIVATE_IDENTIFIER_PATTERN.sub(
        lambda match: _PRIVATE_IDENTIFIERS[match.group(0).lower()], value
    )


def sanitize_public_value(value: Any) -> Any:
    """Recursively sanitize model-facing and API-facing tool payloads."""
    if isinstance(value, str):
        return sanitize_public_text(value)
    if isinstance(value, dict):
        return {
            sanitize_public_text(key) if isinstance(key, str) else key:
            sanitize_public_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_public_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_public_value(item) for item in value)
    return value


class StreamingPublicTextSanitizer:
    """Redact identifiers even when a streaming provider splits their tokens."""

    def __init__(self) -> None:
        self._pending = ""

    @staticmethod
    def _private_prefix_length(value: str) -> int:
        lowered = value.lower()
        longest = 0
        for identifier in _PRIVATE_IDENTIFIERS:
            upper_bound = min(len(lowered), len(identifier) - 1)
            for size in range(upper_bound, longest, -1):
                if lowered.endswith(identifier[:size]):
                    longest = size
                    break
        return longest

    def feed(self, value: str) -> str:
        if not value:
            return ""
        self._pending += value
        sanitized = sanitize_public_text(self._pending)
        hold = self._private_prefix_length(sanitized)
        if hold:
            visible = sanitized[:-hold]
            self._pending = sanitized[-hold:]
            return visible
        self._pending = ""
        return sanitized

    def flush(self) -> str:
        visible = sanitize_public_text(self._pending)
        self._pending = ""
        return visible
