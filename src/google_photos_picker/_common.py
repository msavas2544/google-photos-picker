"""Shared request building and helpers for the sync and async clients."""

from __future__ import annotations

import os
import random
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union

import httpx

from ._version import __version__
from .errors import from_response
from .models import MAX_ITEMS, PickedMediaItem, PickingSession

BASE_URL = "https://photospicker.googleapis.com/v1/"
USER_AGENT = f"google-photos-picker-python/{__version__}"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_POLL_INTERVAL = 5.0


@dataclass
class Call:
    method: str
    path: str
    params: Optional[Dict[str, Any]] = None
    json: Any = None
    idempotent: bool = True


def session_id(session: Union[PickingSession, str]) -> str:
    sid = session.id if isinstance(session, PickingSession) else str(session)
    if sid.startswith("sessions/"):
        sid = sid.split("/", 1)[1]
    if not sid or "/" in sid:
        raise ValueError(f"invalid session id {sid!r}")
    return sid


def create_call(max_items: Optional[int], request_id: Optional[str]) -> Call:
    body: Dict[str, Any] = {}
    if max_items is not None:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        body["pickingConfig"] = {"maxItemCount": str(min(max_items, MAX_ITEMS))}
    params = None
    if request_id is not None:
        uuid.UUID(request_id, version=4)  # validates format, raises ValueError
        params = {"requestId": request_id}
    return Call("POST", "sessions", params, body, idempotent=request_id is not None)


def list_call(sid: str, page_size: int, token: Optional[str]) -> Call:
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    params: Dict[str, Any] = {"sessionId": sid, "pageSize": page_size}
    if token:
        params["pageToken"] = token
    return Call("GET", "mediaItems", params)


def backoff(attempt: int, base: float, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 60.0))
        except ValueError:
            pass
    return min(base * (2 ** attempt), 30.0) * (0.5 + random.random() / 2)


def should_retry(call: Call, status: int, attempt: int, max_retries: int) -> bool:
    if attempt >= max_retries or status not in RETRY_STATUSES:
        return False
    return call.idempotent or status in (429, 503)


def error(resp: httpx.Response) -> Exception:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return from_response(resp.status_code, body)


def next_poll_delay(s: PickingSession, min_interval: float) -> float:
    interval = s.polling.poll_interval if s.polling and s.polling.poll_interval else None
    return max(min_interval, interval if interval is not None else DEFAULT_POLL_INTERVAL)


def polling_expired(s: PickingSession) -> bool:
    return bool(s.polling and s.polling.timeout_in is not None and s.polling.timeout_in <= 0)


# --------------------------------------------------------------------------- filenames
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
                "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4",
                "video/quicktime": ".mov"}


def safe_filename(item: PickedMediaItem) -> str:
    name = _UNSAFE.sub("_", os.path.basename(item.filename or "")).strip(" .")
    if not name:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", item.id[:32]) + _EXT_BY_MIME.get(
            item.mime_type or "", "")
    return name


def fix_extension(name: str, content_type: Optional[str]) -> str:
    """Make the extension match what the server actually sent (resized videos come back
    as JPEG thumbnails, ``=dv`` may be a transcode in a different container)."""
    if not content_type:
        return name
    ext = _EXT_BY_MIME.get(content_type.split(";")[0].strip().lower())
    if not ext:
        return name
    stem, cur = os.path.splitext(name)
    if cur.lower() in (ext, ".jpeg" if ext == ".jpg" else ext):
        return name
    return stem + ext


def unique_path(directory: Path, name: str, taken: Set[str]) -> Path:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while candidate.lower() in taken or (directory / candidate).exists():
        candidate = f"{stem} ({n}){ext}"
        n += 1
    taken.add(candidate.lower())
    return directory / candidate
