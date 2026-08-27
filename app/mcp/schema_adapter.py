"""把 MCP inputSchema 变成 LangChain StructuredTool，而不是只认识几个手工签名。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool


def _json_type(name: str) -> type:
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return mapping.get((name or "string").lower(), str)


def args_schema_from_input_schema(tool_name: str, input_schema: Optional[dict[str, Any]]):
    schema = input_schema or {}
    props = dict(schema.get("properties") or {})
    if not props:
        return None
    required = set(schema.get("required") or [])
    try:
        from pydantic import Field, create_model
    except Exception:
        return None
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        typ = _json_type(str(spec.get("type") or "string"))
        default = ... if key in required else spec.get("default", None)
        desc = str(spec.get("description") or "")
        if desc:
            fields[key] = (typ, Field(default, description=desc))
        else:
            fields[key] = (typ, default)
    model_name = "".join(part.title() for part in tool_name.replace("-", "_").split("_")) + "Input"
    return create_model(model_name, **fields)  # type: ignore[arg-type]


def build_structured_tool(
    *,
    name: str,
    description: str,
    invoke: Callable[[dict[str, Any]], Any],
    input_schema: Optional[dict[str, Any]] = None,
) -> StructuredTool:
    schema_model = args_schema_from_input_schema(name, input_schema)

    def func(**kwargs: Any) -> Any:
        return invoke(kwargs)

    func.__name__ = name
    func.__doc__ = description
    if schema_model is not None:
        return StructuredTool.from_function(
            func=func,
            name=name,
            description=description,
            args_schema=schema_model,
        )
    return StructuredTool.from_function(func=func, name=name, description=description)
