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

"""Combined FastAPI app for magic-storybook.

Single Cloud Run service exposing, on one port:
  1. HTTP direct access — ADK REST API (/run, /run_sse, /apps/*, sessions) via
     get_fast_api_app, plus the storybook REST API (/api/*) for the frontend.
  2. A2A + A2UI — a custom A2A app (app/a2ui_app.py) mounted at /a2a/app, whose
     agent card is at /a2a/app/.well-known/agent-card.json. It declares the A2UI
     extension so Gemini Enterprise renders an interactive reader surface.
  3. MCP — the FastMCP streamable-HTTP server mounted at /mcp.
  4. Static frontend (public/).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.events import Event as AdkEvent
from google.adk.plugins.base_plugin import BasePlugin

from app import config  # noqa: F401  (configures Vertex env + shared client)
from app.a2ui_app import build_a2a_routes
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.mcp_server import mcp
from app.rest_api import router as rest_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("magic-storybook")

_ESCAPE_HATCH_PREFIX = b"<a2a_datapart_json>"


class _StripCardPlugin(BasePlugin):
    """Drop A2UI escape-hatch card parts on the HTTP /run channel.

    The shared agent's after_agent_callback emits the A2UI card as inline_data
    escape-hatch bytes for the A2A channel. The homepage chatbot (/run) can't render
    that and would otherwise store the raw JSON in history — strip it here so the
    chatbot just shows the intro text (and refreshes the shelf via /api/books).
    """

    def __init__(self) -> None:
        super().__init__(name="strip_a2ui_card")

    async def on_event_callback(self, *, invocation_context, event: AdkEvent):
        content = getattr(event, "content", None)
        if not (content and content.parts):
            return None
        clean = [
            p for p in content.parts
            if not (
                getattr(p, "inline_data", None)
                and getattr(p.inline_data, "data", None)
                and isinstance(p.inline_data.data, (bytes, bytearray))
                and p.inline_data.data.startswith(_ESCAPE_HATCH_PREFIX)
            )
        ]
        if len(clean) == len(content.parts):
            return None
        return event.model_copy(
            update={"content": content.model_copy(update={"parts": clean or None})}
        )

PROJECT_ROOT = Path(__file__).parent.parent
APP_DIR = PROJECT_ROOT / "app"
PUBLIC_DIR = PROJECT_ROOT / "public"

setup_telemetry()

_logs_bucket = os.environ.get("LOGS_BUCKET_NAME")
_artifact_service_uri = f"gs://{_logs_bucket}" if _logs_bucket else None

# Build the MCP streamable-HTTP ASGI app up front so mcp.session_manager exists.
_mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Run the MCP session manager for the lifetime of the server.
    async with mcp.session_manager.run():
        yield


# ADK app: HTTP direct access (/run, /run_sse, /apps/*). A2A is mounted below via
# a custom A2UI-enabled app, so get_fast_api_app's auto-a2a is disabled here.
app: FastAPI = get_fast_api_app(
    agents_dir=str(PROJECT_ROOT),
    web=False,
    a2a=False,
    allow_origins=["*"],
    artifact_service_uri=_artifact_service_uri,
    otel_to_cloud=True,
    lifespan=_lifespan,
    extra_plugins=[_StripCardPlugin()],
)
app.title = "magic-storybook"
app.description = "AI storybook agent — HTTP + A2A (A2UI) + MCP"

# Storybook REST API for the frontend.
app.include_router(rest_router)

# A2A + A2UI routes at /a2a/app (card at /a2a/app/.well-known/agent-card.json).
for _route in build_a2a_routes():
    app.router.routes.append(_route)

# MCP endpoint (Streamable HTTP) at /mcp.
app.mount("/mcp", _mcp_app)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    logger.info("feedback: %s", feedback.model_dump())
    return {"status": "success"}


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "healthy"}


# --- Static frontend (named pages, added last so API routes take precedence) ---
_PAGES = {
    "/": "index.html",
    "/create": "create.html",
    "/reader": "reader.html",
    "/edit": "edit.html",
}


def _register_page(route: str, filename: str) -> None:
    async def _serve():
        path = PUBLIC_DIR / filename
        if path.is_file():
            return FileResponse(str(path))
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    app.get(route, include_in_schema=False, response_model=None)(_serve)


for _route, _filename in _PAGES.items():
    _register_page(_route, _filename)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
