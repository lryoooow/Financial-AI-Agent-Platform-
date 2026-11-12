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


MAX_IMAGE_COUNT = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _detect_image_ext(raw: bytes) -> Optional[str]:
    """按文件魔数校验真实图片格式，拒绝伪造 MIME 的 SVG/HTML/超大负载。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


def _is_active_markup(raw: bytes) -> bool:
    head = raw.lstrip()[:128].lower()
    return head.startswith((b"<svg", b"<!doctype", b"<html", b"<script", b"<?xml")) or b"<script" in head


def validate_images_input(images: Optional[list[str]]) -> list[str]:
    """校验多模态输入图片数量、单张体积与魔数（供路由前置拦截，违规抛 ValueError）。"""
    if not images:
        return []
    if len(images) > MAX_IMAGE_COUNT:
        raise ValueError(f"单次最多支持上传 {MAX_IMAGE_COUNT} 张图片")
    cleaned: list[str] = []
    for idx, image in enumerate(images, 1):
        if not isinstance(image, str) or not image.strip():
            continue
        if image.startswith("/api/context/media/"):
            cleaned.append(image)
            continue
        match = _DATA_URL.match(image)
        if not match:
            raise ValueError(f"第 {idx} 张图片格式无效（需为 PNG/JPEG/WEBP/GIF）")
        mime, payload = match.groups()
        if mime.lower() not in _EXT:
            raise ValueError(f"第 {idx} 张图片类型不支持：{mime}")
        # base64 长度约 4/3 原字节，先粗筛防内存膨胀
        if len(payload) > (MAX_IMAGE_BYTES * 4 // 3) + 1024:
            raise ValueError(f"第 {idx} 张图片超过 8MB 大小上限")
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise ValueError(f"第 {idx} 张图片编码损坏") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"第 {idx} 张图片超过 8MB 大小上限")
        if _detect_image_ext(raw) is None:
            raise ValueError(f"第 {idx} 张图片文件内容不是有效的 PNG/JPEG/WEBP/GIF")
        cleaned.append(image)
    return cleaned


def conversation_dir(conversation_id: str, root: Optional[Path] = None, *, create: bool = True) -> Path:
    base = Path(root) if root is not None else Path(app_data_dir()) / "context"
    path = base / "conversations" / _safe(conversation_id, "conversation_id")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def offload_message_media(message: dict, conversation_id: str, root: Optional[Path] = None) -> dict:
    """把 data URL 图片按内容哈希落盘，存储消息只留下同源 URL。"""
    stored = dict(message)
    images = list(stored.get("images") or [])
    if not images:
        return stored
    media_dir = conversation_dir(conversation_id, root, create=True) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    refs: list[str] = []
    for image in images[:MAX_IMAGE_COUNT]:
        if not isinstance(image, str):
            continue
        match = _DATA_URL.match(image)
        if not match:
            if image.startswith("/api/context/media/"):
                refs.append(image)
            continue
        mime, payload = match.groups()
        if len(payload) > (MAX_IMAGE_BYTES * 4 // 3) + 1024:
            continue
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if len(raw) > MAX_IMAGE_BYTES or _is_active_markup(raw):
            continue
        ext = _detect_image_ext(raw) or _EXT.get(mime.lower())
        if ext is None:
            continue
        digest = hashlib.sha256(raw).hexdigest()[:20]
        filename = digest + ext
        target = media_dir / filename
        if not target.exists():
            target.write_bytes(raw)
        refs.append(f"/api/context/media/{conversation_id}/{filename}")
    stored["images"] = refs
    return stored


def media_path(conversation_id: str, filename: str, root: Optional[Path] = None) -> Path:
    _safe(filename.replace(".", "_"), "filename")
    conv_root = conversation_dir(conversation_id, root, create=False)
    media_root = (conv_root / "media").resolve()
    target = (conv_root / "media" / filename).resolve()
    if media_root not in target.parents:
        raise ValueError("invalid media path")
    return target


def delete_conversation_data(conversation_id: str, root: Optional[Path] = None) -> None:
    """删除会话时同步清理其外部记忆目录；安全边界仍由 conversation_dir 白名单保证。"""
    target = conversation_dir(conversation_id, root, create=False)
    if target.exists():
        shutil.rmtree(target)






