"""对话外部记忆：媒体卸载、老消息原文归档与按需片段检索。

固定管线没有通用 read_file 工具，因此取回由后端在装配上下文前按当前问题做有界检索；
只有命中的少量片段重新进入模型上下文，完整原文始终留在磁盘。
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from pathlib import Path
from typing import Optional

from ..config import app_data_dir

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_DATA_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", re.DOTALL)
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def _safe(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value or ""):
        raise ValueError(f"invalid {label}")
    return value


def conversation_dir(conversation_id: str, root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else Path(app_data_dir()) / "context"
    path = base / "conversations" / _safe(conversation_id, "conversation_id")
    path.mkdir(parents=True, exist_ok=True)
    return path


def offload_message_media(message: dict, conversation_id: str, root: Optional[Path] = None) -> dict:
    """把 data URL 图片按内容哈希落盘，存储消息只留下同源 URL。"""
    stored = dict(message)
    images = list(stored.get("images") or [])
    if not images:
        return stored
    media_dir = conversation_dir(conversation_id, root) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    refs: list[str] = []
    for image in images:
        if not isinstance(image, str):
            continue
        match = _DATA_URL.match(image)
        if not match:
            refs.append(image)
            continue
        mime, payload = match.groups()
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        digest = hashlib.sha256(raw).hexdigest()[:20]
        filename = digest + _EXT.get(mime.lower(), ".bin")
        target = media_dir / filename
        if not target.exists():
            target.write_bytes(raw)
        refs.append(f"/api/context/media/{conversation_id}/{filename}")
    stored["images"] = refs
    return stored


def media_path(conversation_id: str, filename: str, root: Optional[Path] = None) -> Path:
    _safe(filename.replace(".", "_"), "filename")
    target = conversation_dir(conversation_id, root) / "media" / filename
    resolved = target.resolve()
    media_root = (conversation_dir(conversation_id, root) / "media").resolve()
    if media_root not in resolved.parents:
        raise ValueError("invalid media path")
    return resolved


def delete_conversation_data(conversation_id: str, root: Optional[Path] = None) -> None:
    """删除会话时同步清理其外部记忆目录；安全边界仍由 conversation_dir 白名单保证。"""
    target = conversation_dir(conversation_id, root)
    if target.exists():
        shutil.rmtree(target)






