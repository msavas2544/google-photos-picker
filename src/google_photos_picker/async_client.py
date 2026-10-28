"""asyncio client for the Google Photos Picker API (same surface as :class:`PickerClient`)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import (
    IO,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
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


class AsyncPickerClient:
    """Non-blocking client – ideal inside web apps (FastAPI, aiohttp, Django async)."""

    def __init__(
        self,
        credentials: Union[Credentials, str],
        *,
        http: Optional[httpx.AsyncClient] = None,
        base_url: str = c.BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = StaticToken(credentials) if isinstance(credentials, str) else credentials
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self._clock = clock

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def __aenter__(self) -> AsyncPickerClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, call: c.Call) -> Any:
        attempt, reauthed = 0, False
        while True:
            token = await self.credentials.atoken(self.http)
            try:
                resp = await self.http.request(
                    call.method, self.base_url + call.path, params=call.params, json=call.json,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": c.USER_AGENT})
            except httpx.TransportError:
                if not call.idempotent or attempt >= self.max_retries:
                    raise
                await self._sleep(c.backoff(attempt, self.backoff, None))
                attempt += 1
                continue
            if resp.status_code == 401 and not reauthed:
                reauthed = True
                self.credentials.invalidate(token)
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            if c.should_retry(call, resp.status_code, attempt, self.max_retries):
                await self._sleep(c.backoff(attempt, self.backoff,
                                            resp.headers.get("Retry-After")))
                attempt += 1
                continue
            raise c.error(resp)

    async def create_session(self, *, max_items: Optional[int] = None,
                             request_id: Optional[str] = None) -> PickingSession:
        return PickingSession.from_api(
            await self._request(c.create_call(max_items, request_id)))

    async def get_session(self, session: SessionLike) -> PickingSession:
        return PickingSession.from_api(
            await self._request(c.Call("GET", f"sessions/{c.session_id(session)}")))

    async def delete_session(self, session: SessionLike, *, missing_ok: bool = True) -> None:
        try:
            await self._request(c.Call("DELETE", f"sessions/{c.session_id(session)}"))
        except NotFound:
            if not missing_ok:
                raise

    @contextlib.asynccontextmanager
    async def session(self, *, max_items: Optional[int] = None,
                      request_id: Optional[str] = None) -> AsyncIterator[PickingSession]:
        s = await self.create_session(max_items=max_items, request_id=request_id)
        try:
            yield s
        finally:
            try:
                await self.delete_session(s)
            except Exception:
                pass

    async def wait(
        self,
        session: SessionLike,
        *,
        timeout: Optional[float] = None,
        min_interval: float = 1.0,
        on_poll: Optional[Callable[[PickingSession], Any]] = None,
    ) -> PickingSession:
        deadline = None if timeout is None else self._clock() + timeout
        s = session if isinstance(session, PickingSession) else await self.get_session(session)
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
            await self._sleep(delay)
            s = await self.get_session(s)
            if on_poll:
                res = on_poll(s)
                if asyncio.iscoroutine(res):
                    await res

    async def list_items(self, session: SessionLike, *, page_size: int = 100
                         ) -> AsyncIterator[PickedMediaItem]:
        sid = c.session_id(session)
        token: Optional[str] = None
        while True:
            page = await self._request(c.list_call(sid, page_size, token))
            for raw in page.get("mediaItems") or []:
                yield PickedMediaItem.from_api(raw)
            token = page.get("nextPageToken")
            if not token:
                return

    async def collect_items(self, session: SessionLike) -> List[PickedMediaItem]:
        return [i async for i in self.list_items(session)]

    def open(self, item: PickedMediaItem, *, width: Optional[int] = None,
             height: Optional[int] = None, crop: bool = False, video_overlay: bool = True,
             allow_unready: bool = False
             ) -> contextlib.AbstractAsyncContextManager[httpx.Response]:
        if width is None and item.is_video and not item.ready and not allow_unready:
            raise VideoNotReady(item.id, item.video.processing_status if item.video else None)
        url = media_url(item, width=width, height=height, crop=crop, video_overlay=video_overlay)
        return self._stream(url)

    @contextlib.asynccontextmanager
    async def _stream(self, url: str) -> AsyncIterator[httpx.Response]:
        attempt, reauthed = 0, False
        while True:
            token = await self.credentials.atoken(self.http)
            cm = self.http.stream("GET", url, follow_redirects=True,
                                  headers={"Authorization": f"Bearer {token}",
                                           "User-Agent": c.USER_AGENT})
            try:
                resp = await cm.__aenter__()
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                await self._sleep(c.backoff(attempt, self.backoff, None))
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
                        await self._sleep(c.backoff(attempt - 1, self.backoff, retry_after))
                        continue
                    await resp.aread()
                    raise c.error(resp)
                yield resp
                return
            finally:
                await cm.__aexit__(None, None, None)

    async def read(self, item: PickedMediaItem, **kwargs: Any) -> bytes:
        async with self.open(item, **kwargs) as r:
            return await r.aread()

    async def download(self, item: PickedMediaItem, dest: Dest,
                       **kwargs: Any) -> Optional[Path]:
        """Download to a directory, a file path, or a binary file object."""
        return await self._download(item, dest, set(), **kwargs)

    async def _download(self, item: PickedMediaItem, dest: Dest,
                        taken: Set[str], **kwargs: Any) -> Optional[Path]:
        async with self.open(item, **kwargs) as r:
            if hasattr(dest, "write"):
                async for chunk in r.aiter_bytes():
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
                with open(part, "wb") as fh:  # small sync writes are fine for chunks
                    async for chunk in r.aiter_bytes():
                        fh.write(chunk)
                os.replace(part, p)
            finally:
                if part.exists():
                    part.unlink()
            return p

    async def download_all(
        self,
        items: Union[Iterable[PickedMediaItem], AsyncIterator[PickedMediaItem]],
        directory: Union[str, os.PathLike[str]],
        *,
        concurrency: int = 4,
        skip_unready: bool = True,
        on_item: Optional[Callable[[PickedMediaItem, Optional[Path]], Any]] = None,
        photo_size: Optional[int] = None,
        **kwargs: Any,
    ) -> List[Path]:
        """Download concurrently (``concurrency`` parallel transfers). Order of the
        returned paths follows the input order. If any download fails, the others are
        cancelled (and their partial files removed) before the error is raised."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        if hasattr(items, "__aiter__"):
            seq = [i async for i in items]  # type: ignore[union-attr]
        else:
            seq = list(items)  # type: ignore[arg-type]
        async def report(item: PickedMediaItem, path: Optional[Path]) -> None:
            if on_item:
                res = on_item(item, path)
                if asyncio.iscoroutine(res):
                    await res

        if skip_unready and "width" not in kwargs:
            for i in seq:
                if not i.ready:
                    await report(i, None)
            seq = [i for i in seq if i.ready]
        # Names are chosen and reserved synchronously (no await in between), so
        # concurrent transfers can never pick the same filename.
        taken: Set[str] = set()
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(item: PickedMediaItem) -> Path:
            async with sem:
                p = await self._download(item, d, taken,
                                         **c.item_kwargs(item, photo_size, kwargs))
            assert p is not None
            await report(item, p)
            return p

        tasks = [asyncio.ensure_future(one(i)) for i in seq]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def pick(
        self,
        directory: Union[str, os.PathLike[str]],
        *,
        max_items: Optional[int] = None,
        show: Callable[[PickingSession], Any] = lambda s: print(s.picker_uri),
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> List[Path]:
        async with self.session(max_items=max_items) as s:
            res = show(s)
            if asyncio.iscoroutine(res):
                await res
            s = await self.wait(s, timeout=timeout)
            return await self.download_all(self.list_items(s), directory, **kwargs)
