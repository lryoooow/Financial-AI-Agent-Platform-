import base64

import pytest

from finresearch.tools.context_store import (
    image_asset_data_urls,
    image_asset_path,
    media_path,
    offload_message_media,
    save_image_asset,
    validate_legacy_images,
)


def test_media_is_offloaded_by_hash_and_message_keeps_reference(tmp_path):
    raw = b"fake-png-content"
    message = {
        "role": "user",
        "content": "请看图",
        "images": ["data:image/png;base64," + base64.b64encode(raw).decode()],
    }
    stored = offload_message_media(message, "conv_1", tmp_path)
    assert stored["images"][0].startswith("/api/context/media/conv_1/")
    assert "base64" not in stored["images"][0]
    filename = stored["images"][0].rsplit("/", 1)[-1]
    assert media_path("conv_1", filename, tmp_path).read_bytes() == raw
    assert message["images"][0].startswith("data:"), "不得原地改写当前视觉请求"


def test_image_asset_roundtrip_uses_asset_id(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"content"

    meta = save_image_asset(raw, "image/png", tmp_path)

    assert meta["asset_id"].startswith("img_")
    assert "path" not in meta
    path, mime = image_asset_path(meta["asset_id"], tmp_path)
    assert path.read_bytes() == raw
    assert mime == "image/png"
    assert image_asset_data_urls([meta["asset_id"]], tmp_path)[0].startswith("data:image/png;base64,")


def test_image_asset_rejects_mime_spoof(tmp_path):
    with pytest.raises(ValueError, match="格式"):
        save_image_asset(b"not-a-png", "image/png", tmp_path)


def test_legacy_images_enforce_count_and_type():
    with pytest.raises(ValueError, match="最多"):
        validate_legacy_images(["data:image/png;base64,AA=="] * 5)
    with pytest.raises(ValueError, match="仅支持"):
        validate_legacy_images(["data:image/gif;base64,R0lGODlh"])








def test_save_image_asset_downsamples_large_uploads(tmp_path):
    """大图必须服务端降采样：MarketChatPanel 与直接 API 不走前端压缩，5MB 原图
    base64 后 ≈700 万字符进上下文并驻留检查点每轮重发。"""
    import io

    from PIL import Image

    from finresearch.tools.context_store import save_image_asset

    buf = io.BytesIO()
    # 噪声图：纯色 JPEG 压缩率太高，构造不出真实大图
    Image.effect_noise((3000, 2000), 96).convert("RGB").save(buf, format="JPEG", quality=95)
    big = buf.getvalue()
    assert len(big) > 300 * 1024  # 确认 fixture 确实是大图

    result = save_image_asset(big, "image/jpeg", root=tmp_path)
    assert result["size"] < len(big)
    with Image.open(tmp_path / "assets" / "images" / f"{result['asset_id']}.jpg") as im:
        assert max(im.size) <= 1280  # 长边钳制


def test_save_image_asset_keeps_small_uploads(tmp_path):
    """小图（≤1280px 且 ≤300KB）原样存储，不做有损重编码。"""
    import io

    from PIL import Image

    from finresearch.tools.context_store import save_image_asset

    buf = io.BytesIO()
    Image.new("RGB", (200, 100), color=(1, 2, 3)).save(buf, format="JPEG", quality=90)
    small = buf.getvalue()

    result = save_image_asset(small, "image/jpeg", root=tmp_path)
    assert result["size"] == len(small)
