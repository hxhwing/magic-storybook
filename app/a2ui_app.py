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

"""A2A + A2UI wrapper for the shared magic-storybook agent (app/agent.py).

Serves the SAME agent over A2A with the A2UI extension so Gemini Enterprise renders
a rich storybook card: 书名 + 每页一个 Tab（插画 / 配音 / 文字）+ 共享主题曲 + 沉浸阅读链接.

Key A2A specifics handled here (the agent itself is channel-agnostic):
  • force_new_version=True — the NEW ADK executor emits the reply as ONE artifact.
    (The legacy executor also re-emitted it as a `working` status-update message,
    which GE rendered in its Thinking pane — the reply-duplication bug.)
  • _inject_status_update — turns the agent's ui:status_update state-delta (set by
    show_progress) into a live TaskStatusUpdateEvent for GE's Thinking bar.
  • _activate_a2ui_extension — echoes GE's requested A2UI extension so it renders
    the escape-hatch DataParts (built in app/a2ui_render.py).
  • _SanitizingSessionService — keeps escape-hatch bytes out of session history.
"""

from __future__ import annotations

import datetime as _dt
import os
import uuid as _uuid

from a2a.server.agent_execution import RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2ui.a2a.extension import get_a2ui_agent_extension
from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.catalog import CatalogConfig
from a2ui.schema.catalog_provider import FileSystemCatalogProvider
from a2ui.schema.common_modifiers import remove_strict_validation
from a2ui.schema.constants import VERSION_0_8
from a2ui.schema.manager import A2uiSchemaManager
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig, ExecuteInterceptor
from google.adk.events import Event as AdkEvent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, Session

from app import config
from app.agent import STATUS_KEY, root_agent

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CATALOG_V0_8 = os.path.join(_APP_DIR, "a2ui", "catalog", "0.8", "storybook_catalog_definition.json")
_EXAMPLES_V0_8 = os.path.join(_APP_DIR, "a2ui", "examples", "0.8")
_A2UI_EXT_BASE = "https://a2ui.org/a2a-extension/a2ui"
_ESCAPE_HATCH_PREFIX = b"<a2a_datapart_json>"


class _SanitizingSessionService(InMemorySessionService):
    """Strip A2UI escape-hatch parts before storing them in session history.

    _finalize_turn returns Content with inline_data escape-hatch bytes so the A2A
    layer converts them to DataParts. Keeping those bytes in history would make
    Gemini re-read and echo the raw JSON as text on the next turn — so drop them
    here (session storage and A2A event yielding are independent in the Runner).
    """

    async def append_event(self, session: Session, event: AdkEvent) -> AdkEvent:
        return await super().append_event(session, self._strip_escape_hatch(event))

    @staticmethod
    def _strip_escape_hatch(event: AdkEvent) -> AdkEvent:
        if not (event.content and event.content.parts):
            return event
        clean = [
            p for p in event.content.parts
            if not (
                getattr(p, "inline_data", None)
                and getattr(p.inline_data, "data", None)
                and isinstance(p.inline_data.data, (bytes, bytearray))
                and p.inline_data.data.startswith(_ESCAPE_HATCH_PREFIX)
            )
        ]
        if len(clean) == len(event.content.parts):
            return event
        # Return a NEW event — mutating event.content in place would also strip the
        # card parts from the live A2A stream (the same object is yielded there).
        new_content = event.content.model_copy(update={"parts": clean or None})
        return event.model_copy(update={"content": new_content})


async def _activate_a2ui_extension(ctx: RequestContext) -> RequestContext:
    """Echo back whichever A2UI extension GE advertised, so it renders DataParts.

    GE reads the X-A2A-Extensions response header (from ctx.activated_extensions)
    to decide whether to render A2UI DataParts. Runs as a before_agent interceptor
    so it works with the new executor impl (which bypasses _prepare_session).
    """
    for uri in (ctx.requested_extensions or set()):
        if uri.startswith(_A2UI_EXT_BASE):
            ctx.add_activated_extension(uri)
    return ctx


