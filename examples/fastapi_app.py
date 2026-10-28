"""Complete "Import from Google Photos" demo (single user, for local testing).

    pip install fastapi uvicorn google-photos-picker
    google-photos-picker login            # creates the token file used below
    uvicorn fastapi_app:app --reload      # open http://127.0.0.1:8000
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from google_photos_picker import AsyncPickerClient, OAuthCredentials
from google_photos_picker.cli import DEFAULT_TOKEN

app = FastAPI()
creds = OAuthCredentials.load(DEFAULT_TOKEN)
OUT = Path("imported")
state: dict = {}
lock = asyncio.Lock()  # one finisher only, even if two /status polls race

PAGE = """<!doctype html><meta charset=utf-8><title>Import from Google Photos</title>
<button id=go>Import from Google Photos</button><p id=msg></p>
<script>
go.onclick = async () => {
  const w = window.open("about:blank");                // open synchronously (popup blockers)
  const r = await (await fetch("/start", {method: "POST"})).json();
  w.location = r.pickerUri;
  msg.textContent = "Waiting for you to press Done…";
  const poll = async () => {
    const s = await (await fetch("/status")).json();
    if (s.done) { msg.textContent = `Imported ${s.count} file(s).`; return; }
    if (s.expired) { msg.textContent = "Timed out – click the button to try again."; return; }
    setTimeout(poll, Math.max(1, s.pollAfter || 5) * 1000);
  };
  poll();
};
</script>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.post("/start")
async def start() -> dict:
    state.pop("result", None)
    async with AsyncPickerClient(creds) as c:
        if "session" in state:
            await c.delete_session(state.pop("session"))
        s = await c.create_session(max_items=100)
    state["session"] = s.id
    return {"pickerUri": s.picker_uri_autoclose}


@app.get("/status")
async def status() -> dict:
    async with lock, AsyncPickerClient(creds) as c:
        if "result" in state:
            return {"done": True, "count": state["result"]}
        if "session" not in state:
            return {"done": False, "expired": True}
        s = await c.get_session(state["session"])
        if not s.media_items_set:
            if s.polling and s.polling.timeout_in == 0:
                await c.delete_session(state.pop("session"))
                return {"done": False, "expired": True}
            return {"done": False,
                    "pollAfter": (s.polling.poll_interval if s.polling else None) or 5}
        paths = await c.download_all(c.list_items(s), OUT, concurrency=6)
        await c.delete_session(state.pop("session"))
        state["result"] = len(paths)
        return {"done": True, "count": len(paths)}
