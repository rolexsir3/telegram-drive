"""Telegram Drive: use a private channel as storage (folders, photos, videos).
Folders = a caption tag "📁 path/to/folder" on each file (+ a text marker message for empty folders).
Env: API_ID, API_HASH, CHANNEL_ID (e.g. -1001234567890). Optional: APP_PASSWORD, HOST.
"""
import os, json, base64, hmac, mimetypes, shutil, tempfile, asyncio
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from telethon import TelegramClient

BASE = os.path.dirname(os.path.abspath(__file__))
THUMBS = os.path.join(BASE, "thumbs"); os.makedirs(THUMBS, exist_ok=True)
client = TelegramClient(os.path.join(BASE, "drive"), int(os.environ["API_ID"]), os.environ["API_HASH"])
CHANNEL = int(os.environ["CHANNEL_ID"])
PW = os.getenv("APP_PASSWORD")
TAG = "📁"
app = FastAPI()
cache = {"items": None}
sem = asyncio.Semaphore(4)
up_sem = asyncio.Semaphore(3)   # parallel uploads to Telegram
ent = None


@app.middleware("http")
async def auth(req: Request, call):
    if PW:
        h = req.headers.get("authorization", "")
        try:
            ok = h.startswith("Basic ") and hmac.compare_digest(
                base64.b64decode(h[6:]).decode().split(":", 1)[-1], PW)
        except Exception:
            ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="drive"'})
    return await call(req)


@app.on_event("startup")
async def start():
    global ent
    await client.start()          # first run: asks for phone + login code in the terminal
    await client.get_dialogs()
    ent = await client.get_entity(CHANNEL)


def folder_of(m):
    t = (m.message or "").strip()
    return t[len(TAG):].strip("/ ") if t.startswith(TAG) else ""


def kind_of(m):
    mime = (m.file.mime_type if m.file else "") or ""
    if m.photo or mime.startswith("image/"): return "photo"
    if m.video or mime.startswith("video/"): return "video"
    return "file"


@app.get("/api/items")
async def items():
    if cache["items"] is None:
        out = []
        async for m in client.iter_messages(ent):
            f = folder_of(m)
            if m.media and m.file:
                out.append(dict(id=m.id, folder=f, kind=kind_of(m), name=m.file.name or f"{m.id}{m.file.ext or ''}",
                                size=m.file.size, w=m.file.width or 0, h=m.file.height or 0, date=m.date.isoformat()))
            elif f:
                out.append(dict(id=m.id, folder=f, kind="folder", date=m.date.isoformat()))
        cache["items"] = out
    return cache["items"]


@app.post("/api/folder")
async def new_folder(d: dict):
    await client.send_message(ent, f"{TAG} {d['name'].strip('/ ')}")
    cache["items"] = None
    return {"ok": True}


@app.post("/api/upload")
async def upload(folder: str = Form(""), original: bool = Form(True), files: list[UploadFile] = File(...)):
    folder = folder.strip("/ ")
    for f in files:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, os.path.basename(f.filename))   # keeps the real filename in Telegram
        with open(path, "wb") as o:
            while chunk := await f.read(1024 * 1024):             # async copy, does not block other uploads
                o.write(chunk)
        mime = mimetypes.guess_type(path)[0] or ""
        try:
            async with up_sem:
                msg = await client.send_file(ent, path, caption=f"{TAG} {folder}".strip(),
                                             force_document=original and mime.startswith("image/"),  # photos keep full quality
                                             supports_streaming=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if cache["items"] is not None and getattr(msg, "file", None):   # update list without rescanning the channel
            cache["items"].insert(0, dict(id=msg.id, folder=folder, kind=kind_of(msg),
                                          name=msg.file.name or f"{msg.id}{msg.file.ext or ''}",
                                          size=msg.file.size, w=msg.file.width or 0, h=msg.file.height or 0, date=msg.date.isoformat()))
    return {"ok": True}


@app.get("/api/thumb/{mid}")
async def thumb(mid: int):
    p = os.path.join(THUMBS, f"{mid}.jpg")
    if not os.path.exists(p):
        async with sem:
            m = await client.get_messages(ent, ids=mid)
            b = await client.download_media(m, bytes, thumb=-1) if m else None
        if not b:
            raise HTTPException(404)
        open(p, "wb").write(b)
    return FileResponse(p, headers={"Cache-Control": "max-age=31536000"})


@app.get("/api/file/{mid}")
async def file(mid: int, request: Request):
    m = await client.get_messages(ent, ids=mid)
    if not m or not m.file:
        raise HTTPException(404)
    size = m.file.size
    start, end, status = 0, size - 1, 200
    r = request.headers.get("range")
    if r:                                           # Range support = video seeking works
        a, b = r.replace("bytes=", "").split("-")
        start, end, status = int(a or 0), int(b) if b else size - 1, 206

    async def gen():
        left = end - start + 1
        async for ch in client.iter_download(m.media, offset=start, request_size=512 * 1024):
            ch = ch[:left]; left -= len(ch)
            yield ch
            if left <= 0: break

    hdr = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
           "Content-Range": f"bytes {start}-{end}/{size}"}
    return StreamingResponse(gen(), status_code=status, headers=hdr,
                             media_type=m.file.mime_type or "application/octet-stream")


@app.post("/api/move")
async def move(d: dict):
    for mid in d["ids"]:
        m = await client.get_messages(ent, ids=mid)
        await client.edit_message(ent, m, f"{TAG} {d['folder'].strip('/ ')}".strip())
    cache["items"] = None
    return {"ok": True}


@app.post("/api/delete")
async def delete(d: dict):
    await client.delete_messages(ent, d["ids"])
    cache["items"] = None
    return {"ok": True}


COLORS = os.path.join(BASE, "colors.json")


@app.get("/api/colors")
async def colors_get():
    return json.load(open(COLORS)) if os.path.exists(COLORS) else {}


@app.post("/api/colors")
async def colors_set(d: dict):
    c = await colors_get()
    if d.get("color"): c[d["folder"]] = d["color"]
    else: c.pop(d["folder"], None)
    json.dump(c, open(COLORS, "w"))
    return c


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 8000)))
