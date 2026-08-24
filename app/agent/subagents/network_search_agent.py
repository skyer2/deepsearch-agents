"""
网络搜索子智能体配置模块

将 app/prompt/prompts.yml 中的 tavily 配置与 internet_search 工具组装成
DeepAgents 可识别的字典式子智能体。
"""

from app.agent.prompts import sub_agents_content
from app.mcp.client import get_internet_search_tool


def build_network_search_agent() -> dict:
    return {
        "name": sub_agents_content["tavily"]["name"],
        "description": sub_agents_content["tavily"]["description"],
        "system_prompt": sub_agents_content["tavily"]["system_prompt"],
        "tools": [get_internet_search_tool()],
    }


# 向后兼容：需在 bootstrap_mcp_registry() 之后访问
network_search_agent = None
