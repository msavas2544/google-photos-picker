"""Synchronous client for the Google Photos Picker API."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import (
    IO,
    Any,
    Callable,
    Iterator,
    List,
    Optional,
    Set,
    Union,
)

import httpx

from . import _common as c
from .auth import Credentials, StaticToken
from .errors import NotFound, PickerTimeout, VideoNotReady
from .models import PickedMediaItem, PickingSession, media_url

SessionLike = Union[PickingSession, str]
Dest = Union[str, "os.PathLike[str]", IO[bytes]]


class PickerClient:
    """Blocking Google Photos Picker client.

    >>> client = PickerClient(OAuthCredentials.load("token.json"))
    >>> with client.session(max_items=20) as s:
    ...     print("Pick photos here:", s.picker_uri)
    ...     s = client.wait(s)
    ...     paths = client.download_all(client.list_items(s), "photos/")
    """

    def __init__(
        self,
        credentials: Union[Credentials, str],
        *,
        http: Optional[httpx.Client] = None,
        base_url: str = c.BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = StaticToken(credentials) if isinstance(credentials, str) else credentials
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=timeout)
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        if self._own_http:
            self.http.close()

    def __enter__(self) -> PickerClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport -----------------------------------------------------------------------
    def _request(self, call: c.Call) -> Any:
        attempt, reauthed = 0, False
        while True:
            token = self.credentials.token(self.http)
            try:
                resp = self.http.request(
                    call.method, self.base_url + call.path, params=call.params, json=call.json,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": c.USER_AGENT})
            except httpx.TransportError:
                # Network blips must not abort a long wait() (and delete the session).
                if not call.idempotent or attempt >= self.max_retries:
                    raise
                self._sleep(c.backoff(attempt, self.backoff, None))
                attempt += 1
                continue
            if resp.status_code == 401 and not reauthed:
                reauthed = True
                self.credentials.invalidate(token)
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            if c.should_retry(call, resp.status_code, attempt, self.max_retries):
                self._sleep(c.backoff(attempt, self.backoff, resp.headers.get("Retry-After")))
                attempt += 1
                continue
            raise c.error(resp)

    # -- sessions ------------------------------------------------------------------------
    def create_session(self, *, max_items: Optional[int] = None,
                       request_id: Optional[str] = None) -> PickingSession:
        """Start a picking session. Send the user to ``session.picker_uri``
        (or ``picker_uri_autoclose`` for web apps; it cannot be iframed)."""
        return PickingSession.from_api(self._request(c.create_call(max_items, request_id)))

    def get_session(self, session: SessionLike) -> PickingSession:
        return PickingSession.from_api(
            self._request(c.Call("GET", f"sessions/{c.session_id(session)}")))

    def delete_session(self, session: SessionLike, *, missing_ok: bool = True) -> None:
        """Delete a session (do this once you've downloaded the bytes – sessions are
        rate limited per user)."""
        try:
            self._request(c.Call("DELETE", f"sessions/{c.session_id(session)}"))
        except NotFound:
            if not missing_ok:
                raise

    @contextlib.contextmanager
    def session(self, *, max_items: Optional[int] = None,
                request_id: Optional[str] = None) -> Iterator[PickingSession]:
        """Context manager that always deletes the session on exit."""
        s = self.create_session(max_items=max_items, request_id=request_id)
        try:
            yield s
        finally:
            try:
                self.delete_session(s)
            except Exception:  # never mask the original error
                pass

    def wait(
        self,
        session: SessionLike,
        *,
        timeout: Optional[float] = None,
        min_interval: float = 1.0,
        on_poll: Optional[Callable[[PickingSession], None]] = None,
    ) -> PickingSession:
        """Poll until the user finishes picking, honouring the server's
        ``pollInterval`` and ``timeoutIn``. Raises :class:`PickerTimeout`."""
        deadline = None if timeout is None else self._clock() + timeout
        s = session if isinstance(session, PickingSession) else self.get_session(session)
        while True:
            if s.media_items_set:
                return s
            if c.polling_expired(s):
                raise PickerTimeout(s.id)
            delay = c.next_poll_delay(s, min_interval)
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise PickerTimeout(s.id)
                delay = min(delay, remaining)
            self._sleep(delay)
            s = self.get_session(s)
            if on_poll:
                on_poll(s)

    # -- media items ---------------------------------------------------------------------
    def list_items(self, session: SessionLike, *, page_size: int = 100
                   ) -> Iterator[PickedMediaItem]:
        """Iterate the user's picks (raises FailedPrecondition before picking is done)."""
        sid = c.session_id(session)
        token: Optional[str] = None
        while True:
            page = self._request(c.list_call(sid, page_size, token))
            for raw in page.get("mediaItems") or []:
                yield PickedMediaItem.from_api(raw)
            token = page.get("nextPageToken")
            if not token:
                return

    def open(self, item: PickedMediaItem, *, width: Optional[int] = None,
             height: Optional[int] = None, crop: bool = False, video_overlay: bool = True,
             allow_unready: bool = False) -> contextlib.AbstractContextManager[httpx.Response]:
        """Stream a media item's bytes: ``with client.open(item) as r: r.iter_bytes()``."""
        if width is None and item.is_video and not item.ready and not allow_unready:
            raise VideoNotReady(item.id, item.video.processing_status if item.video else None)
        url = media_url(item, width=width, height=height, crop=crop, video_overlay=video_overlay)
        return self._stream(url)

    @contextlib.contextmanager
    def _stream(self, url: str) -> Iterator[httpx.Response]:
        attempt, reauthed = 0, False
        while True:
            token = self.credentials.token(self.http)
            cm = self.http.stream("GET", url, follow_redirects=True,
                                  headers={"Authorization": f"Bearer {token}",
                                           "User-Agent": c.USER_AGENT})
            try:
                resp = cm.__enter__()
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                self._sleep(c.backoff(attempt, self.backoff, None))
                attempt += 1
                continue
            try:
                if resp.status_code == 401 and not reauthed:
                    reauthed = True
                    self.credentials.invalidate(token)
                    continue
                if resp.status_code >= 400:
                    if resp.status_code in c.RETRY_STATUSES and attempt < self.max_retries:
                        retry_after = resp.headers.get("Retry-After")
                        attempt += 1
                        self._sleep(c.backoff(attempt - 1, self.backoff, retry_after))
                        continue
                    resp.read()
                    raise c.error(resp)
                yield resp
                return
            finally:
                cm.__exit__(None, None, None)

    def read(self, item: PickedMediaItem, **kwargs: Any) -> bytes:
        """Download a media item into memory (see :func:`media_url` for size options)."""
        with self.open(item, **kwargs) as r:
            return r.read()

    def download(self, item: PickedMediaItem, dest: Dest, **kwargs: Any) -> Optional[Path]:
        """Download to a directory (safe, unique filename), a file path, or a binary
        file object. Returns the written path (``None`` for file objects)."""
        return self._download(item, dest, set(), **kwargs)

    def _download(self, item: PickedMediaItem, dest: Dest, taken: Set[str],
                  **kwargs: Any) -> Optional[Path]:
        with self.open(item, **kwargs) as r:
            if hasattr(dest, "write"):
                for chunk in r.iter_bytes():
                    dest.write(chunk)  # type: ignore[union-attr]
                return None
            p = Path(dest)  # type: ignore[arg-type]
            if c.is_directory_target(dest):
                p.mkdir(parents=True, exist_ok=True)
                name = c.fix_extension(c.safe_filename(item), r.headers.get("content-type"))
                p = c.unique_path(p, name, taken)
            p.parent.mkdir(parents=True, exist_ok=True)
            part = p.with_name(p.name + ".part")
            try:
                with open(part, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
                os.replace(part, p)
            finally:
                if part.exists():
                    part.unlink()
            return p

    def download_all(
        self,
        items: Union[Iterator[PickedMediaItem], List[PickedMediaItem]],
        directory: Union[str, os.PathLike[str]],
        *,
        skip_unready: bool = True,
        on_item: Optional[Callable[[PickedMediaItem, Optional[Path]], None]] = None,
        photo_size: Optional[int] = None,
        **kwargs: Any,
    ) -> List[Path]:
        """Download every item into ``directory``. Unready videos are skipped (reported
        to ``on_item`` with ``None``) unless ``skip_unready=False``. ``photo_size``
        resizes photos to fit N×N while videos are still downloaded in full."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        taken: Set[str] = set()
        out: List[Path] = []
        for item in items:
            if skip_unready and not item.ready and "width" not in kwargs:
                if on_item:
                    on_item(item, None)
                continue
            p = self._download(item, d, taken, **c.item_kwargs(item, photo_size, kwargs))
            assert p is not None
            out.append(p)
            if on_item:
                on_item(item, p)
        return out

    # -- one-shot ------------------------------------------------------------------------
    def pick(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        max_items: Optional[int] = None,
        show: Callable[[PickingSession], None] = lambda s: print(s.picker_uri),
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> List[Path]:
        """Create a session, show the link, wait, download everything, clean up."""
        with self.session(max_items=max_items) as s:
            show(s)
            s = self.wait(s, timeout=timeout)
            return self.download_all(self.list_items(s), directory, **kwargs)
