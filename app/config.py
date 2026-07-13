"""magic-storybook configuration.

Central place for GCP project settings, model IDs, generation tuning knobs and
the shared async genai client.
"""

from __future__ import annotations

import os

import google.auth
from google import genai

# ---------------------------------------------------------------------------
# GCP / auth
# ---------------------------------------------------------------------------
try:
    _, _default_project = google.auth.default()
except Exception:  # pragma: no cover - ADC not available (e.g. some CI)
    _default_project = ""

GCP_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or _default_project or ""
GCP_LOCATION = os.environ.get("GCP_LOCATION") or os.environ.get(
    "GOOGLE_CLOUD_LOCATION", "global"
)
# Defaults follow the deploy.sh naming (magic-storybook-<PROJECT_ID>); in production
# these are always set explicitly via env vars, so the defaults only matter locally.
GCS_BUCKET = os.environ.get("GCS_BUCKET") or f"magic-storybook-{GCP_PROJECT_ID}"
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE") or f"magic-storybook-{GCP_PROJECT_ID}"
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "storybooks")

# Private bucket → media served via V4 signed URLs (max TTL 7 days), minted on read
# via IAM signBlob. SIGNING_SA is the SA to sign as (set by deploy.sh); it needs
# roles/iam.serviceAccountTokenCreator on itself. Falls back to the ADC SA email.
SIGNED_URL_TTL_SECONDS = int(os.environ.get("SIGNED_URL_TTL_SECONDS", str(7 * 24 * 3600)))
SIGNING_SA = os.environ.get("SIGNING_SA", "")

# Make sure ADK / genai pick up Vertex config consistently.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", GCP_PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", GCP_LOCATION)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
# Story outline + root agent — switched to gemini-3.5-flash per project spec.
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash")
GEMINI_IMAGE_MODEL = os.environ.get(
    "GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image"
)
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
TTS_VOICE = os.environ.get("TTS_VOICE", "Achernar")
LYRIA_MODEL = os.environ.get("LYRIA_MODEL", "lyria-3-pro-preview")
VEO_MODEL = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001")
# Shared aspect ratio for BOTH page illustrations and page videos, so the video
# matches the image ratio. Veo supports "16:9" / "9:16".
MEDIA_ASPECT_RATIO = os.environ.get("MEDIA_ASPECT_RATIO", "16:9")

# ---------------------------------------------------------------------------
# Generation tuning (env-overridable, same defaults as the Node version)
# ---------------------------------------------------------------------------
IMAGE_GEN_MAX_RETRIES = int(os.environ.get("IMAGE_GEN_MAX_RETRIES", "5"))
IMAGE_GEN_RETRY_DELAY_MS = int(os.environ.get("IMAGE_GEN_RETRY_DELAY_MS", "10000"))
IMAGE_GEN_CONCURRENCY = int(os.environ.get("IMAGE_GEN_CONCURRENCY", "2"))
TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "5"))
VIDEO_GEN_CONCURRENCY = int(os.environ.get("VIDEO_GEN_CONCURRENCY", "2"))
VIDEO_POLL_INTERVAL_MS = int(os.environ.get("VIDEO_POLL_INTERVAL_MS", "15000"))
VIDEO_GEN_MAX_RETRIES = int(os.environ.get("VIDEO_GEN_MAX_RETRIES", "1"))

# ---------------------------------------------------------------------------
# Shared genai client (Vertex AI via ADC). Use `.aio` for async calls.
# ---------------------------------------------------------------------------
genai_client = genai.Client(
    vertexai=True, project=GCP_PROJECT_ID, location=GCP_LOCATION
)

# Public base URL of THIS service (set at deploy time). Used for the A2A card's
# RPC url (…/a2a/app).
APP_URL = os.environ.get("APP_URL", f"http://localhost:{os.environ.get('PORT', '8000')}")

# Base URL of the user-facing web frontend for reader/progress links. On a split
# deployment the A2A service points this at the (IAP) frontend service; otherwise
# it defaults to this service.
READER_BASE_URL = os.environ.get("READER_BASE_URL", APP_URL)


def reader_url(book_id: str) -> str:
    """Immersive reader URL for a completed book (on the web frontend)."""
    return f"{READER_BASE_URL}/reader?bookId={book_id}"


def progress_url(book_id: str) -> str:
    """Creation/progress page for a book (on the web frontend)."""
    return f"{READER_BASE_URL}/create?bookId={book_id}"


# Valid enum values surfaced to tools / MCP / API.
STYLES = ["3D动画", "水彩风", "蜡笔画", "剪纸风", "黏土动画", "水墨风"]
MUSIC_STYLES = ["国风", "动漫", "R&B", "POP", "儿歌", "RAP", "摇篮曲", "古典", "电子"]
