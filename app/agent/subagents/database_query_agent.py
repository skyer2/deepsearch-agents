"""
数据库查询子智能体配置模块

将 app/prompt/prompts.yml 中的 db 配置与 MySQL 查询工具组装成
DeepAgents 可识别的字典式子智能体。
"""

from app.agent.prompts import sub_agents_content
from app.mcp.client import get_db_tools


def build_database_query_agent() -> dict:
    return {
        "name": sub_agents_content["db"]["name"],
        "description": sub_agents_content["db"]["description"],
        "system_prompt": sub_agents_content["db"]["system_prompt"],
        "tools": get_db_tools(),
    }


database_query_agent = None
