"""nl2sql_skill MCP 服务端入口（薄层）。

参考 ``retrieve_skill.mcp``：本文件只负责命令行参数解析、加载配置并调用
``create_mcp_server()`` 启动服务；服务器构建与工具定义在 ``mcp_server.py`` 里。
支持 stdio / sse / streamable_http 三种传输方式。入口可通过已安装的包调用：
``python -m nl2sql_skill.mcp`` 或 ``nl2sql-skill-mcp``；IDE 直接运行本文件时
（脚本方式）默认启动 streamable-http，便于一键本地调试。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

if not __package__:
    # 脚本方式直接运行（如 PyCharm Run 按钮）时，Python 会把脚本所在目录
    # 放进 sys.path[0]，其中的 mcp.py 会遮蔽第三方 mcp 包，因此只移除它；
    # common_core / nl2sql_skill 由 editable 安装提供，不再手动拼源码路径。
    script_dir = str(Path(sys.argv[0]).resolve().parent)
    sys.path[:] = [
        entry
        for entry in sys.path
        if entry and str(Path(entry).resolve()) != script_dir
    ]

from common_core.observability import Observability

if __package__:
    from .mcp_server import create_mcp_server
else:
    from nl2sql_skill.mcp_server import create_mcp_server


def _normalize_transport(value: str) -> str:
    """把 CLI 里两种拼法并归成一个：``streamable-http`` / ``streamable_http``。"""
    return "streamable-http" if value.replace("_", "-") == "streamable-http" else value


def _run_server(server: Any, transport: str) -> None:
    """按传输方式启动 FastMCP 服务（stdio / sse / streamable-http）。"""
    import asyncio

    if transport == "stdio":
        server.run(transport="stdio")
        return
    if transport == "sse":
        asyncio.run(server.run_sse_async())
        return
    asyncio.run(server.run_streamable_http_async())


def _build_runtime(env_file: str | None) -> tuple[Any, str]:
    """解析并加载 .env，返回校验后的配置与配置来源。

    环境文件优先级：--env-file > NL2SQL_ENV_FILE > 当前工作目录的 .env；
    找不到时再兜底读取 nl2sql_skill 项目根目录下的 .env，保证 CLI 与 IDE
    一键启动两个入口的配置加载一致。

    缺少数据源注册或 LLM 必填项时 fail-fast，拒绝带病运行。
    """
    from common_core.config import load_env_files, resolve_env_file

    from .builder import build_llm
    from .config import NL2SQLConfig

    resolved = resolve_env_file(env_file, env_key="NL2SQL_ENV_FILE")
    if resolved is None:
        # 脚本方式直接运行（如 IDE Run 按钮）时，工作目录不一定是项目根目录。
        # 兜底用 nl2sql_skill 包根目录下的 .env，避免配置找不到。
        fallback = Path(__file__).resolve().parents[2] / ".env"
        if fallback.is_file():
            resolved = str(fallback)
    if resolved:
        load_env_files(resolved)
    config = NL2SQLConfig.from_env()
    if not config.datasources:
        raise RuntimeError(
            "未配置任何数据源：请设置 NL2SQL_DB_<ID>_DSN（可参考 .env.example）"
        )
    # LLM 必填项（BASE_URL / MODEL）缺失时 build_llm 抛 ConfigError，fail-fast。
    build_llm()
    source = "env:" + resolved if resolved else "process-env"
    logger.info(
        "config source=%s datasources=%s",
        source,
        ",".join(sorted(config.datasources)),
    )
    return config, source


def main() -> None:
    """运行 MCP 服务端并加载配置，支持多种传输方式。

    默认传输：以脚本方式直接运行（IDE Run 按钮）时为 ``streamable-http``，
    方便一键启动 HTTP 服务；以已安装包 / ``python -m`` 方式运行时为 ``stdio``，
    适合被本地 MCP client 拉起。显式传 ``--transport`` 仍可覆盖。
    ``--transport sse`` / ``--transport streamable-http``（或 ``streamable_http``）
    会启动一个 HTTP 服务并暴露 URL，供远程调用方 / MCP client 连接。
    缺少必需配置时快速报错终止。
    """
    import argparse

    default_transport = "streamable-http" if not __package__ else "stdio"
    parser = argparse.ArgumentParser(prog="nl2sql-skill-mcp")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to a .env file (default: NL2SQL_ENV_FILE or ./.env)",
    )
    parser.add_argument(
        "--transport",
        default=default_transport,
        choices=["stdio", "sse", "streamable-http", "streamable_http"],
        help=(
            f"Transport to expose (default: {default_transport}). "
            "sse / streamable-http start an HTTP server."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host for HTTP transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port for HTTP transports (default: 8000)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="HTTP endpoint path for streamable-http (default /streamable) or sse (default /sse)",
    )
    args = parser.parse_args()

    config, config_source = _build_runtime(args.env_file)
    metrics = Observability.from_env()
    metrics.start_server()

    transport = _normalize_transport(args.transport)
    if args.host is not None or args.port is not None or args.path is not None:
        host = "127.0.0.1" if args.host is None else args.host
        port = 8000 if args.port is None else args.port
        streamable_path = args.path or "/streamable"
        sse_path = args.path or "/sse"
    else:
        host, port, streamable_path, sse_path = None, None, "/streamable", "/sse"
    server = create_mcp_server(
        host=host,
        port=port,
        streamable_path=streamable_path,
        sse_path=sse_path,
        metrics=metrics,
        config=config,
        config_source=config_source,
    )
    _run_server(server, transport)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_mcp_server", "main"]
