"""
【Phase 16】Files MCP Server — Resources（会话文件只读）+ 异步 PDF 任务。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.tools.upload_file_read_tool import read_file_content as read_upload_file
from app.tools.markdown_tools import generate_markdown as gen_md
from app.utils.path_utils import resolve_path

mcp = FastMCP("files-mcp")

SESSIONS_ROOT = Path(__file__).resolve().parents[2] / "data" / "sessions"


def _safe_session_file_uri(uri: str) -> tuple[Path, str]:
    """解析 session://{session_id}/{filename}"""
    if not uri.startswith("session://"):
        raise ValueError("unsupported resource uri")
    rest = uri.removeprefix("session://")
    session_id, _, filename = rest.partition("/")
    if not session_id or not filename:
        raise ValueError("invalid session resource uri")
    base = (SESSIONS_ROOT / session_id).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("path traversal denied")
    return target, session_id


@mcp.resource("session://{session_id}/{filename}")
def session_file_resource(session_id: str, filename: str) -> str:
    """读取会话目录下的文件内容（Markdown/文本）。"""
    uri = f"session://{session_id}/{filename}"
    path, _ = _safe_session_file_uri(uri)
    if not path.exists():
        return f"文件不存在: {filename}"
    if path.suffix.lower() in {".md", ".txt", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")
    return f"二进制或不支持直接读取的类型: {path.name}"


@mcp.tool()
def read_file_content(filename: str) -> str:
    """读取当前会话上传/生成文件（与 LangChain 工具同 core）。"""
    return read_upload_file.invoke({"filename": filename})


@mcp.tool()
def generate_markdown(content: str, filename: str = "report.md") -> str:
    """生成 Markdown 报告文件。"""
    return gen_md.invoke({"content": content, "filename": filename})


@mcp.tool()
def convert_md_to_pdf_async(md_filename: str, pdf_filename: Optional[str] = None) -> str:
    """
    异步提交 Markdown→PDF 转换任务，返回 task_id（Harness 可轮询）。
    """
    from app.mcp.mcp_tasks import get_mcp_task_manager
    from app.tools.pdf_tools import convert_md_to_pdf

    manager = get_mcp_task_manager()

    def _run() -> str:
        return convert_md_to_pdf.invoke(
            {"md_filename": md_filename, "pdf_filename": pdf_filename}
        )

    task_id = manager.submit(
        server_module="app.mcp.servers.files_server",
        tool_name="convert_md_to_pdf_async",
        runner=_run,
    )
    return json.dumps({"task_id": task_id, "status": "pending"}, ensure_ascii=False)


if __name__ == "__main__":
    print("[FilesMCP] starting stdio server", file=sys.stderr)
    mcp.run(transport="stdio")
