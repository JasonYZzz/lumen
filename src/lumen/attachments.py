"""Canonical user attachments backed by the content-addressed ArtifactStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumen.context.artifacts import ArtifactStore, ArtifactStoreError

MAX_ATTACHMENTS_PER_INPUT = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_MARKER_PREFIX = "lumen-attachment:"


class AttachmentError(ValueError):
    """Raised when an attachment is invalid, unsupported, or unavailable."""


class AttachmentRef(BaseModel):
    """Provider-neutral reference to one immutable user attachment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal["image"] = "image"
    media_type: str
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(ge=1, le=MAX_IMAGE_BYTES)

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in SUPPORTED_IMAGE_MEDIA_TYPES:
            supported = ", ".join(sorted(SUPPORTED_IMAGE_MEDIA_TYPES))
            raise ValueError(f"unsupported image media type {value!r}; supported: {supported}")
        return normalized

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        name = Path(value).name
        if name in {"", ".", ".."}:
            raise ValueError("attachment filename is invalid")
        return name


class AttachmentStore:
    """Validate image uploads and store only their immutable bytes."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def store_image(self, *, filename: str, media_type: str, content: bytes) -> AttachmentRef:
        if not content:
            raise AttachmentError("image attachment is empty")
        if len(content) > MAX_IMAGE_BYTES:
            raise AttachmentError(f"image attachment exceeds {MAX_IMAGE_BYTES} bytes")
        try:
            candidate = AttachmentRef(
                artifact_ref="sha256:" + "0" * 64,
                media_type=media_type,
                filename=filename,
                byte_size=len(content),
            )
        except ValueError as error:
            raise AttachmentError(str(error)) from error
        _validate_image_signature(candidate.media_type, content)
        return candidate.model_copy(update={"artifact_ref": self.artifacts.store(content)})

    def read(self, attachment: AttachmentRef) -> bytes:
        try:
            content = self.artifacts.read(attachment.artifact_ref)
        except ArtifactStoreError as error:
            raise AttachmentError(str(error)) from error
        if content is None:
            raise AttachmentError(f"attachment artifact is missing: {attachment.artifact_ref}")
        if len(content) != attachment.byte_size:
            raise AttachmentError(f"attachment size mismatch: {attachment.artifact_ref}")
        _validate_image_signature(attachment.media_type, content)
        return content


def attachment_marker(attachment: AttachmentRef) -> str:
    """Encode a bounded reference marker suitable for canonical model history."""

    return _MARKER_PREFIX + json.dumps(
        attachment.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def attachment_from_marker(value: str) -> AttachmentRef | None:
    """Decode an exact Lumen marker; ordinary user text is never interpreted."""

    if not value.startswith(_MARKER_PREFIX):
        return None
    try:
        raw = json.loads(value.removeprefix(_MARKER_PREFIX))
        return AttachmentRef.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _validate_image_signature(media_type: str, content: bytes) -> None:
    valid = (
        (media_type == "image/png"
        and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (media_type == "image/jpeg"
        and content.startswith(b"\xff\xd8\xff"))
        or (media_type == "image/gif"
        and content.startswith((b"GIF87a", b"GIF89a")))
        or (media_type == "image/webp"
        and len(content) >= 12
        and content[:4] == b"RIFF"
        and content[8:12] == b"WEBP")
    )
    if not valid:
        raise AttachmentError(f"content does not match declared media type {media_type!r}")


__all__ = [
    "MAX_ATTACHMENTS_PER_INPUT",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "AttachmentError",
    "AttachmentRef",
    "AttachmentStore",
    "attachment_from_marker",
    "attachment_marker",
]
