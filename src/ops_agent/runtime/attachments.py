from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .domain import AttachmentReference, AttachmentUploadRequest


class AttachmentError(ValueError):
    pass


_FORMAT_TO_MEDIA_TYPE = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class LocalAttachmentStore:
    """Tenant-scoped, content-addressed image storage."""

    def __init__(
        self,
        root: Path,
        *,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_image_pixels: int = 40_000_000,
    ) -> None:
        self.root = root
        self.max_image_bytes = max_image_bytes
        self.max_image_pixels = max_image_pixels
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _tenant_key(tenant_id: str) -> str:
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _safe_name(name: str | None) -> str | None:
        if not name:
            return None
        cleaned = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name).strip()
        return cleaned[:255] or None

    def _paths(self, tenant_id: str, digest: str) -> tuple[Path, Path]:
        base = self.root / self._tenant_key(tenant_id)
        return (
            base / "objects" / digest[:2] / digest,
            base / "metadata" / f"{digest}.json",
        )

    def save(
        self, payload: AttachmentUploadRequest, *, tenant_id: str
    ) -> AttachmentReference:
        try:
            raw = base64.b64decode(payload.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentError("INVALID_IMAGE_BASE64") from exc
        if not raw:
            raise AttachmentError("INVALID_IMAGE")
        if len(raw) > self.max_image_bytes:
            raise AttachmentError("IMAGE_TOO_LARGE")

        try:
            with Image.open(BytesIO(raw)) as image:
                image.load()
                detected = _FORMAT_TO_MEDIA_TYPE.get(image.format or "")
                width, height = image.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AttachmentError("INVALID_IMAGE") from exc
        if detected != payload.media_type:
            raise AttachmentError("IMAGE_TYPE_MISMATCH")
        if width * height > self.max_image_pixels:
            raise AttachmentError("IMAGE_TOO_MANY_PIXELS")

        digest = hashlib.sha256(raw).hexdigest()
        object_path, metadata_path = self._paths(tenant_id, digest)
        reference = AttachmentReference(
            attachment_id=f"sha256:{digest}",
            media_type=payload.media_type,
            bytes=len(raw),
            width=width,
            height=height,
            name=self._safe_name(payload.name),
        )
        object_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            temporary = object_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_bytes(raw)
            temporary.replace(object_path)
        metadata_path.write_text(
            json.dumps(reference.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        return reference

    def get(
        self, attachment_id: str, *, tenant_id: str
    ) -> tuple[AttachmentReference, bytes]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", attachment_id):
            raise AttachmentError("INVALID_ATTACHMENT_ID")
        digest = attachment_id.removeprefix("sha256:")
        object_path, metadata_path = self._paths(tenant_id, digest)
        if not object_path.is_file() or not metadata_path.is_file():
            raise AttachmentError("ATTACHMENT_NOT_FOUND")
        reference = AttachmentReference.model_validate_json(
            metadata_path.read_text(encoding="utf-8")
        )
        raw = object_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest or len(raw) != reference.bytes:
            raise AttachmentError("ATTACHMENT_CORRUPT")
        return reference, raw

    def data_url(self, attachment_id: str, *, tenant_id: str) -> str:
        reference, raw = self.get(attachment_id, tenant_id=tenant_id)
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{reference.media_type};base64,{encoded}"
