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

"""magic-storybook — the single shared agent.

ONE agent definition (instruction, tools, model, callbacks) drives every channel:
  • HTTP /run — the homepage chatbot (via get_fast_api_app)
  • A2A — Gemini Enterprise (wrapped with the A2UI card in app/a2ui_app.py)
  • MCP — the tool server

Book creation is a two-step flow so a "创作中" line can stream to GE's Thinking bar
during the ~1-2 min wait: create_book (instant, returns progressUrl) → show_progress
→ wait_for_book (blocks) → the A2UI card is emitted in after_agent_callback.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

# Import for its side effect of configuring Vertex env + the shared client.
from app import config  # noqa: F401
from app.a2ui_render import a2ui_card_parts, build_book_a2ui, build_books_list_a2ui
from app.tools import create_book, get_book, list_books, wait_for_book

# State key GE reads for its "Thinking" bar; the A2A layer (a2ui_app) turns the
# resulting state-delta into a live TaskStatusUpdateEvent. On other channels it is
# a harmless no-op state write.
STATUS_KEY = "ui:status_update"
# Captured card to emit at end of turn: {"kind": "book"|"list", "payload": ...}.
_CARD_KEY = "last_card"

# Disable model "thinking" so no thought parts ever reach GE — only our explicit
# show_progress line appears in the Thinking bar; everything else is the reply/card.
_GEN_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(include_thoughts=False),
)

_INSTRUCTION = """你是 magic-storybook 的创作助手，帮助用户创作绘本。

工具：
- show_progress(message) —— 在等待期间向用户实时显示一句进度提示（可含 Markdown 链接）。
- create_book(title, theme, style, music_style, page_count, outline) —— 提交创作任务并**立即返回** bookId 和 progressUrl（进度页链接）。
- wait_for_book(book_id) —— 等待绘本生成完成（约需 1-2 分钟）后返回完整绘本；期间请耐心等待，不要重复调用。
- get_book(book_id) —— 查询/打开某本已有绘本（会自动渲染该绘本卡片）。
- list_books() —— 列出最近创建的绘本（会自动渲染带封面缩略图的绘本列表卡片）。

**当用户只是打招呼、或问"你能做什么"时**：用一两句话友好地介绍你能把想法变成完整绘本（故事、逐页插画、有声朗读、主题曲、沉浸式阅读），并提示用户可以（可选）指定**页数、画面风格、主题曲风格**——不指定则自动选择；此时先邀请用户说出想法，不要直接创作。
用户只需说出想法即可：**若用户没给出书名或主题，你要根据用户的想法自动拟定贴切有吸引力的 title 和 theme 再创建**，不要反过来要求用户提供。
**若用户没指定画风或主题曲，就把 style / music_style 留空**，由系统根据主题自动选择最合适的；page_count 未指定默认 6。

**创作绘本的流程（严格按顺序）**：
① 调用 create_book，拿到返回的 bookId 和 progressUrl；
② 调用 show_progress，message 形如「🪄 正在为您创作《书名》，正在绘制精美插画与配乐，请稍候 1-2 分钟…　👉 [查看创作进度](progressUrl)」——把《书名》换成真实书名、progressUrl 换成上一步返回的真实链接；
③ 调用 wait_for_book(bookId) 等待生成完成；
④ 生成完成后**不要再输出多余文字**——绘本卡片会自动渲染在下方（含完成介绍）。

**当用户想看有哪些绘本 / 书架时**：调用 list_books，会自动渲染带封面的绘本列表卡片；无需自己拼文字列表。
**当用户想查看/打开/阅读某一本已有绘本时**（例如先 list_books 再选一本，或直接给出书名/bookId）：调用 get_book(book_id)，该绘本卡片会自动渲染，无需自己拼界面。

画面风格参考（可自定义）：3D动画/水彩风/蜡笔画/剪纸风/黏土动画/水墨风。
主题曲风格参考（可自定义）：国风/动漫/R&B/POP/儿歌/RAP/摇篮曲/古典/电子。
回答简洁友好，用中文。"""


def show_progress(message: str, tool_context: ToolContext) -> dict:
    """向用户实时显示一句进度提示（用于创作等待期间）。

    Writes ``message`` to the ``ui:status_update`` state key. On the A2A channel an
    after_event interceptor turns the resulting state-delta into a live
    TaskStatusUpdateEvent that GE renders in its Thinking bar.
    """
    tool_context.state[STATUS_KEY] = message or None
    return {"ok": True}


def _reset_turn_state(callback_context: CallbackContext):
    """Clear ephemeral per-turn UI state at the start of each turn."""
    callback_context.state[STATUS_KEY] = None
    callback_context.state[_CARD_KEY] = None
    return None


def _after_tool(tool, args, tool_context, tool_response):
    """Capture the latest tool result so its A2UI card can be emitted at turn end."""
    if not isinstance(tool_response, dict):
        return None
    if tool.name in ("wait_for_book", "get_book"):
        payload = tool_response
        if (
            "book" not in payload
            and "bookId" not in payload
            and isinstance(payload.get("result"), dict)
        ):
            payload = payload["result"]
        tool_context.state[_CARD_KEY] = {"kind": "book", "payload": payload}
    elif tool.name == "list_books":
        tool_context.state[_CARD_KEY] = {"kind": "list", "payload": tool_response}
    return None


async def _finalize_turn(callback_context: CallbackContext):
    """Emit the A2UI card (book or book-list) as escape-hatch parts at turn end.

    On the A2A channel these convert to A2UI DataParts (GE renders the card). On the
    HTTP /run channel a plugin strips them, so the chatbot just shows the intro text.
    """
    callback_context.state[STATUS_KEY] = None  # clear the Thinking bar
    card = callback_context.state.get(_CARD_KEY)
    callback_context.state[_CARD_KEY] = None
    if not isinstance(card, dict):
        return None

    kind, payload = card.get("kind"), card.get("payload")
    if kind == "list" and isinstance(payload, dict):
        intro, messages = build_books_list_a2ui(payload.get("books") or [])
    elif kind == "book" and isinstance(payload, dict) and not payload.get("error"):
        book = payload.get("book") or payload
        intro, messages = build_book_a2ui(
            book, payload.get("readerUrl", ""), payload.get("progressUrl", "")
        )
    else:
        return None

    parts = [types.Part(text=intro)] + a2ui_card_parts(messages)
    return types.Content(role="model", parts=parts)


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=config.GEMINI_TEXT_MODEL,  # gemini-3.5-flash
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    generate_content_config=_GEN_CONFIG,
    description="创建并管理 AI 生成的绘本（故事、插画、配音、主题曲），并渲染交互式绘本卡片。",
    instruction=_INSTRUCTION,
    tools=[show_progress, create_book, wait_for_book, get_book, list_books],
    before_agent_callback=_reset_turn_state,
    after_tool_callback=_after_tool,
    after_agent_callback=_finalize_turn,
)

app = App(
    root_agent=root_agent,
    name="app",
)
