"""Telegram Drive: use a private channel as storage (folders, photos, videos).
Folders = a caption tag "📁 path/to/folder" on each file (+ a text marker message for empty folders).
Env: API_ID, API_HASH, CHANNEL_ID (e.g. -1001234567890). Optional: APP_PASSWORD, HOST.
"""
import os, re, json, zipfile, requests, base64, hmac, mimetypes, shutil, tempfile, asyncio
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from urllib.parse import quote
from telethon import TelegramClient, events

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
    client.add_event_handler(on_new, events.NewMessage(chats=ent))
    if os.getenv("BOT_TOKEN") and os.getenv("OWNER_ID"):        # fast mode: save what you send the bot via your own account
        try:
            bot_ent = await client.get_entity(int(os.environ["BOT_TOKEN"].split(":")[0]))
            client.add_event_handler(on_bot_media, events.NewMessage(chats=bot_ent, outgoing=True))
            asyncio.create_task(fast_worker())
            print("fast upload mode ready", flush=True)
        except Exception as e:
            print("fast mode unavailable:", e, flush=True)


def folder_of(m):
    t = (m.message or "").strip()
    return t[len(TAG):].strip("/ ") if t.startswith(TAG) else ""


def kind_of(m):
    mime = (m.file.mime_type if m.file else "") or ""
    if m.photo or mime.startswith("image/"): return "photo"
    if m.video or mime.startswith("video/"): return "video"
    return "file"


def to_item(m):
    f = folder_of(m)
    if m.media and m.file:
        return dict(id=m.id, folder=f, kind=kind_of(m), name=m.file.name or f"{m.id}{m.file.ext or ''}",
                    size=m.file.size, w=m.file.width or 0, h=m.file.height or 0, date=m.date.isoformat())
    if f:
        return dict(id=m.id, folder=f, kind="folder", date=m.date.isoformat())


async def on_new(ev):                    # posts made by the upload bot show up without a rescan
    it = to_item(ev.message)
    if it and cache["items"] is not None and not any(x["id"] == it["id"] for x in cache["items"]):
        cache["items"].insert(0, it)


BSTATE = os.path.join(BASE, "bot_state.json")
fq = asyncio.Queue()


def bstate():
    try:
        return json.load(open(BSTATE))
    except Exception:
        return {}


async def bot_say(text):
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage"
    await asyncio.to_thread(requests.post, url, json={"chat_id": int(os.environ["OWNER_ID"]), "text": text}, timeout=30)


async def on_bot_media(ev):              # files you send or forward to the bot
    m = ev.message
    if m.media and m.file and bstate().get("fast", True):
        await fq.put(m)


def dest_for(m, s):
    f, mime = s.get("folder", ""), m.file.mime_type or ""
    if s.get("split", True):
        sub = "Videos" if m.video or mime.startswith("video/") else "Photos" if m.photo or mime.startswith("image/") else "Files"
        f = (f + "/" if f else "") + sub
    return f


async def fast_worker():
    """Albums of 10 per request through your own account: far above the bot's ~20 posts/minute limit."""
    while True:
        batch = [await fq.get()]
        await asyncio.sleep(1.5)         # let a whole album or forward batch arrive
        while not fq.empty() and len(batch) < 200:
            batch.append(fq.get_nowait())
        s, ok, bad, dests = bstate(), 0, 0, set()
        for key in ("media", "doc"):     # photos+videos can share an album, plain files cannot mix with them
            grp = [m for m in batch if ("media" if m.photo or m.video else "doc") == key]
            for i in range(0, len(grp), 10):
                chunk = grp[i:i + 10]
                caps = []
                for m in chunk:
                    d = dest_for(m, s)
                    dests.add(d or "Home")
                    caps.append(f"{TAG} {d}".strip())
                try:
                    if len(chunk) == 1:
                        await client.send_file(ent, chunk[0], caption=caps[0])
                    else:
                        await client.send_file(ent, chunk, caption=caps)
                    ok += len(chunk)
                except Exception as e:
                    bad += len(chunk)
                    print("fast send failed:", e, flush=True)
        if not s.get("quiet"):
            try:
                await bot_say(f"Saved {ok} file(s) to {', '.join(sorted(dests))}" + (f" ({bad} failed)" if bad else ""))
            except Exception as e:
                print("summary failed:", e, flush=True)


@app.get("/api/items")
async def items():
    if cache["items"] is None:
        cache["items"] = [i async for i in (to_item(m) async for m in client.iter_messages(ent)) if i]
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
    if request.query_params.get("dl"):
        hdr["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(m.file.name or f"{m.id}{m.file.ext or ''}")
    return StreamingResponse(gen(), status_code=status, headers=hdr,
                             media_type=m.file.mime_type or "application/octet-stream")


class Sink:                              # lets zipfile write to a stream that cannot seek
    def __init__(self): self.buf, self.pos = bytearray(), 0
    def write(self, b): self.buf += b; self.pos += len(b); return len(b)
    def flush(self): pass
    def tell(self): return self.pos


def safe(n):
    return re.sub(r'[\\/:*?"<>|]+', "_", n).strip() or "file"


@app.post("/api/zip")
async def zip_dl(request: Request):
    form = await request.form()
    folder = (form.get("folder") or "").strip("/ ")
    ids = [int(x) for x in (form.get("ids") or "").split(",") if x]
    byid = {i["id"]: i for i in await items() if i["kind"] != "folder"}
    sel = [i for i in byid.values() if i["folder"] == folder or i["folder"].startswith(folder + "/")] if folder \
        else [byid[i] for i in ids if i in byid]
    if not sel:
        raise HTTPException(404, "Nothing to download")
    root = safe(folder.split("/")[-1]) if folder else "Drive selection"
    used, entries = set(), []
    for it in sel:
        rel = it["folder"][len(folder):].strip("/") if folder else ""
        parts = ([root] if folder else []) + [safe(x) for x in rel.split("/") if x]
        base = safe(it["name"]); name, n = base, 1
        while "/".join(parts + [name]) in used:        # keep duplicate file names apart
            n += 1
            stem, ext = os.path.splitext(base)
            name = f"{stem} ({n}){ext}"
        path = "/".join(parts + [name]); used.add(path)
        entries.append((path, it["id"]))

    async def gen():
        sink = Sink()
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)   # photos/videos are already compressed
        msgs = {}
        for i in range(0, len(entries), 100):
            for m in await client.get_messages(ent, ids=[e[1] for e in entries[i:i + 100]]):
                if m and m.media:
                    msgs[m.id] = m
        for path, mid in entries:
            m = msgs.get(mid)
            if not m:
                continue
            zi = zipfile.ZipInfo(path, date_time=m.date.timetuple()[:6])
            zi.compress_type = zipfile.ZIP_STORED
            with zf.open(zi, "w", force_zip64=True) as w:
                async for ch in client.iter_download(m.media, request_size=512 * 1024):
                    w.write(ch)
                    if sink.buf:
                        yield bytes(sink.buf)
                        sink.buf.clear()
        zf.close()
        yield bytes(sink.buf)

    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(root + ".zip")})


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
