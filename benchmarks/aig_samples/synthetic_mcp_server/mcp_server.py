"""白名单合成 MCP 样例：仅用于 A.I.G 扫描链路验证，不含真实凭据。"""

import os
import subprocess

from mcp.server.fastmcp import FastMCP

server = FastMCP("synthetic-demo")


@server.tool()
def run_diagnostic(command: str) -> str:
    """执行诊断命令（合成样例，故意保留不安全模式以触发扫描）。"""
    result = subprocess.check_output(command, shell=True, text=True)
    return result


@server.tool()
def read_demo_file(path: str) -> str:
    """读取任意路径（合成样例，故意保留路径穿越风险）。"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@server.tool()
def demo_token() -> str:
    """返回演示占位符，不是真实凭据。"""
    return os.environ.get("DEMO_TOKEN_PLACEHOLDER", "DEMO_PLACEHOLDER_NOT_A_SECRET")


if __name__ == "__main__":
    server.run()
