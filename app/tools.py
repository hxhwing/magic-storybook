"""Business logic shared by the ADK agent, the MCP server and the REST API.

Each function returns a JSON-serializable dict. Book generation runs in the
background (fire-and-forget asyncio task) exactly like the legacy Node version,
so Cloud Run must run with CPU always allocated (--no-cpu-throttling).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app import generation, storage
from app.config import GCS_BUCKET, progress_url, reader_url

# Keep strong references so fire-and-forget tasks are not garbage collected.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def create_book(
    title: str,
    theme: str,
    style: str = "",
    music_style: str = "",
    page_count: int = 6,
    outline: str = "",
) -> dict:
    """Create a new storybook. Generates outline, illustrations, narration,
    theme music and animated videos asynchronously and returns immediately.

    Args:
        title: Storybook title (Chinese).
        theme: Story theme, e.g. "友谊" / "勇气" / "太空冒险".
        style: Illustration style. Leave EMPTY to let the model pick the best
            fit for the theme. Reference values (custom allowed):
            3D动画, 水彩风, 蜡笔画, 剪纸风, 黏土动画, 水墨风.
        music_style: Theme-song style. Leave EMPTY to auto-pick by theme.
            Reference values (custom allowed):
            国风, 动漫, R&B, POP, 儿歌, RAP, 摇篮曲, 古典, 电子.
        page_count: Number of pages, 1-20.
        outline: Optional outline reference to steer the story.

    Returns:
        dict with bookId, status and a message.
    """
    if not title or not theme:
        return {"error": "缺少 title 或 theme"}

    book_id = f"book_{int(time.time() * 1000)}"
    pc = min(max(int(page_count or 6), 1), 20)
    book_data = {
        "bookId": book_id,
        "title": title,
        "theme": theme,
        "style": style,
        "musicStyle": music_style,
        "pageCount": pc,
        "status": "queued",
        "progress": 0,
        "statusMessage": "排队中，即将开始创作...",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "gcsBucket": GCS_BUCKET,
        "pages": [],
    }
    await storage.save_book(book_id, book_data)
    _spawn(
        generation.run_generation_job(
            book_id, title, theme, style, pc, outline or None, music_style
        )
    )
    return {
        "bookId": book_id,
        "title": title,
        "status": "queued",
        "message": "绘本创作任务已提交！可在创作进度页查看生成过程。",
        # After creation the book is still generating — send users to the
        # progress page, not the reader (which is for a completed book).
        "progressUrl": progress_url(book_id),
        "readerUrl": reader_url(book_id),
    }


async def wait_for_book(book_id: str, timeout_seconds: int = 240) -> dict:
    """Wait until a storybook that is already being created becomes ready to read,
    then return the completed book. Poll-based (illustrations, narration and theme
    song are awaited; page videos keep generating in the background). Pair with
    create_book for synchronous clients that show the finished book in one turn
    (e.g. Gemini Enterprise). May block 1-2 minutes.

    Args:
        book_id: The book ID returned by create_book.
        timeout_seconds: Max seconds to wait before returning the current state.

    Returns:
        dict with the completed book, readerUrl and progressUrl.
    """
    if not book_id:
        return {"error": "缺少 bookId"}
    deadline = time.time() + max(30, int(timeout_seconds or 240))
    while time.time() < deadline:
        book = await storage.get_book(book_id)
        if not book:
            return {"error": "绘本未找到", "bookId": book_id}
        status = book.get("status")
        if status == "complete":
            return {
                "book": await storage.sign_book(book),
                "readerUrl": reader_url(book_id),
                "progressUrl": progress_url(book_id),
            }
        if status == "error":
            return {
                "error": book.get("statusMessage") or "生成失败",
                "bookId": book_id,
                "book": await storage.sign_book(book),
            }
        await asyncio.sleep(3)
    # Timed out — return whatever we have so the caller still gets something.
    book = await storage.get_book(book_id)
    return {
        "book": await storage.sign_book(book) if book else None,
        "readerUrl": reader_url(book_id),
        "progressUrl": progress_url(book_id),
        "timedOut": True,
    }


async def get_book(book_id: str) -> dict:
    """Get full details and generation progress of a storybook.

    Args:
        book_id: The book ID, e.g. book_1713800000000.

    Returns:
        dict with the book document, or an error.
    """
    if not book_id:
        return {"error": "缺少 bookId"}
    book = await storage.get_book(book_id)
    if not book:
        return {"error": "绘本未找到", "bookId": book_id}
    # readerUrl for a completed book; progressUrl while it is still generating.
    return {
        "book": await storage.sign_book(book),
        "readerUrl": reader_url(book_id),
        "progressUrl": progress_url(book_id),
    }


def _first_image_path(book: dict) -> str | None:
    for p in book.get("pages") or []:
        if p.get("imagePath"):
            return p["imagePath"]
    return None


async def list_books() -> dict:
    """List all storybooks (most recent first, up to 50).

    Each entry includes a signed cover URL (the first page's illustration; the
    bucket is private so it must be signed on read) for rich rendering.

    Returns:
        dict with count and a list of book summaries.
    """
    books = await storage.list_books()
    # Sign each book's cover (first-page image) concurrently — private bucket.
    covers = await asyncio.gather(*[
        storage.signed_url(_first_image_path(b)) if _first_image_path(b) else _none()
        for b in books
    ])
    summary = [
        {
            "id": b.get("id"),
            "title": b.get("title"),
            "theme": b.get("theme"),
            "style": b.get("style"),
            "status": b.get("status"),
            "statusMessage": b.get("statusMessage"),
            "pageCount": b.get("pageCount"),
            "createdAt": b.get("createdAt"),
            "coverUrl": covers[i],
            "readerUrl": reader_url(b["id"]) if b.get("id") else None,
            "progressUrl": progress_url(b["id"]) if b.get("id") else None,
        }
        for i, b in enumerate(books)
    ]
    return {"count": len(summary), "books": summary}


async def _none():
    return None
