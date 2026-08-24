"""
MySQL 数据库查询工具模块

封装数据库查询助手使用的三个 LangChain 工具。
底层逻辑与 MCP Server 共用 app.tools.db_core。
"""

from langchain_core.tools import tool

from app.api.monitor import monitor
from app.tools import db_core


@tool
def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    :return: 有表：可用的表有：表1,表2,表3...
             没有表：没有可用的表
             出现异常：查询出现异常：异常信息
    """
    monitor.report_tool(
        tool_name="数据库表名查询工具：list_sql_tables",
        args={"transport": "langchain-tool"},
    )
    return db_core.list_sql_tables()


@tool
def get_table_data(table_name) -> str:
    """
    查询指定表的前 100 行数据

    当前工具调用之前，应先调用 list_sql_tables 完成表名校验。
    :param table_name: 表名
    :return: CSV 格式数据
    """
    monitor.report_tool(
        tool_name="数据库表数据查询工具：get_table_data",
        args={"table_name": table_name, "transport": "langchain-tool"},
    )
    return db_core.get_table_data(table_name)


@tool
def execute_sql_query(query) -> str:
    """
    执行自定义 SQL 查询

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    :param query: 要执行的自定义 SQL 语句
    :return: CSV 格式数据
    """
    monitor.report_tool(
        tool_name="数据库表数据查询工具：execute_sql_query",
        args={"query": query, "transport": "langchain-tool"},
    )
    return db_core.execute_sql_query(query)


if __name__ == "__main__":
    print(
        execute_sql_query.invoke(
            {
                "query": (
                    "SELECT * FROM `drugs` dgs join sales_records srd "
                    "on dgs.drug_id = srd.drug_id"
                )
            }
        )
    )
