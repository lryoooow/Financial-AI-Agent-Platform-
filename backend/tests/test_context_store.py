import base64

from finresearch.tools.context_store import media_path, offload_message_media


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






