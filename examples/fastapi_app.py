"""Complete "Import from Google Photos" demo (single user, for local testing).

    pip install fastapi uvicorn google-photos-picker
    google-photos-picker login            # creates the token file used below
    uvicorn fastapi_app:app --reload      # open http://127.0.0.1:8000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from google_photos_picker import AsyncPickerClient, OAuthCredentials
from google_photos_picker.cli import DEFAULT_TOKEN

app = FastAPI()
creds = OAuthCredentials.load(DEFAULT_TOKEN)
OUT = Path("imported")
state: dict = {}

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
    setTimeout(poll, s.pollAfter * 1000);
  };
  poll();
};
</script>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.post("/start")
async def start() -> dict:
    async with AsyncPickerClient(creds) as c:
        if "session" in state:
            await c.delete_session(state.pop("session"))
        s = await c.create_session(max_items=100)
    state["session"] = s.id
    return {"pickerUri": s.picker_uri_autoclose}


@app.get("/status")
async def status() -> dict:
    async with AsyncPickerClient(creds) as c:
        s = await c.get_session(state["session"])
        if not s.media_items_set:
            return {"done": False, "pollAfter": s.polling.poll_interval if s.polling else 5}
        paths = await c.download_all(c.list_items(s), OUT, concurrency=6)
        await c.delete_session(s)
        state.pop("session", None)
        return {"done": True, "count": len(paths)}
