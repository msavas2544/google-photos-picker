from __future__ import annotations

import io
import json
import uuid
from typing import Any, Dict, List

import httpx
import pytest

from google_photos_picker import (
    AsyncPickerClient,
    FailedPrecondition,
    InvalidArgument,
    NotFound,
    OAuthCredentials,
    PickedMediaItem,
    PickerClient,
    PickerTimeout,
    PickingSession,
    RateLimited,
    StaticToken,
    VideoNotReady,
    media_url,
)
from google_photos_picker.cli import build_parser

PHOTO = {
    "id": "AF1QipPhoto1",
    "createTime": "2026-07-14T09:31:02.123456789Z",
    "type": "PHOTO",
    "mediaFile": {
        "baseUrl": "https://lh3.googleusercontent.com/ppa/photo1",
        "mimeType": "image/jpeg",
        "filename": "IMG_0001.JPG",
        "mediaFileMetadata": {"width": 4032, "height": 3024, "cameraMake": "Google",
                              "cameraModel": "Pixel 9",
                              "photoMetadata": {"focalLength": 6.9, "apertureFNumber": 1.7,
                                                "isoEquivalent": 50,
                                                "exposureTime": "0.008s"}},
    },
}
VIDEO = {
    "id": "AF1QipVideo1",
    "type": "VIDEO",
    "mediaFile": {
        "baseUrl": "https://lh3.googleusercontent.com/ppa/video1",
        "mimeType": "video/quicktime",
        "filename": "clip.MOV",
        "mediaFileMetadata": {"width": 1920, "height": 1080,
                              "videoMetadata": {"fps": 30, "processingStatus": "READY"}},
    },
}
PROCESSING = json.loads(json.dumps(VIDEO))
PROCESSING["id"] = "AF1QipVideo2"
PROCESSING["mediaFile"]["filename"] = "IMG_0001.JPG"  # collide on purpose
PROCESSING["mediaFile"]["mediaFileMetadata"]["videoMetadata"]["processingStatus"] = "PROCESSING"


def session_json(set_: bool = False, poll: str = "2s", timeout_in: str = "600s") -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": "sess-123", "pickerUri": "https://photos.google.com/picker/abc",
                         "expireTime": "2026-10-29T12:00:00Z", "mediaItemsSet": set_,
                         "pickingConfig": {"maxItemCount": "20"}}
    if not set_:
        d["pollingConfig"] = {"pollInterval": poll, "timeoutIn": timeout_in}
    return d


