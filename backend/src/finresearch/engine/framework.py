"""Small adapter functions for framework-specific message and event APIs."""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer


def push_custom_event(payload: Any) -> None:
    get_stream_writer()(payload)


def vision_message(image: str, instruction: str = ".") -> HumanMessage:
    return HumanMessage(content=[
        {"type": "text", "text": instruction},
        {"type": "image_url", "image_url": {"url": image}},
    ])
