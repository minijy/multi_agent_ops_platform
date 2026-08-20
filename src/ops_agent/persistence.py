"""Small durable-write primitives shared by local control-plane registries."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any, *, mode: int | None = None) -> None:
    """Write JSON without ever exposing a partially-written destination file."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if mode is not None:
            os.chmod(temporary, mode)
        temporary.replace(path)
        if mode is not None:
            os.chmod(path, mode)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
