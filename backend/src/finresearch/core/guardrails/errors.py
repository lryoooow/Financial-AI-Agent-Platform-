"""Public diagnostics for failures whose original payload may contain credentials."""

from __future__ import annotations

from openai import APIConnectionError, APITimeoutError


class ModelServiceError(RuntimeError):
    def __init__(self, error: Exception) -> None:
        status = _http_status(error)
        self.vision_unsupported = status in (None, 400, 415, 422) and vision_error(error)
        super().__init__(public_failure(error))


def _http_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None) or getattr(error, "code", None)
    return status if isinstance(status, int) and 400 <= status < 600 else None


def public_failure(error: BaseException) -> str:
    """Return application-owned text, independent of exception messages and HTTP bodies."""
    if isinstance(error, ModelServiceError):
        return str(error)
    if isinstance(error, (TimeoutError, APITimeoutError)):
        return "请求超时"
    status = _http_status(error)
    if status:
        detail = {
            400: "请求参数被服务拒绝", 401: "认证失败，请检查 API Key",
            403: "服务拒绝访问，请检查权限", 404: "服务或模型不存在",
            408: "请求超时", 413: "请求内容超限", 429: "服务限流，请稍后重试",
        }.get(status, "服务暂不可用" if status >= 500 else "请求失败")
        return f"{detail}（HTTP {status}）"
    if isinstance(error, (APIConnectionError, ConnectionError)):
        return "无法连接服务，请检查网络和服务地址"
    return "处理失败，请重试"


def vision_error(error: Exception) -> bool:
    if isinstance(error, ModelServiceError):
        return error.vision_unsupported
    msg = str(error).lower()
    if any(word in msg for word in ("too large", "too long", "too big", "exceed", "rate limit", "quota")):
        return False
    if any(word in msg for word in ("image_url", "input_image", "multimodal", "vision", "图片", "图像", "视觉", "多模态")):
        return True
    return "image" in msg and any(
        word in msg for word in ("not support", "unsupported", "invalid", "unknown", "expected string")
    )
