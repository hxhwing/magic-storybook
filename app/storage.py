"""Firestore + GCS helpers (async wrappers over the sync Google Cloud clients).

The bucket is PRIVATE. Firestore stores only object paths (blob keys); media is
served via V4 signed URLs minted on read (see `signed_url` / `sign_book`).
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
import wave
from datetime import timedelta
from io import BytesIO
from typing import Any

import google.auth
import google.auth.transport.requests
from google.cloud import firestore, storage

from app.config import (
    FIRESTORE_COLLECTION,
    FIRESTORE_DATABASE,
    GCP_PROJECT_ID,
    GCS_BUCKET,
    SIGNED_URL_TTL_SECONDS,
    SIGNING_SA,
)

_firestore = firestore.Client(project=GCP_PROJECT_ID, database=FIRESTORE_DATABASE)
_gcs = storage.Client(project=GCP_PROJECT_ID)


# ── Firestore ──────────────────────────────────────────────────────────────
def _save_book_sync(book_id: str, data: dict[str, Any]) -> None:
    _firestore.collection(FIRESTORE_COLLECTION).document(book_id).set(data, merge=True)


def _get_book_sync(book_id: str) -> dict[str, Any] | None:
    doc = _firestore.collection(FIRESTORE_COLLECTION).document(book_id).get()
    return {"id": doc.id, **doc.to_dict()} if doc.exists else None


def _list_books_sync(limit: int = 50) -> list[dict[str, Any]]:
    snap = (
        _firestore.collection(FIRESTORE_COLLECTION)
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [{"id": d.id, **d.to_dict()} for d in snap]


def _delete_book_sync(book_id: str) -> None:
    _firestore.collection(FIRESTORE_COLLECTION).document(book_id).delete()
    try:
        bucket = _gcs.bucket(GCS_BUCKET)
        blobs = list(bucket.list_blobs(prefix=f"books/{book_id}/"))
        bucket.delete_blobs(blobs)
    except Exception:
        pass


async def save_book(book_id: str, data: dict[str, Any]) -> None:
    await asyncio.to_thread(_save_book_sync, book_id, data)


async def get_book(book_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_book_sync, book_id)


async def list_books(limit: int = 50) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_books_sync, limit)


async def delete_book(book_id: str) -> None:
    await asyncio.to_thread(_delete_book_sync, book_id)


# ── GCS upload (returns the object PATH, not a URL) ─────────────────────────
def _upload_bytes_sync(data: bytes, path: str, content_type: str) -> str:
    blob = _gcs.bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(data, content_type=content_type)
    return path


async def upload_bytes(data: bytes, path: str, content_type: str) -> str:
    """Upload and return the GCS object path (blob key), e.g. books/<id>/x.png."""
    return await asyncio.to_thread(_upload_bytes_sync, data, path, content_type)


# ── V4 signed URLs (private bucket; IAM signBlob, no key needed) ─────────────
# Firestore stores only object PATHS. Signed URLs are minted fresh on read, with
# a small in-process cache so the same path isn't re-signed on every read.
_signing_creds = None
_creds_lock = threading.Lock()  # serialize init/refresh (list_books signs covers concurrently)
_sign_cache: dict[str, tuple[str, float]] = {}  # path -> (url, minted_at)
# Reuse a cached signed URL only while it still has ample validity left (>= 1 day),
# so a read/refresh never hands back an about-to-expire URL.
_CACHE_REUSE_SECONDS = max(60, SIGNED_URL_TTL_SECONDS - 86400)


def _creds():
    global _signing_creds
    # Lock so concurrent cover-signing threads don't race on init/refresh (which
    # can hand back a half-initialized creds with no token → signing failure).
    with _creds_lock:
        if _signing_creds is None:
            _signing_creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not _signing_creds.valid:
            _signing_creds.refresh(google.auth.transport.requests.Request())
        return _signing_creds


def _signed_url_sync(path: str) -> str:
    now = time.time()
    hit = _sign_cache.get(path)
    if hit and (now - hit[1]) < _CACHE_REUSE_SECONDS:
        return hit[0]
    creds = _creds()
    sa_email = SIGNING_SA or getattr(creds, "service_account_email", None)
    url = _gcs.bucket(GCS_BUCKET).blob(path).generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=SIGNED_URL_TTL_SECONDS),
        method="GET",
        service_account_email=sa_email,
        access_token=creds.token,
    )
    _sign_cache[path] = (url, now)
    return url


async def signed_url(path: str) -> str:
    """Mint (or reuse from cache) a V4 signed URL for a GCS object path."""
    return await asyncio.to_thread(_signed_url_sync, path)


async def sign_book(book: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of the book with *Path fields signed into *Url fields."""
    if not book:
        return book
    b = copy.deepcopy(book)
    if b.get("musicPath"):
        b["musicUrl"] = await signed_url(b["musicPath"])
    for p in b.get("pages") or []:
        if p.get("imagePath"):
            p["imageUrl"] = await signed_url(p["imagePath"])
        if p.get("audioPath"):
            p["audioUrl"] = await signed_url(p["audioPath"])
        if p.get("videoPath"):
            p["videoUrl"] = await signed_url(p["videoPath"])
    return b


# ── WAV helper (wrap raw PCM from the TTS model) ────────────────────────────
def pcm_to_wav(
    pcm: bytes, channels: int = 1, rate: int = 24000, sample_width: int = 2
) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ── Path <-> gs:// helpers ──────────────────────────────────────────────────
def path_to_gcs_uri(path: str) -> str:
    return f"gs://{GCS_BUCKET}/{path}"


def gcs_uri_to_path(uri: str) -> str | None:
    prefix = f"gs://{GCS_BUCKET}/"
    return uri[len(prefix):] if uri.startswith(prefix) else None


def mime_type_from_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "image/png")