class Fake:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.polls = 0
        self.ready_after = 2
        self.deleted: List[str] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        assert r.headers["Authorization"].startswith("Bearer ")
        path = r.url.path
        if r.url.host == "lh3.googleusercontent.com":
            if path.endswith("=d") or path.endswith("photo1=w100-h100-c"):
                return httpx.Response(200, content=b"JPEGDATA",
                                      headers={"content-type": "image/jpeg"})
            if path.endswith("=dv"):
                return httpx.Response(200, content=b"MP4DATA",
                                      headers={"content-type": "video/mp4"})
            return httpx.Response(200, content=b"THUMB", headers={"content-type": "image/jpeg"})
        if r.method == "POST" and path == "/v1/sessions":
            return httpx.Response(200, json=session_json())
        if r.method == "GET" and path == "/v1/sessions/sess-123":
            self.polls += 1
            return httpx.Response(200, json=session_json(self.polls >= self.ready_after))
        if r.method == "DELETE" and path.startswith("/v1/sessions/"):
            sid = path.rsplit("/", 1)[1]
            if sid in self.deleted:
                return httpx.Response(404, json={"error": {"status": "NOT_FOUND",
                                                           "message": "gone"}})
            self.deleted.append(sid)
            return httpx.Response(200, json={})
        if r.method == "GET" and path == "/v1/mediaItems":
            if self.polls < self.ready_after:
                return httpx.Response(400, json={"error": {"status": "FAILED_PRECONDITION",
                                                           "message": "not done"}})
            if r.url.params.get("pageToken") == "p2":
                return httpx.Response(200, json={"mediaItems": [VIDEO, PROCESSING]})
            return httpx.Response(200, json={"mediaItems": [PHOTO], "nextPageToken": "p2"})
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "message": path}})


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: List[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(fake: Fake, clock: Clock) -> PickerClient:
    return PickerClient(StaticToken("tok"), http=httpx.Client(transport=httpx.MockTransport(fake)),
                        sleep=clock.sleep, clock=clock)


# ---------------------------------------------------------------------------- models
def test_models_parse() -> None:
    s = PickingSession.from_api(session_json())
    assert s.polling and s.polling.poll_interval == 2.0 and s.polling.timeout_in == 600.0
    assert s.max_item_count == 20 and not s.media_items_set
    assert s.picker_uri_autoclose == "https://photos.google.com/picker/abc/autoclose"
    p = PickedMediaItem.from_api(PHOTO)
    assert p.is_photo and p.ready and p.width == 4032 and p.camera_model == "Pixel 9"
    assert p.photo and p.photo.exposure_time == 0.008 and p.photo.iso_equivalent == 50
    assert p.create_time and p.create_time.microsecond == 123456
    v = PickedMediaItem.from_api(PROCESSING)
    assert v.is_video and not v.ready and v.video and v.video.fps == 30


def test_media_url() -> None:
    p, v = PickedMediaItem.from_api(PHOTO), PickedMediaItem.from_api(VIDEO)
    assert media_url(p).endswith("photo1=d")
    assert media_url(v).endswith("video1=dv")
    assert media_url(p, width=2048, height=1024).endswith("=w2048-h1024")
    assert media_url(v, width=320, height=180, crop=True, video_overlay=False).endswith(
        "=w320-h180-c-no")
    with pytest.raises(ValueError):
        media_url(p, width=100)
    with pytest.raises(ValueError):
        media_url(p, crop=True)
    with pytest.raises(ValueError):
        media_url(p, width=0, height=10)


# ---------------------------------------------------------------------------- sessions
def test_create_session_body_and_request_id(fake, client) -> None:
    client.create_session(max_items=5000)
    assert json.loads(fake.requests[0].content) == {"pickingConfig": {"maxItemCount": "2000"}}
    rid = str(uuid.uuid4())
    client.create_session(request_id=rid)
    assert fake.requests[1].url.params["requestId"] == rid
    assert json.loads(fake.requests[1].content) == {}
    with pytest.raises(ValueError):
        client.create_session(request_id="not-a-uuid")
    with pytest.raises(ValueError):
        client.create_session(max_items=0)


def test_wait_honours_poll_interval(fake, client, clock) -> None:
    fake.ready_after = 3
    s = client.create_session()
    seen = []
    done = client.wait(s, on_poll=lambda x: seen.append(x.media_items_set))
    assert done.media_items_set
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert seen == [False, False, True]


def test_wait_timeout(fake, client, clock) -> None:
    fake.ready_after = 1000
    with pytest.raises(PickerTimeout):
        client.wait("sess-123", timeout=5)
    assert sum(clock.sleeps) == pytest.approx(5.0)


def test_wait_server_timeout() -> None:
    def handler(r):
        return httpx.Response(200, json=session_json(timeout_in="0s"))
    c = PickerClient("tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PickerTimeout):
        c.wait("sessions/sess-123")


def test_list_before_done_raises(fake, client) -> None:
    with pytest.raises(FailedPrecondition):
        list(client.list_items("sess-123"))


def test_full_flow_downloads_and_cleans_up(fake, client, tmp_path) -> None:
    (tmp_path / "IMG_0001.JPG").write_bytes(b"existing")
    shown = []
    skipped = []
    paths = client.pick(tmp_path, max_items=20, show=shown.append,
                        on_item=lambda i, p: p is None and skipped.append(i.id))
    assert shown[0].picker_uri.startswith("https://photos.google.com/")
    names = sorted(p.name for p in paths)
    assert names == ["IMG_0001 (1).JPG", "clip.mp4"]
    assert (tmp_path / "IMG_0001 (1).JPG").read_bytes() == b"JPEGDATA"
    assert (tmp_path / "clip.mp4").read_bytes() == b"MP4DATA"
    assert (tmp_path / "IMG_0001.JPG").read_bytes() == b"existing"
    assert skipped == ["AF1QipVideo2"]
    assert fake.deleted == ["sess-123"]
    assert not list(tmp_path.glob("*.part"))
    page2 = [r for r in fake.requests if r.url.path == "/v1/mediaItems"][-1]
    assert page2.url.params["pageToken"] == "p2" and page2.url.params["pageSize"] == "100"


def test_session_context_deletes_on_error(fake, client) -> None:
    with pytest.raises(RuntimeError):
        with client.session():
            raise RuntimeError("boom")
    assert fake.deleted == ["sess-123"]
    client.delete_session("sess-123")  # already gone → ignored
    with pytest.raises(NotFound):
        client.delete_session("sess-123", missing_ok=False)


def test_download_variants(fake, client, tmp_path) -> None:
    v = PickedMediaItem.from_api(PROCESSING)
    with pytest.raises(VideoNotReady):
        client.read(v)
    assert client.read(v, width=100, height=100) == b"THUMB"  # thumbnails are fine
    p = PickedMediaItem.from_api(PHOTO)
    assert client.read(p, width=100, height=100, crop=True) == b"JPEGDATA"
    buf = io.BytesIO()
    assert client.download(p, buf) is None and buf.getvalue() == b"JPEGDATA"
    out = client.download(p, tmp_path / "sub" / "x.jpg")
    assert out == tmp_path / "sub" / "x.jpg" and out.read_bytes() == b"JPEGDATA"
    thumb = client.download(PickedMediaItem.from_api(VIDEO), tmp_path, width=64, height=64)
    assert thumb is not None and thumb.name == "clip.jpg"


# ---------------------------------------------------------------------------- errors
def test_retry_and_errors(clock) -> None:
    calls = []

    def handler(r):
        calls.append(r)
        if len(calls) < 3:
            return httpx.Response(503)
        if r.method == "POST":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED",
                                                       "message": "too many sessions"}})
        return httpx.Response(200, json=session_json())

    c = PickerClient("tok", http=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleep=clock.sleep)
    assert c.get_session("sess-123").id == "sess-123"
    assert len(calls) == 3
    with pytest.raises(RateLimited):
        c.create_session()

    bad = PickerClient("tok", http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT",
                                                      "message": "pageSize"}}))))
    with pytest.raises(InvalidArgument):
        bad.get_session("x")
    with pytest.raises(ValueError):
        list(bad.list_items("x", page_size=500))
    with pytest.raises(ValueError):
        bad.get_session("a/b")


