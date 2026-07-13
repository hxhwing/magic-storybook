"""REST API router — direct HTTP access for the static frontend.

Mirrors the /api/* routes from legacy-node/server.js so the existing
public/*.html frontend keeps working unchanged.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app import generation, storage, tools

router = APIRouter(prefix="/api", tags=["storybook"])


@router.post("/generate")
async def generate(payload: dict = Body(...)) -> JSONResponse:
    title = payload.get("title")
    theme = payload.get("theme")
    if not title or not theme:
        return JSONResponse({"error": "缺少标题或主题"}, status_code=400)
    result = await tools.create_book(
        title=title,
        theme=theme,
        style=payload.get("style", ""),
        music_style=payload.get("musicStyle", ""),
        page_count=int(payload.get("pageCount") or 6),
        outline=payload.get("outline", "") or "",
    )
    return JSONResponse(
        {"bookId": result["bookId"], "status": "queued", "message": "绘本创作任务已提交！"},
        status_code=202,
    )


@router.get("/books")
async def get_books() -> dict:
    # Sign media (V4) on read — the bucket is private, Firestore stores paths.
    books = await storage.list_books()
    return {"books": [await storage.sign_book(b) for b in books]}


@router.get("/books/{book_id}")
async def get_one(book_id: str) -> JSONResponse:
    book = await storage.get_book(book_id)
    if not book:
        return JSONResponse({"error": "绘本未找到"}, status_code=404)
    return JSONResponse({"book": await storage.sign_book(book)})


@router.patch("/books/{book_id}")
async def patch_book(book_id: str, payload: dict = Body(...)) -> JSONResponse:
    await storage.save_book(book_id, payload)
    return JSONResponse({"ok": True, "book": await storage.sign_book(await storage.get_book(book_id))})


@router.delete("/books/{book_id}")
async def delete_book(book_id: str) -> JSONResponse:
    await storage.delete_book(book_id)
    return JSONResponse({"ok": True, "message": "绘本及相关资源已删除"})


@router.post("/books/{book_id}/regenerate-page")
async def regenerate_page(book_id: str, payload: dict = Body(...)) -> JSONResponse:
    page_index = payload.get("pageIndex")
    image_prompt = payload.get("imagePrompt")
    text = payload.get("text")
    if page_index is None or not image_prompt or not text:
        return JSONResponse({"error": "缺少 pageIndex / imagePrompt / text"}, status_code=400)
    book = await storage.get_book(book_id)
    if not book:
        return JSONResponse({"error": "绘本未找到"}, status_code=404)

    style = payload.get("style") or book.get("style") or "3D动画"
    data, mime = await generation.generate_page_image_with_retry(
        image_prompt, style, f"[Regen {book_id}] Page {page_index + 1}"
    )
    ext = mime.split("/")[1] or "png"
    path = f"books/{book_id}/page_{str(page_index + 1).zfill(2)}_v{int(time.time() * 1000)}.{ext}"
    image_path = await storage.upload_bytes(data, path, mime)

    pages = book.get("pages") or []
    if 0 <= page_index < len(pages):
        pages[page_index]["imagePath"] = image_path
        pages[page_index]["text"] = text
        pages[page_index]["imagePrompt"] = image_prompt
        await storage.save_book(book_id, {"pages": pages})

    image_url = await storage.signed_url(image_path)
    return JSONResponse({"ok": True, "imageUrl": image_url, "message": "页面已重新生成！"})
