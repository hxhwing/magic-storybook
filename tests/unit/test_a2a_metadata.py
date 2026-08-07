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

"""Regression tests: GE must never receive a stringified adk_custom_metadata.

GE sends request-level metadata (a2uiClientCapabilities + the A2UI extension
URI). ADK's default request converter turns it into
RunConfig.custom_metadata={"a2a_metadata": {...}}, stamps it on every event and
str()s it into the outgoing status update, which GE rejects with:
"Failed to convert status update: ... custom_metadata Input should be a valid
dictionary".
"""

from __future__ import annotations

import uuid

from a2a.server.agent_execution import RequestContext
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    TaskStatusUpdateEvent,
    TextPart,
)
from google.adk.a2a.converters.from_adk_event import convert_event_to_a2a_events
from google.adk.a2a.converters.request_converter import (
    convert_a2a_request_to_agent_run_request,
)
from google.adk.events import Event as AdkEvent
from google.adk.events.event_actions import EventActions

from app.a2ui_app import (
    _drop_request_custom_metadata,
    _strip_stringified_custom_metadata,
)

_GE_REQUEST_METADATA = {
    "a2uiClientCapabilities": {"catalogIds": ["basic"]},
    "extensionUri": "https://a2ui.org/a2a-extension/a2ui/v0.8",
}


def _ge_request_context() -> RequestContext:
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.user,
        parts=[Part(root=TextPart(text="做一本绘本"))],
        metadata=_GE_REQUEST_METADATA,
    )
    return RequestContext(
        request=MessageSendParams(message=message, metadata=_GE_REQUEST_METADATA),
        task_id="task-1",
        context_id="ctx-1",
    )


def _status_update(custom_metadata) -> TaskStatusUpdateEvent:
    """Convert an actions-only ADK event (what show_progress emits) to A2A."""
    adk_event = AdkEvent(
        invocation_id="inv-1",
        author="root_agent",
        actions=EventActions(state_delta={"ui:status_update": "🪄 创作中…"}),
        custom_metadata=custom_metadata,
    )
    events = convert_event_to_a2a_events(adk_event, {}, "task-1", "ctx-1")
    return next(e for e in events if isinstance(e, TaskStatusUpdateEvent))


def test_default_converter_stringifies_custom_metadata():
    """Guard on the upstream behaviour this workaround exists for."""
    custom_metadata = convert_a2a_request_to_agent_run_request(
        _ge_request_context()
    ).run_config.custom_metadata
    assert custom_metadata == {"a2a_metadata": _GE_REQUEST_METADATA}
    metadata = _status_update(custom_metadata).status.message.metadata
    assert isinstance(metadata["adk_custom_metadata"], str)


def test_request_converter_drops_custom_metadata():
    run_request = _drop_request_custom_metadata(_ge_request_context())
    assert run_request.run_config.custom_metadata is None
    assert run_request.new_message.parts  # the message itself is untouched
    metadata = _status_update(run_request.run_config.custom_metadata).status.message.metadata
    assert "adk_custom_metadata" not in metadata


def test_sanitizer_strips_stringified_custom_metadata():
    event = _status_update({"a2a_metadata": _GE_REQUEST_METADATA})
    _strip_stringified_custom_metadata(event)
    assert "adk_custom_metadata" not in event.status.message.metadata


def test_sanitizer_keeps_dict_custom_metadata():
    event = _status_update(None)
    event.status.message.metadata["adk_custom_metadata"] = {"ok": True}
    _strip_stringified_custom_metadata(event)
    assert event.status.message.metadata["adk_custom_metadata"] == {"ok": True}
