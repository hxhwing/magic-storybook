"""MCP server (Streamable HTTP, stateless) exposing the storybook tools.

Mounted onto the main FastAPI app at /mcp by fast_api_app.py, so a single Cloud
Run service serves HTTP + A2A + MCP on one port. Equivalent to the legacy
mcp-handler.mjs (list_books / create_book / get_book).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import tools

# Behind Cloud Run the Host header is the run.app domain, which FastMCP's default
# DNS-rebinding protection (localhost only) rejects with HTTP 421. Disable it —
# host/origin are controlled by the Cloud Run front end.
mcp = FastMCP(
    name="magic-storybook",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
# Serve the JSON-RPC endpoint at the mount root, so mounting at /mcp yields /mcp.
mcp.settings.streamable_http_path = "/"


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


@mcp.tool(
    description="列出所有已创建的绘本（按创建时间倒序，最多 50 本）。返回每本书的 ID、标题、主题、风格、生成状态、页数等摘要信息。"
)
async def list_books() -> str:
    return _dumps(await tools.list_books())


@mcp.tool(
    description="创建一本新绘本。系统会异步生成故事大纲、插画、配音和主题曲，立即返回 bookId，后续可用 get_book 查询进度。"
)
async def create_book(
    title: str,
    theme: str,
    style: str = "",
    music_style: str = "",
    page_count: int = 6,
    outline: str = "",
) -> str:
    """创建新绘本。

    Args:
        title: 绘本标题（中文）。
        theme: 故事主题，如 "友谊"、"勇气"、"太空冒险"。
        style: 画面风格。留空则由模型根据主题自动选择。参考值（可自定义）: 3D动画, 水彩风, 蜡笔画, 剪纸风, 黏土动画, 水墨风。
        music_style: 主题曲风格。留空则由模型根据主题自动选择。参考值（可自定义）: 国风, 动漫, R&B, POP, 儿歌, RAP, 摇篮曲, 古典, 电子。
        page_count: 页数，1-20（默认 6）。
        outline: 可选的故事大纲参考。
    """
    return _dumps(
        await tools.create_book(title, theme, style, music_style, page_count, outline)
    )


@mcp.tool(
    description="获取指定绘本的详细信息，包括生成状态、每页内容、插画/音频/视频 URL 等。可用于轮询创建进度。"
)
async def get_book(book_id: str) -> str:
    """查询绘本详情。

    Args:
        book_id: 绘本 ID，如 book_1713800000000。
    """
    return _dumps(await tools.get_book(book_id))
