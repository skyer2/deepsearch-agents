"""
RAGFlow 知识库子智能体配置模块

将 app/prompt/prompts.yml 中的 ragflow 配置与 RAGFlow 工具组装成
DeepAgents 可识别的字典式子智能体。
"""

from app.agent.prompts import sub_agents_content
from app.mcp.client import get_ragflow_tools


def build_knowledge_base_agent() -> dict:
    return {
        "name": sub_agents_content["ragflow"]["name"],
        "description": sub_agents_content["ragflow"]["description"],
        "system_prompt": sub_agents_content["ragflow"]["system_prompt"],
        "tools": get_ragflow_tools(),
    }


knowledge_base_agent = None
