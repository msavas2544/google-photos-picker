# google-photos-picker

A Python client for the **Google Photos Picker API**, the supported way for apps to let users choose photos and videos from their Google Photos library.

> On 31 March 2025 Google removed the Library API scopes that could read a user's whole library (`photoslibrary.readonly`, `photoslibrary`, `photoslibrary.sharing`). Apps that imported photos with those scopes must move to the Picker API. Google publishes no Python client for it.

The flow looks simple, but you have to get these steps right:

1. create a session
2. send the user to `pickerUri`
3. poll at the server-recommended interval until `mediaItemsSet`, respecting `timeoutIn`
4. page through the picked items
5. download each one with an OAuth header and the right `baseUrl` suffix (`=d`, `=dv`, `=wN-hN`...) before the URLs expire after about 60 minutes
6. skip videos that are still processing
7. delete the session so you don't hit per-user limits

This library does all of that:

* **Sync and async clients** that share one API surface (`PickerClient`, `AsyncPickerClient`) and run on Python 3.9+.
* **`wait()`** follows `pollInterval` and `timeoutIn` exactly and raises `PickerTimeout`.
* **`session()` context manager**: sessions are always deleted, even if your code raises.
* **Downloads**: streamed to a file, a directory or a file object. Filenames are safe and unique, partial files never linger (`.part` and then an atomic rename), the extension is corrected from `Content-Type`, thumbnails use resize and crop options, and unready videos are detected.
* **Concurrent async downloads** via `download_all(..., concurrency=4)`.
* **Typed models** (`PickingSession`, `PickedMediaItem` with photo and video metadata), plus OAuth 2.0 with PKCE and refresh, and retries with backoff.
* **A CLI**: `google-photos-picker pick --out ./photos --qr`. Scan the QR code on your phone, pick, and the files appear on disk.

## Install

```bash
pip install google-photos-picker
pip install "google-photos-picker[qr]"   # terminal QR codes for phone hand-off
```

## CLI

```bash
export GOOGLE_PHOTOS_PICKER_CLIENT_ID=...apps.googleusercontent.com
export GOOGLE_PHOTOS_PICKER_CLIENT_SECRET=...
google-photos-picker login
google-photos-picker pick --out ./holiday --max-items 50 --qr
google-photos-picker pick --out ./thumbs --max-size 1024     # resized copies
```

## Library

```python
from google_photos_picker import PickerClient, OAuthCredentials

client = PickerClient(OAuthCredentials.load("token.json"))

with client.session(max_items=20) as session:           # deleted automatically
    print("Pick here:", session.picker_uri)              # or session.picker_uri_autoclose
    session = client.wait(session, timeout=600)
    for item in client.list_items(session):
        print(item.type, item.filename, item.width, item.height, item.create_time)
    paths = client.download_all(client.list_items(session), "imports/")
```

Or as one call:

```python
paths = client.pick("imports/", max_items=20, show=lambda s: send_link_to_user(s.picker_uri))
```

### Media options

```python
client.read(item)                                  # original bytes (=d / =dv)
client.read(item, width=512, height=512, crop=True) # square thumbnail (=w512-h512-c)
client.download(item, "out/", width=2048, height=2048)
with client.open(item) as r:                        # stream it yourself
    for chunk in r.iter_bytes(): ...
media_url(item, width=320, height=180, video_overlay=False)  # needs an auth header to fetch!
```

Videos whose `processingStatus` isn't `READY` raise `VideoNotReady` when you ask for the original. Thumbnails still work. `download_all` skips them by default and reports them through `on_item(item, None)`.

### Web apps (async)

```python
from google_photos_picker import AsyncPickerClient

@app.post("/import/start")
async def start(user=Depends(current_user)):
    async with AsyncPickerClient(user.google_creds) as c:
        s = await c.create_session(max_items=100)
    await db.save_picker_session(user.id, s.id)
    return {"pickerUri": s.picker_uri_autoclose}      # open in a new tab (cannot be iframed)

@app.get("/import/status")
async def status(user=Depends(current_user)):
    async with AsyncPickerClient(user.google_creds) as c:
        s = await c.get_session(await db.picker_session(user.id))
        if not s.media_items_set:
            return {"done": False, "pollAfter": s.polling.poll_interval if s.polling else 5}
        paths = await c.download_all(c.list_items(s), f"uploads/{user.id}", concurrency=6)
        await c.delete_session(s)
        return {"done": True, "count": len(paths)}
```

[`examples/fastapi_app.py`](examples/fastapi_app.py) is a complete runnable version with the browser side included.

## Notes

* Scope: `https://www.googleapis.com/auth/photospicker.mediaitems.readonly` (exported as `SCOPE`).
* `pickerUri` stops working once the user presses Done. Create a new session to let them pick more.
* `PickedMediaItem.id` is stable across sessions, so use it to de-duplicate repeated imports.
* For TVs and other limited-input devices, pass `request_id=str(uuid.uuid4())` to `create_session` to get the streamlined device flow.

## Status

Version 0.1, alpha. Built from the official REST reference, covered by 16 unit tests on Python 3.9–3.13, which run against mocked HTTP. This is a community project, not affiliated with or endorsed by Google.

## License

Apache-2.0
