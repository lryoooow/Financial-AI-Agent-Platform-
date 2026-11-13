"""Only the engine may directly depend on the agent frameworks."""

import ast
from pathlib import Path


def test_agent_framework_imports_are_confined_to_engine():
    root = Path(__file__).resolve().parents[1] / "src" / "finresearch"
    violations = []
    for source in root.rglob("*.py"):
        relative = source.relative_to(root)
        if relative.parts[0] == "engine":
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(relative))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                names = [str(node.args[0].value)] if name in {"import_module", "__import__"} else []
            else:
                names = []
            for name in names:
                if name.startswith(("langchain", "langgraph", "deepagents")):
                    violations.append(f"{relative}:{node.lineno} imports {name}")
    assert not violations, "\n".join(violations)
