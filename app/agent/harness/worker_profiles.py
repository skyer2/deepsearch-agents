"""
Worker Profiles — 稳定、最小的 model-visible tool surface。

不按 Task 动态增删 37 个 schema（破坏 KV cache），而是有限几个固定 Profile：
Web / DB / KB / File / Mixed。Harness Policy 仍 fail-closed。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

WEB_TOOLS = ("internet_search",)
DB_TOOLS = ("list_sql_tables", "get_table_data", "execute_sql_query")
KB_TOOLS = ("get_assistant_list", "create_ask_delete")
FILE_READ_TOOLS = ("read_file_content",)
CONTEXT_TOOLS = ("read_artifact", "read_evidence")
SYNTHESIS_EXTRA = ("generate_markdown",)

PROFILE_WEB = "web_researcher"
PROFILE_DB = "db_researcher"
PROFILE_KB = "kb_researcher"
PROFILE_FILE = "file_researcher"
PROFILE_MIXED = "mixed_researcher"
PROFILE_SYNTHESIS = "synthesis_editor"


@dataclass(frozen=True)
class WorkerProfile:
    name: str
    tools: tuple[str, ...]
    step_types: tuple[str, ...]


PROFILES: dict[str, WorkerProfile] = {
    PROFILE_WEB: WorkerProfile(
        name=PROFILE_WEB,
        tools=WEB_TOOLS + CONTEXT_TOOLS,
        step_types=("network_search", "research"),
    ),
    PROFILE_DB: WorkerProfile(
        name=PROFILE_DB,
        tools=DB_TOOLS + CONTEXT_TOOLS,
        step_types=("database_query", "research"),
    ),
    PROFILE_KB: WorkerProfile(
        name=PROFILE_KB,
        tools=KB_TOOLS + CONTEXT_TOOLS,
        step_types=("knowledge_base", "research"),
    ),
    PROFILE_FILE: WorkerProfile(
        name=PROFILE_FILE,
        tools=FILE_READ_TOOLS + CONTEXT_TOOLS,
        step_types=("file_read", "research"),
    ),
    PROFILE_MIXED: WorkerProfile(
        name=PROFILE_MIXED,
        tools=WEB_TOOLS + DB_TOOLS + KB_TOOLS + FILE_READ_TOOLS + CONTEXT_TOOLS,
        step_types=("research",),
    ),
    PROFILE_SYNTHESIS: WorkerProfile(
        name=PROFILE_SYNTHESIS,
        tools=("generate_markdown", "read_file_content") + CONTEXT_TOOLS,
        step_types=("generate_markdown", "summarize", "convert_pdf"),
    ),
}


def _has_any(tools: set[str], group: Iterable[str]) -> bool:
    return any(name in tools for name in group)


def resolve_worker_profile(
    step_type: str,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    kind = (step_type or "").strip().lower()
    if kind == "network_search":
        return PROFILE_WEB
    if kind == "database_query":
        return PROFILE_DB
    if kind == "knowledge_base":
        return PROFILE_KB
    if kind == "file_read":
        return PROFILE_FILE
    if kind in {"generate_markdown", "summarize", "convert_pdf"}:
        return PROFILE_SYNTHESIS
    tools = {str(t) for t in (allowed_tools or []) if t}
    if not tools:
        return PROFILE_MIXED if kind == "research" else PROFILE_MIXED
    flags = [
        _has_any(tools, WEB_TOOLS),
        _has_any(tools, DB_TOOLS),
        _has_any(tools, KB_TOOLS),
        _has_any(tools, FILE_READ_TOOLS),
    ]
    if sum(1 for x in flags if x) <= 1:
        if flags[0]:
            return PROFILE_WEB
        if flags[1]:
            return PROFILE_DB
        if flags[2]:
            return PROFILE_KB
        if flags[3]:
            return PROFILE_FILE
    return PROFILE_MIXED


def tools_for_profile(profile: str) -> list[str]:
    spec = PROFILES.get(profile)
    if spec is None:
        spec = PROFILES[PROFILE_MIXED]
    return list(spec.tools)


def filter_tools_for_profile(tools: list[Any], profile: str) -> list[Any]:
    allowed = set(tools_for_profile(profile))
    filtered = [tool for tool in tools if getattr(tool, "name", "") in allowed]
    return filtered or list(tools)
