import re
from dataclasses import dataclass
from typing import Literal, Optional

# Matches our own output filenames so the webhook doesn't reprocess them.
#   New format (v2):    <originalStem>_<attachmentId>_preview.pdf
#   Legacy format (v1): preview_<attachmentId>.pdf
# Attachment IDs are uppercase alphanumeric in practice; IGNORECASE for robustness.
PREVIEW_FILENAME_RE = re.compile(
    r"(.+_[A-Z0-9]+_preview\.pdf|^preview_[A-Z0-9]+\.pdf)$",
    re.IGNORECASE,
)

CONVERTIBLE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
ALREADY_PREVIEWABLE_EXTS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".heic",
    ".svg",
    ".tiff",
    ".psd",
}

MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MiB


@dataclass(frozen=True)
class ClassifyResult:
    action: Literal["convert", "skip"]
    reason: Optional[str]


def classify_attachment(attachment: dict) -> ClassifyResult:
    """Classify a Wrike attachment.

    Order of checks matters: scope filter, then self-output guard, then size,
    then extension.
    """
    name: str = attachment["name"]
    scope: str = attachment.get("scope", "task")
    size: int = attachment.get("size") or 0

    if scope != "task":
        return ClassifyResult(action="skip", reason="wrong_scope")

    if PREVIEW_FILENAME_RE.match(name):
        return ClassifyResult(action="skip", reason="filename_is_preview")

    if size > MAX_SIZE_BYTES:
        return ClassifyResult(action="skip", reason="too_large")

    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ALREADY_PREVIEWABLE_EXTS:
        return ClassifyResult(action="skip", reason="already_previewable")
    if ext in CONVERTIBLE_EXTS:
        return ClassifyResult(action="convert", reason=None)
    return ClassifyResult(action="skip", reason="wrong_type")
