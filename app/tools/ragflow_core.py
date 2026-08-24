"""
RAGFlow 知识库核心逻辑

供 LangChain @tool 与 MCP Server 共用，避免重复实现。
"""

from __future__ import annotations

import json

from ragflow_sdk import RAGFlow

from app.ragflow.rag_config import _load_ragflow_env

_client: RAGFlow | None = None


def get_ragflow_client() -> RAGFlow:
    global _client
    if _client is None:
        api_key, base_url = _load_ragflow_env()
        _client = RAGFlow(api_key=api_key, base_url=base_url)
    return _client


def get_assistant_list() -> str:
    """查询 RAGFlow 可用聊天助手及其关联知识库。"""
    try:
        chat_list = get_ragflow_client().list_chats()
        if not chat_list:
            return "没有任何可用助手"

        count_chat_info = ""
        for chat in chat_list:
            dataset_names = getattr(chat, "kb_names", []) or []
            count_chat_info += (
                f"助手名称:{chat.name};功能介绍：{chat.description}; "
                f"关联的知识库：{'、'.join(dataset_names)} \n"
            )
        return count_chat_info
    except Exception as e:
        return f"查询助手信息异常，无可用助手,异常信息:{str(e)}"


def create_ask_delete(chat_name: str, question: str) -> str:
    """向指定 RAGFlow 助手创建临时会话、提问并删除会话。"""
    try:
        client = get_ragflow_client()
        chats = client.list_chats(name=chat_name)
        use_chat = chats[0]
        session = use_chat.create_session(name="temp_session_ask")

        response = client.post(
            f"/chats/{use_chat.id}/completions",
            {
                "messages": [{"role": "user", "content": question}],
                "stream": True,
                "session_id": session.id,
            },
            stream=True,
        )
        result = ""
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.removeprefix("data:").strip()
            if line == "[DONE]":
                break
            data = json.loads(line)
            chunk_data = data.get("data")
            if not isinstance(chunk_data, dict):
                continue
            answer = chunk_data.get("answer")
            if answer:
                if answer.startswith(result):
                    result = answer
                elif not result.startswith(answer):
                    result += answer

        use_chat.delete_sessions(ids=[session.id])
        return result
    except Exception as e:
        return f"提问失败，错误原因：{str(e)}"
