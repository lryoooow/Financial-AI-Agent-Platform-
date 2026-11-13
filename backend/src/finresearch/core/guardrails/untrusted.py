"""Fence content retrieved from sources outside the current conversation."""

import html
import re

_INJECTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|system\s*prompt|developer\s+message|remember_user_preference"
    r"|(?:忽略|覆盖|无视)(?:之前|以上|前面|此前|所有).{0,16}(?:要求|指令|规则)"
    r"|(?:泄露|输出|打印|复述).{0,12}(?:系统提示词|系统提示|API\s*Key))",
    re.IGNORECASE,
)


def looks_like_instruction(text: str) -> bool:
    return bool(_INJECTION.search(text or ""))


def fence_external(text: str, source: str, *, limit: int) -> str:
    label = html.escape(source[:120], quote=True)
    safe = html.escape(text[:limit], quote=False)
    return f'<untrusted_external_data source="{label}">{safe}</untrusted_external_data>'
