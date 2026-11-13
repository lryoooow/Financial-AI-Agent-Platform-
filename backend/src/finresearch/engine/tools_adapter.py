"""Compile framework-independent tool registrations for LangChain."""

from typing import Any

from langchain_core.tools import StructuredTool, tool

from ..tools.registry import RegisteredTool
from .turn_context import current_turn_state


def compile_tool(registration: RegisteredTool) -> StructuredTool:
    async def execute(**arguments: Any) -> tuple[str, dict | None]:
        turn = current_turn_state()
        if turn is not None and registration.name == "search_knowledge":
            key = (str(arguments.get("query") or ""), str(arguments.get("ticker") or ""), arguments.get("limit", 5))
            result = turn.retrieval_cache.get(key)
            if result is None:
                result = await registration.run(arguments)
                turn.retrieval_cache[key] = result
        else:
            result = await registration.run(arguments)
        artifact = result.artifact
        if result.error_code:
            artifact = {**(artifact or {}), "error_code": result.error_code}
        return result.content, artifact

    return StructuredTool.from_function(
        coroutine=execute,
        name=registration.name,
        description=registration.description,
        args_schema=registration.args_schema,
        response_format="content_and_artifact",
    )


def compile_tools(registrations: list[RegisteredTool]) -> list[StructuredTool]:
    return [compile_tool(registration) for registration in sorted(registrations, key=lambda item: item.name)]
