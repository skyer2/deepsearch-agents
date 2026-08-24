"""
RAGFlow 知识库工具模块

封装 RAGFlow 子智能体使用的 LangChain 工具。
底层逻辑与 MCP Server 共用 app.tools.ragflow_core。
"""

from langchain_core.tools import tool

from app.api.monitor import monitor
from app.tools import ragflow_core


@tool
def get_assistant_list() -> str:
    """
    查询 RAGFlow 中有哪些聊天助手，以及每个助手关联了哪些知识库

    调用 create_ask_delete 之前，应先调用本工具确认助手名称。
    :return: 助手名称、功能介绍、关联知识库；无助手或异常时返回中文提示
    """
    monitor.report_tool(
        tool_name="ragflow聊天助手列表查询工具：get_assistant_list",
        args={"transport": "langchain-tool"},
    )
    return ragflow_core.get_assistant_list()


@tool
def create_ask_delete(chat_name, question) -> str:
    """
    向某个 RAGFlow 聊天助手创建临时会话并完成一次提问

    注意：调用此工具之前，必须先调用 get_assistant_list。
    :param chat_name: 助手名称，必须来自 get_assistant_list 返回结果
    :param question: 本次提问的问题
    :return: RAGFlow 返回的回答文本；异常时返回中文错误提示
    """
    monitor.report_tool(
        tool_name="ragflow提问助手工具：create_ask_delete",
        args={
            "chat_name": chat_name,
            "question": question,
            "transport": "langchain-tool",
        },
    )
    return ragflow_core.create_ask_delete(chat_name, question)
