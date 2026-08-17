from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def _format_params(request_params: dict[str, Any] | None) -> str:
    if not request_params:
        return ""
    canonical: list[str] = []
    for key in sorted(request_params):
        value = request_params[key]
        if value == "":
            continue
        if isinstance(value, (dict, list)):
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            canonical.append(f"{key}={encoded}")
        else:
            canonical.append(f"{key}={value}")
    return "&".join(canonical)


def _md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def _aes_ecb_encrypt(key: str, data: str) -> str:
    key_bytes = key.encode("utf-8")
    if len(key_bytes) not in {16, 24, 32}:
        pad_len = 16 - (len(key_bytes) % 16)
        key_bytes = key_bytes + (b"\0" * pad_len)
    padder = PKCS7(128).padder()
    padded = padder.update(data.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("utf-8")


def generate_sign(app_id: str, request_params: dict[str, Any]) -> str:
    canonical = _format_params(request_params)
    md5_str = _md5_upper(canonical)
    return _aes_ecb_encrypt(app_id, md5_str)