def test_download_refreshes_on_401(tmp_path) -> None:
    import time
    seen = []

    def handler(r):
        if r.url.path == "/token":
            return httpx.Response(200, json={"access_token": f"t{len(seen)}", "expires_in": 3600})
        seen.append(r.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401)
        return httpx.Response(200, content=b"OK", headers={"content-type": "image/jpeg"})

    creds = OAuthCredentials(access_token="old", refresh_token="r", client_id="c",
                             expires_at=time.time() + 3600)
    c = PickerClient(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.read(PickedMediaItem.from_api(PHOTO)) == b"OK"
    assert seen == ["Bearer old", "Bearer t1"]


# ---------------------------------------------------------------------------- async
async def test_async_flow(fake, tmp_path) -> None:
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    async with AsyncPickerClient(
            "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
            sleep=sleep) as c:
        async with c.session(max_items=3) as s:
            s = await c.wait(s)
            items = await c.collect_items(s)
            assert [i.id for i in items] == ["AF1QipPhoto1", "AF1QipVideo1", "AF1QipVideo2"]
            paths = await c.download_all(items, tmp_path, concurrency=2)
        assert sorted(p.name for p in paths) == ["IMG_0001.JPG", "clip.mp4"]
        assert fake.deleted == ["sess-123"] and sleeps == [2.0, 2.0]
        with pytest.raises(VideoNotReady):
            await c.read(items[2])


def test_cli_parser() -> None:
    a = build_parser().parse_args(["pick", "--out", "x", "--max-items", "5", "--qr"])
    assert a.out == "x" and a.max_items == 5 and a.qr