async def _inject_status_update(executor_context, a2a_event, adk_event):
    """Turn a ui:status_update state-delta into a live Thinking-bar status event.

    show_progress writes ui:status_update to state; GE reads it only from a
    TaskStatusUpdateEvent whose message part is flagged adk_thought. We intercept
    each outgoing event and, when the delta carries a status, prepend that event.
    """
    if not adk_event:
        return a2a_event
    state_delta = getattr(getattr(adk_event, "actions", None), "state_delta", None)
    if not state_delta or STATUS_KEY not in state_delta:
        return a2a_event
    status_value = state_delta.get(STATUS_KEY)
    if not status_value:
        return a2a_event  # status cleared (None) — nothing to show
    injected = TaskStatusUpdateEvent(
        task_id=a2a_event.task_id,
        context_id=a2a_event.context_id,
        final=False,
        metadata={"adk_actions": {"stateDelta": {STATUS_KEY: status_value}}},
        status=TaskStatus(
            state=TaskState.working,
            message=Message(
                message_id=str(_uuid.uuid4()),
                role=Role.agent,
                parts=[Part(root=TextPart(text=status_value, metadata={"adk_thought": True}))],
            ),
            timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        ),
    )
    return [injected, a2a_event]


class StorybookA2uiApp:
    """Bundles the A2UI schema manager (for the card extension), card and runner."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self._app_name = "app"
        self._schema_manager = A2uiSchemaManager(
            version=VERSION_0_8,
            catalogs=[
                CatalogConfig(
                    name="magic_storybook",
                    provider=FileSystemCatalogProvider(_CATALOG_V0_8),
                    examples_path=_EXAMPLES_V0_8,
                ),
                BasicCatalog.get_config(version=VERSION_0_8, examples_path=_EXAMPLES_V0_8),
            ],
            accepts_inline_catalogs=True,
            schema_modifiers=[remove_strict_validation],
        )
        # Same shared agent as HTTP/MCP; only the session service differs (sanitizing).
        self._runner = Runner(
            app_name=self._app_name,
            agent=root_agent,
            session_service=_SanitizingSessionService(),
        )
        self._agent_card = self._build_agent_card()

    @property
    def agent_card(self) -> AgentCard:
        return self._agent_card

    def get_runner(self) -> Runner:
        return self._runner

    def _build_agent_card(self) -> AgentCard:
        ext = get_a2ui_agent_extension(
            VERSION_0_8,
            self._schema_manager.accepts_inline_catalogs,
            self._schema_manager.supported_catalog_ids,
        )
        return AgentCard(
            name="魔法绘本",
            description=(
                "✨说出你的想法，我就把它变成一本魔法绘本！生成完整故事、逐页精美插画、"
                "有声朗读和专属主题曲，还给你一个能直接翻阅的沉浸式阅读器，画风任你选。"
            ),
            url=f"{self.base_url}/a2a/app",
            version=os.environ.get("AGENT_VERSION", "0.1.0"),
            default_input_modes=["text", "text/plain"],
            default_output_modes=["text", "text/plain"],
            capabilities=AgentCapabilities(streaming=True, extensions=[ext]),
            skills=[
                AgentSkill(
                    id="create_storybook",
                    name="创作绘本",
                    description="说出你的想法即可（书名和主题可由我自动构思），生成完整故事、插画、配音、主题曲，并返回沉浸式阅读器。",
                    tags=["storybook", "绘本", "image", "tts", "music"],
                    examples=[
                        "我想要一个关于月亮和小熊的温馨故事",
                        "来点太空冒险，画风用3D动画",
                        "帮我做一本水彩风的绘本，主题你定",
                    ],
                ),
                AgentSkill(
                    id="browse_storybooks",
                    name="查询/阅读绘本",
                    description="列出最近创建的绘本，或打开某一本进行阅读（自动渲染绘本卡片）。",
                    tags=["storybook", "绘本", "status", "read"],
                    examples=["列出所有绘本", "打开《月亮和小熊》给我看看", "读一下最近那本"],
                ),
            ],
        )


def build_a2a_routes() -> list:
    """Build the A2A (with A2UI) routes to mount on the main FastAPI app."""
    from a2a.server.apps import A2AStarletteApplication
    from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

    a2ui_app = StorybookA2uiApp(base_url=config.APP_URL)
    executor = A2aAgentExecutor(
        runner=a2ui_app.get_runner(),
        config=A2aAgentExecutorConfig(
            execute_interceptors=[
                ExecuteInterceptor(
                    before_agent=_activate_a2ui_extension,
                    after_event=_inject_status_update,
                )
            ],
        ),
        force_new_version=True,
    )
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore()
    )
    a2a_app = A2AStarletteApplication(
        agent_card=a2ui_app.agent_card, http_handler=handler
    )
    return a2a_app.routes(
        rpc_url="/a2a/app",
        agent_card_url=f"/a2a/app{AGENT_CARD_WELL_KNOWN_PATH}",
    )
