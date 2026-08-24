"""
MCP 工具注册表

【Phase 10】MCP 风格 descriptor + 按 step 发现 + 工具目录 catalog API。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPToolDescriptor:
    name: str
    description: str
    server: str
    permissions: list[str] = field(default_factory=list)
    step_types: list[str] = field(default_factory=list)
    transport: str = "langchain-tool"


class MCPRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[MCPToolDescriptor, Any]] = {}

    def register(
        self,
        descriptor: MCPToolDescriptor,
        langchain_tool: Any,
        transport: str | None = None,
    ) -> None:
        if transport:
            descriptor.transport = transport
        self._tools[descriptor.name] = (descriptor, langchain_tool)

    def register_or_update(
        self,
        descriptor: MCPToolDescriptor,
        langchain_tool: Any,
        transport: str | None = None,
    ) -> None:
        self.register(descriptor, langchain_tool, transport=transport)

    def clear(self) -> None:
        self._tools.clear()

    def get_tools_for_step(self, step_type: str) -> list[Any]:
        return [
            tool
            for desc, tool in self._tools.values()
            if step_type in desc.step_types
        ]

    def list_descriptors(self, step_type: str | None = None) -> list[MCPToolDescriptor]:
        if step_type is None:
            return [desc for desc, _ in self._tools.values()]
        return [
            desc
            for desc, _ in self._tools.values()
            if step_type in desc.step_types
        ]

    def build_tool_context(self, step_type: str) -> str:
        descriptors = self.list_descriptors(step_type)
        if not descriptors:
            return ""
        lines = [
            f"- {d.name} ({d.server}, {d.transport}): {d.description} [perms={','.join(d.permissions)}]"
            for d in descriptors
        ]
        return "\n".join(lines)

    def get_descriptor(self, tool_name: str) -> MCPToolDescriptor | None:
        entry = self._tools.get(tool_name)
        return entry[0] if entry else None

    def get_tool(self, tool_name: str) -> Any | None:
        entry = self._tools.get(tool_name)
        return entry[1] if entry else None

    def is_registered_for_step(self, tool_name: str, step_type: str) -> bool:
        desc = self.get_descriptor(tool_name)
        return desc is not None and step_type in desc.step_types

    def to_catalog(self) -> list[dict[str, Any]]:
        """【Phase 10】工具目录，供 API / 运维审计。"""
        return [
            {
                "name": desc.name,
                "server": desc.server,
                "transport": desc.transport,
                "permissions": desc.permissions,
                "step_types": desc.step_types,
                "description": desc.description,
            }
            for desc, _ in self._tools.values()
        ]


mcp_registry = MCPRegistry()
