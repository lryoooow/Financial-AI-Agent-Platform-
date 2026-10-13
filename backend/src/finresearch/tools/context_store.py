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
_ASSET_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_IMAGES_PER_MESSAGE = 4

# 图片降采样阈值：多模态消息以 base64 进模型上下文且驻留检查点、每轮重发，
# 5MB 原图 ≈ 700 万字符 base64，一条消息即可吃光上下文预算。前端 ChatDesk 会压一道，
# 但 MarketChatPanel 与直接 API 调用直传原图——服务端必须自己设防。
_DOWNSAMPLE_MAX_EDGE = 1280
_DOWNSAMPLE_MIN_BYTES = 300 * 1024


def _downsample_image(raw: bytes, mime: str) -> tuple[bytes, str]:
    """长边 >1280px 或 >300KB 的图片降采样重编码；失败/变大时原样返回。"""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as im:
            width, height = im.size
            needs_resize = max(width, height) > _DOWNSAMPLE_MAX_EDGE
            if not needs_resize and len(raw) <= _DOWNSAMPLE_MIN_BYTES:
                return raw, mime
            if needs_resize:
                ratio = _DOWNSAMPLE_MAX_EDGE / max(width, height)
                im = im.resize(
                    (max(1, round(width * ratio)), max(1, round(height * ratio))),
                    Image.LANCZOS,
                )
            buf = BytesIO()
            if mime == "image/jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            elif mime == "image/webp":
                im.save(buf, format="WEBP", quality=85)
            else:  # PNG：保留透明通道，无损重编码
                im.save(buf, format="PNG", optimize=True)
            out = buf.getvalue()
            if 0 < len(out) < len(raw):
                return out, mime
            return raw, mime
    except Exception:
        return raw, mime


def _safe(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value or ""):
        raise ValueError(f"invalid {label}")
    return value


def conversation_dir(conversation_id: str, root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else Path(app_data_dir()) / "context"
    path = base / "conversations" / _safe(conversation_id, "conversation_id")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_image_mime(raw: bytes) -> Optional[str]:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_asset_dir(root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else Path(app_data_dir())
    target = base / "assets" / "images"
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_image_asset(raw: bytes, content_type: str, root: Optional[Path] = None) -> dict:
    """校验并保存图片资产；公共契约只返回 asset_id，不暴露真实路径。"""
    requested_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if requested_mime not in _ASSET_MIME:
        raise ValueError("仅支持 JPEG、PNG、WebP 图片")
    if not raw:
        raise ValueError("图片内容为空")
    detected_mime = _detect_image_mime(raw)
    if detected_mime != requested_mime:
        raise ValueError("图片格式与 Content-Type 不一致")
    # 先降采样再做大小校验与内容哈希：手机原图常见 6-10MB，拒绝不如收敛到 ≤1280px；
    # HTTP 层另有 5MB content-length 上限防滥用，这里保证的是"落盘/进上下文的字节"受控
    raw, detected_mime = _downsample_image(raw, detected_mime)
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 5MB")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    asset_id = f"img_{digest}"
    target = image_asset_dir(root) / f"{asset_id}{_ASSET_MIME[detected_mime]}"
    if not target.exists():
        target.write_bytes(raw)
    return {
        "asset_id": asset_id,
        "url": f"/api/assets/images/{asset_id}",
        "mime_type": detected_mime,
        "size": len(raw),
    }


def image_asset_path(asset_id: str, root: Optional[Path] = None) -> tuple[Path, str]:
    _safe(asset_id, "asset_id")
    if not asset_id.startswith("img_"):
        raise ValueError("invalid asset_id")
    asset_root = image_asset_dir(root).resolve()
    for mime, extension in _ASSET_MIME.items():
        candidate = (asset_root / f"{asset_id}{extension}").resolve()
        if asset_root not in candidate.parents:
            raise ValueError("invalid asset path")
        if candidate.is_file():
            return candidate, mime
    raise FileNotFoundError(asset_id)


def image_asset_urls(asset_ids: list[str], root: Optional[Path] = None) -> list[str]:
    if len(asset_ids) > _MAX_IMAGES_PER_MESSAGE:
        raise ValueError("每条消息最多上传 4 张图片")
    urls: list[str] = []
    for asset_id in asset_ids:
        image_asset_path(asset_id, root)
        urls.append(f"/api/assets/images/{asset_id}")
    return urls


def image_asset_data_urls(asset_ids: list[str], root: Optional[Path] = None) -> list[str]:
    if len(asset_ids) > _MAX_IMAGES_PER_MESSAGE:
        raise ValueError("每条消息最多上传 4 张图片")
    data_urls: list[str] = []
    for asset_id in asset_ids:
        path, mime = image_asset_path(asset_id, root)
        raw = path.read_bytes()
        data_urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}")
    return data_urls


def validate_legacy_images(images: list[str]) -> None:
    """旧版 data URL 只读兼容；新调用必须使用 image_asset_ids。"""
    if len(images) > _MAX_IMAGES_PER_MESSAGE:
        raise ValueError("每条消息最多上传 4 张图片")
    for image in images:
        match = _DATA_URL.match(image or "")
        if not match or match.group(1).lower() not in _ASSET_MIME:
            raise ValueError("仅支持 JPEG、PNG、WebP 图片")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise ValueError("图片编码无效") from exc
        if not raw or len(raw) > _MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能超过 5MB")
        if _detect_image_mime(raw) != match.group(1).lower():
            raise ValueError("图片内容格式无效")


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






