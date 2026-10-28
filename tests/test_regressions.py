"""Regression tests for code-review findings."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest

from google_photos_picker import (
    AsyncPickerClient,
    PermissionDenied,
    PickedMediaItem,
    PickerClient,
    PickingSession,
)
from google_photos_picker import _common as c
from google_photos_picker.models import _timestamp

from .test_picker import PHOTO, PROCESSING, VIDEO, session_json


def item(**over):
    d = {**PHOTO, "mediaFile": {**PHOTO["mediaFile"], **over}}
    return PickedMediaItem.from_api(d)


def test_safe_filename_hardening() -> None:
    assert c.safe_filename(item(filename="../../etc/passwd")) == "passwd"
    assert c.safe_filename(item(filename="C:\\x\\y.jpg")) == "y.jpg"
    assert c.safe_filename(item(filename="CON.jpg")) == "_CON.jpg"
    assert c.safe_filename(item(filename="evil\u202egpj.exe")) == "evilgpj.exe"
    long = c.safe_filename(item(filename="é" * 300 + ".jpg"))
    assert long.endswith(".jpg") and len(long.encode()) <= 200


def test_timestamp_any_fraction() -> None:
    assert _timestamp("2026-01-01T00:00:00.1Z").microsecond == 100000
    assert _timestamp("2026-01-01T00:00:00.123456789Z").microsecond == 123456
    assert _timestamp("2026-01-01T00:00:00Z").microsecond == 0


def test_request_id_validation() -> None:
    u = uuid.uuid4()
    assert c.create_call(None, str(u).upper()).params == {"requestId": str(u)}
    with pytest.raises(ValueError):
        c.create_call(None, str(uuid.uuid1()))
    assert c.create_call(None, u.hex).params == {"requestId": str(u)}


def test_poll_delay_capped_by_timeout_in() -> None:
    s = PickingSession.from_api(session_json(poll="10s", timeout_in="3s"))
    assert c.next_poll_delay(s, 1.0) == 3.0


def _client(handler, **kw) -> PickerClient:
    return PickerClient("tok", http=httpx.Client(transport=httpx.MockTransport(handler)),
                        sleep=lambda s: None, **kw)


def test_transport_errors_retried_for_reads_and_streams() -> None:
    calls = []

    def handler(r):
        calls.append(r.url.path)
        if len(calls) in (1, 3):
            raise httpx.ConnectError("blip")
        if "googleusercontent" in str(r.url):
            return httpx.Response(200, content=b"X", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json=session_json())

    cl = _client(handler)
    assert cl.get_session("sess-123").id == "sess-123"
    assert cl.read(PickedMediaItem.from_api(PHOTO)) == b"X"
    assert len(calls) == 4


def test_create_not_retried_on_transport_error() -> None:
    def handler(r):
        raise httpx.ConnectError("down")
    with pytest.raises(httpx.ConnectError):
        _client(handler).create_session()


def test_trailing_slash_creates_directory(tmp_path) -> None:
    cl = _client(lambda r: httpx.Response(200, content=b"X",
                                          headers={"content-type": "image/jpeg"}))
    p = cl.download(PickedMediaItem.from_api(PHOTO), str(tmp_path / "new") + "/")
    assert p == tmp_path / "new" / "IMG_0001.JPG" and p.read_bytes() == b"X"


def test_photo_size_leaves_videos_alone(tmp_path) -> None:
    urls = []

    def handler(r):
        urls.append(str(r.url).rsplit("=", 1)[1])
        ct = "video/mp4" if "video" in str(r.url) and urls[-1] == "dv" else "image/jpeg"
        return httpx.Response(200, content=b"X", headers={"content-type": ct})

    items = [PickedMediaItem.from_api(PHOTO), PickedMediaItem.from_api(VIDEO)]
    paths = _client(handler).download_all(items, tmp_path, photo_size=512)
    assert urls == ["w512-h512", "dv"]
    assert sorted(p.name for p in paths) == ["IMG_0001.JPG", "clip.mp4"]


async def test_async_failure_cancels_siblings_and_reports_unready(tmp_path) -> None:
    started = []

    async def handler(r):
        started.append(str(r.url))
        if "photo1" in str(r.url):
            return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED",
                                                       "message": "revoked"}})
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=b"X", headers={"content-type": "video/mp4"})

    reported = []
    many = [PickedMediaItem.from_api({**VIDEO, "id": f"v{i}"}) for i in range(3)]
    items = [PickedMediaItem.from_api(PHOTO), PickedMediaItem.from_api(PROCESSING), *many]
    async with AsyncPickerClient(
            "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as cl:
        with pytest.raises(PermissionDenied):
            await cl.download_all(items, tmp_path, concurrency=4,
                                  on_item=lambda i, p: reported.append((i.id, p)))
    await asyncio.sleep(0.3)
    assert list(tmp_path.iterdir()) == []  # nothing written after the error
    assert reported == [("AF1QipVideo2", None)]


async def test_async_download_to_file_object(tmp_path) -> None:
    import io
    async with AsyncPickerClient("tok", http=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"X")))) as cl:
        buf = io.BytesIO()
        assert await cl.download(PickedMediaItem.from_api(PHOTO), buf) is None
        assert buf.getvalue() == b"X"
        out = await cl.download(PickedMediaItem.from_api(PHOTO), Path(tmp_path, "a.jpg"))
        assert out and out.read_bytes() == b"X"
