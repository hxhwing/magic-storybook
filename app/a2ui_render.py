# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic builder + emitter for the storybook A2UI (v0.8) surface in GE.

Layout: 书名(Text) → Tabs(每页一个 tab: 插画 Image + 配音 AudioPlayer + 文字 Text)
→ 底部一个"沉浸体验"链接. Built in Python for reliability.

Emission: the A2UI messages are emitted as A2A DataParts via ADK's documented
escape hatch — a text/plain inline-data Part wrapped in
``<a2a_datapart_json>…</a2a_datapart_json>`` is converted verbatim into the
enclosed DataPart by ``google.adk.a2a.converters.part_converter``. This bypasses
the send_a2ui_json_to_client toolset's strict validator, whose v0.8 reachability
check rejects Tabs' ``tabItems[].child`` — so with the escape hatch, Tabs render
fine in Gemini Enterprise (GE serves GCS images + AudioPlayer natively; no iframe).
"""

from __future__ import annotations

import json

from google.genai import types as genai_types

A2UI_MIME_TYPE = "application/json+a2ui"
_A2A_TAG_START = b"<a2a_datapart_json>"
_A2A_TAG_END = b"</a2a_datapart_json>"
_PRIMARY_COLOR = "#7C3AED"
# Max pages shown in the GE card; longer books show a "还有 N 页" note + reader link.
_MAX_PREVIEW_PAGES = 6


def _text(cid: str, s: str, hint: str | None = None) -> dict:
    comp: dict = {"Text": {"text": {"literalString": s}}}
    if hint:
        comp["Text"]["usageHint"] = hint
    return {"id": cid, "component": comp}


def build_book_a2ui(
    book: dict, reader_url: str, progress_url: str
) -> tuple[str, list[dict]]:
    """Return (plain-text intro, list of v0.8 A2UI messages) for a book.

    `book` may be a full book document (get_book) or the flat create_book result
    (title/status only, no pages yet).
    """
    book_id = book.get("id") or book.get("bookId") or "x"
    title = book.get("title") or "我的魔法绘本"
    status = book.get("status")
    pages = book.get("pages") or []
    surface_id = f"storybook_{book_id}"

    components: list[dict] = [{"id": "root", "component": {"Card": {"child": "sb-col"}}}]
    root_children: list[str] = ["sb-title"]
    components.append(_text("sb-title", f"✨ {title}", "h2"))

    is_complete = status == "complete" and any(p.get("imageUrl") for p in pages)
    music_url = book.get("musicUrl")

    if is_complete:
        # Tabs — one tab per page (插画 + 讲解配音 + 文字). Capped at _MAX_PREVIEW_PAGES so
        # the tab bar doesn't get unwieldy; extra pages are noted + read in the reader.
        total = len(pages)
        preview = pages[:_MAX_PREVIEW_PAGES]
        tab_items: list[dict] = []
        for i, p in enumerate(preview):
            col_children: list[str] = []
            if p.get("imageUrl"):
                components.append({
                    "id": f"img-{i}",
                    "component": {"Image": {
                        "url": {"literalString": p["imageUrl"]},
                        "usageHint": "largeFeature",
                        "fit": "contain",
                    }},
                })
                col_children.append(f"img-{i}")
            # 讲解（本页配音 / TTS）—— 每页各自一个
            if p.get("audioUrl"):
                components.append(_text(f"aud-label-{i}", "🔊 讲解（本页配音）", "caption"))
                col_children.append(f"aud-label-{i}")
                components.append({
                    "id": f"aud-{i}",
                    "component": {"AudioPlayer": {
                        "url": {"literalString": p["audioUrl"]},
                        "description": {"literalString": f"第 {i + 1} 页讲解配音"},
                    }},
                })
                col_children.append(f"aud-{i}")
            # 只显示故事文字，不显示 interactiveHint（💡 …）
            components.append(_text(f"txt-{i}", p.get("text") or "", "body"))
            col_children.append(f"txt-{i}")
            components.append({
                "id": f"pcol-{i}",
                "component": {"Column": {
                    "children": {"explicitList": col_children},
                    "alignment": "stretch",
                }},
            })
            tab_items.append({
                "title": {"literalString": p.get("title") or f"第{i + 1}页"},
                "child": f"pcol-{i}",
            })
        components.append({"id": "sb-tabs", "component": {"Tabs": {"tabItems": tab_items}}})
        root_children.append("sb-tabs")

        # 配乐（整本共用一个主题曲播放器，放在 Tab 外面）
        if music_url:
            components.append(_text("mus-label", "🎵 配乐（全书主题曲）", "caption"))
            root_children.append("mus-label")
            components.append({
                "id": "mus",
                "component": {"AudioPlayer": {
                    "url": {"literalString": music_url},
                    "description": {"literalString": "绘本主题曲"},
                }},
            })
            root_children.append("mus")
        # “还有 N 页”提示放在主题曲下面
        if total > len(preview):
            components.append(_text(
                "sb-more", f"**…… 还有 {total - len(preview)} 页，点下方链接打开完整绘本查看**", "body"))
            root_children.append("sb-more")
        components.append(_text(
            "sb-link",
            f"✨ [在新页面中打开链接，浏览完整绘本 · 开启沉浸式体验]({reader_url})",
            "body",
        ))
        root_children.append("sb-link")
        intro = f"《{title}》来啦！点开每个 Tab 逐页翻看（各页有讲解配音），下方可播放全书主题曲～"
    else:
        components.append(_text("sb-status", "🪄 绘本正在创作中，点击下方查看实时进度。", "caption"))
        root_children.append("sb-status")
        components.append(_text("sb-link", f"👉 [查看创作进度]({progress_url})", "body"))
        root_children.append("sb-link")
        intro = f"已开始创作《{title}》，正在生成中～"

    components.append({
        "id": "sb-col",
        "component": {"Column": {
            "children": {"explicitList": root_children},
            "alignment": "stretch",
        }},
    })

    messages = [
        {"surfaceUpdate": {"surfaceId": surface_id, "components": components}},
        {"beginRendering": {"surfaceId": surface_id, "root": "root",
                            "styles": {"primaryColor": _PRIMARY_COLOR}}},
    ]
    return intro, messages


def build_books_list_a2ui(books: list[dict]) -> tuple[str, list[dict]]:
    """Return (plain-text intro, A2UI messages) for a list of storybooks.

    Renders each book as a row: 封面缩略图 + 书名 + 简介 + 阅读/进度链接.
    `books` items come from list_books (with signed `coverUrl` + reader/progress URLs).
    """
    surface_id = "storybooks_list"
    n = len(books)
    components: list[dict] = [{"id": "root", "component": {"Card": {"child": "list-col"}}}]
    root_children: list[str] = ["list-title"]
    components.append(_text("list-title", f"📚 我的绘本（共 {n} 本）", "h2"))

    if not books:
        components.append(_text("list-empty", "还没有绘本，说出你的想法就能创作一本～", "body"))
        root_children.append("list-empty")

    for i, b in enumerate(books):
        title = b.get("title") or "未命名绘本"
        complete = b.get("status") == "complete"
        cover = b.get("coverUrl")

        info_children = [f"bt-{i}"]
        components.append(_text(f"bt-{i}", title, "h3"))
        desc_parts = [x for x in (b.get("theme"),
                                  (f"{b.get('pageCount')}页" if b.get("pageCount") else None),
                                  (None if complete else "创作中")) if x]
        if desc_parts:
            components.append(_text(f"bd-{i}", " · ".join(desc_parts), "caption"))
            info_children.append(f"bd-{i}")
        components.append({"id": f"bcol-{i}", "component": {"Column": {
            "children": {"explicitList": info_children}, "alignment": "start"}}})

        row_children: list[str] = []
        if cover:
            # 小尺寸封面（avatar，比 icon 大、看得清，行仍紧凑）
            components.append({"id": f"bimg-{i}", "component": {"Image": {
                "url": {"literalString": cover}, "usageHint": "avatar", "fit": "cover"}}})
            row_children.append(f"bimg-{i}")
        row_children.append(f"bcol-{i}")
        components.append({"id": f"brow-{i}", "component": {"Row": {
            "children": {"explicitList": row_children}, "alignment": "center"}}})
        root_children.append(f"brow-{i}")

    components.append({"id": "list-col", "component": {"Column": {
        "children": {"explicitList": root_children}, "alignment": "stretch"}}})

    messages = [
        {"surfaceUpdate": {"surfaceId": surface_id, "components": components}},
        {"beginRendering": {"surfaceId": surface_id, "root": "root",
                            "styles": {"primaryColor": _PRIMARY_COLOR}}},
    ]
    intro = (
        f"这是你最近的 {n} 本绘本，你想看哪一本呢？"
        if n else "你还没有创建绘本～说出你的想法就能创作一本。"
    )
    return intro, messages


# ── Escape-hatch emission (A2UI messages -> A2A DataParts) ───────────────────
def _message_to_part(message: dict) -> genai_types.Part:
    data_part = {"kind": "data", "data": message, "metadata": {"mimeType": A2UI_MIME_TYPE}}
    payload = _A2A_TAG_START + json.dumps(data_part, ensure_ascii=False).encode() + _A2A_TAG_END
    return genai_types.Part(
        inline_data=genai_types.Blob(mime_type="text/plain", data=payload)
    )


def a2ui_card_parts(messages: list[dict]) -> list[genai_types.Part]:
    """Convert A2UI messages into emittable ADK Parts (one per message)."""
    return [_message_to_part(m) for m in messages]
