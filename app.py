"""Telegram Drive: a private Telegram channel as cloud storage (folders, trash, sharing, backup channel, login page).
Env: API_ID, API_HASH, CHANNEL_ID, APP_PASSWORD. Optional: BACKUP_CHANNEL_ID, BOT_TOKEN + OWNER_ID (fast bot mode), HOST, PORT.
"""
import os, re, json, time, zipfile, sqlite3, secrets, hashlib, hmac, mimetypes, shutil, tempfile, asyncio, requests
from urllib.parse import quote
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse
from telethon import TelegramClient, events

BASE = os.path.dirname(os.path.abspath(__file__))
THUMBS = os.path.join(BASE, "thumbs"); os.makedirs(THUMBS, exist_ok=True)
client = TelegramClient(os.path.join(BASE, "drive"), int(os.environ["API_ID"]), os.environ["API_HASH"])
CHANNEL = int(os.environ["CHANNEL_ID"])
BK = os.getenv("BACKUP_CHANNEL_ID")
PW = os.getenv("APP_PASSWORD")
SECRET = hashlib.sha256(f"{os.environ['API_HASH']}|{PW}".encode()).digest()   # changing the password signs everyone out
TAG = "📁"
TRASH_DAYS = 30
app = FastAPI()
sem, up_sem, scan_lock = asyncio.Semaphore(4), asyncio.Semaphore(3), asyncio.Lock()
ent = None
store, bdone, bad, fails, bstat = {}, set(), set(), {}, {"err": ""}

DB = sqlite3.connect(os.path.join(BASE, "drive.db"), check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, folder TEXT, kind TEXT, name TEXT, size INT, w INT, h INT, date TEXT, trashed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS backup(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shares(token TEXT PRIMARY KEY, kind TEXT, folder TEXT, ids TEXT, name TEXT, created INT, expires INT, hits INT DEFAULT 0);
""")
ICOLS = ("id", "folder", "kind", "name", "size", "w", "h", "date", "trashed")

# ---------------------------------------------------------------- login
LOGIN = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Sign in - Channel Drive</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;700&family=Sora:wght@700;800&display=swap" rel=stylesheet>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;font:15px Manrope,system-ui,sans-serif;color:#141830;background:#f3f4fb;overflow:hidden}
body::before,body::after{content:"";position:fixed;width:60vmax;height:60vmax;border-radius:50%;filter:blur(90px);opacity:.55;z-index:-1}
body::before{top:-26vmax;left:-16vmax;background:radial-gradient(circle,#c3ceff,transparent 65%)}body::after{bottom:-32vmax;right:-20vmax;background:radial-gradient(circle,#f8c4e8,transparent 65%)}
@media(prefers-color-scheme:dark){body{background:#070a1c;color:#eef0ff}body::before{background:radial-gradient(circle,#2b3fd1,transparent 65%)}body::after{background:radial-gradient(circle,#9a2fb6,transparent 65%)}.card{background:rgba(255,255,255,.06)!important}input[type=password]{background:#121631!important;color:#eef0ff}}
.card{width:min(380px,92vw);padding:34px 30px;border-radius:28px;background:rgba(255,255,255,.65);backdrop-filter:blur(22px);border:1px solid rgba(128,128,160,.18);box-shadow:0 30px 70px -20px rgba(40,30,120,.35);text-align:center}
.logo{width:56px;height:56px;margin:0 auto 14px;display:grid;place-items:center;border-radius:18px;background:linear-gradient(135deg,#6a7bff,#b36bff 55%,#ff6bb5);color:#fff;font-size:28px;box-shadow:0 16px 30px -10px #b36bff}
h1{margin:0 0 4px;font:800 22px Sora,sans-serif}p{margin:0 0 20px;color:#6a7196}
input[type=password]{width:100%;padding:13px 16px;border-radius:14px;border:1px solid rgba(128,128,160,.25);font:inherit;outline:0;background:#fff}input[type=password]:focus{border-color:#6a7bff;box-shadow:0 0 0 4px rgba(106,123,255,.2)}
label{display:flex;gap:8px;align-items:center;justify-content:center;margin:14px 0;font-size:13px;color:#6a7196}
button{width:100%;padding:13px;border:0;border-radius:14px;font:700 15px Manrope,sans-serif;color:#fff;cursor:pointer;background:linear-gradient(135deg,#6a7bff,#b36bff 55%,#ff6bb5);box-shadow:0 14px 28px -10px #b36bff}
.err{color:#ef4458;font-weight:600;margin-top:14px;min-height:18px}</style>
<form class=card method=post action=/login><div class=logo>&#9729;</div><h1>Channel Drive</h1><p>Enter your password to continue</p>
<input type=password name=password placeholder=Password autofocus required><label><input type=checkbox name=remember checked> Keep me signed in for 30 days</label><button>Sign in</button><div class=err>__ERR__</div></form></html>"""


def make_token(days):
    exp = int(time.time()) + days * 86400
    return f"{exp}.{hmac.new(SECRET, str(exp).encode(), 'sha256').hexdigest()}"


def valid(tok):
    try:
        exp, sig = tok.split(".")
        return int(exp) > time.time() and hmac.compare_digest(sig, hmac.new(SECRET, exp.encode(), "sha256").hexdigest())
    except Exception:
        return False


def client_ip(r):
    return r.headers.get("x-forwarded-for", r.client.host).split(",")[0].strip()


@app.middleware("http")
async def auth(req: Request, call):
    p = req.url.path
    if not PW or p == "/login" or p.startswith("/s/") or valid(req.cookies.get("td", "")):
        return await call(req)
    if p.startswith("/api/"):
        return JSONResponse({"detail": "Unauthorized"}, 401)
    return RedirectResponse("/login", 303)


@app.get("/login")
async def login_page(request: Request):
    if valid(request.cookies.get("td", "")):
        return RedirectResponse("/", 303)
    return HTMLResponse(LOGIN.replace("__ERR__", ""))


@app.post("/login")
async def login(request: Request):
    ip, now = client_ip(request), time.time()
    fails[ip] = recent = [t for t in fails.get(ip, []) if now - t < 900]
    if len(recent) >= 5:                                     # 5 wrong passwords = 15 minute lockout
        return HTMLResponse(LOGIN.replace("__ERR__", "Too many attempts. Try again in 15 minutes."), 429)
    form = await request.form()
    if PW and hmac.compare_digest(str(form.get("password", "")).encode(), PW.encode()):
        fails.pop(ip, None)
        days = 30 if form.get("remember") else 1
        r = RedirectResponse("/", 303)
        r.set_cookie("td", make_token(days), max_age=days * 86400, httponly=True, samesite="lax",
                     secure=request.headers.get("x-forwarded-proto") == "https")
        return r
    fails[ip].append(now)
    await asyncio.sleep(1)
    return HTMLResponse(LOGIN.replace("__ERR__", "Wrong password"), 401)


@app.post("/logout")
async def logout():
    r = JSONResponse({"ok": True}); r.delete_cookie("td"); return r

# ---------------------------------------------------------------- items (SQLite-backed, instant start-up)
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


def norm(it, trashed=0):
    it.setdefault("name", ""); it.setdefault("trashed", trashed)
    for k in ("size", "w", "h"): it.setdefault(k, 0)
    return [it[c] for c in ICOLS]


def put(it):
    old = store.get(it["id"])
    row = norm(it, old["trashed"] if old else 0)
    it["trashed"] = old["trashed"] if old else it["trashed"]
    store[it["id"]] = it
    DB.execute("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?)", row); DB.commit()


def drop(ids):
    for i in ids: store.pop(i, None)
    DB.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids]); DB.commit()


def set_trash(ids, ts):
    for i in ids:
        if i in store: store[i]["trashed"] = ts
    DB.executemany("UPDATE items SET trashed=? WHERE id=?", [(ts, i) for i in ids]); DB.commit()


async def rescan():
    new = {}
    async for m in client.iter_messages(ent):
        it = to_item(m)
        if it:
            it["trashed"] = (store.get(it["id"]) or {}).get("trashed", 0)
            new[it["id"]] = it
    store.clear(); store.update(new)
    DB.execute("DELETE FROM items")
    DB.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)", [norm(i) for i in store.values()]); DB.commit()


async def ensure():
    if not store:
        async with scan_lock:
            if not store: await rescan()


async def rescan_bg():
    try:
        async with scan_lock: await rescan()
    except Exception as e:
        print("rescan failed:", e, flush=True)


async def on_new(ev):                    # also catches posts made by the upload bot
    it = to_item(ev.message)
    if it: put(it)


async def on_del(ev):
    drop([i for i in ev.deleted_ids if i in store])


async def purge_loop():                  # trash older than 30 days is deleted for good
    while True:
        await asyncio.sleep(3600)
        cut = int(time.time()) - TRASH_DAYS * 86400
        ids = [i["id"] for i in store.values() if i["trashed"] and i["trashed"] < cut]
        if ids:
            try:
                await client.delete_messages(ent, ids); drop(ids)
            except Exception as e:
                print("auto purge failed:", e, flush=True)


async def backup_loop():                 # mirror every post to the backup channel using Telegram's own forwarding
    bk = await client.get_entity(int(BK))
    bdone.update(r[0] for r in DB.execute("SELECT id FROM backup"))
    while True:
        try:
            todo = sorted(i for i in store if i not in bdone and i not in bad)[:100]
            if not todo:
                await asyncio.sleep(20); continue
            try:
                await client.forward_messages(bk, todo, from_peer=ent)
            except Exception as e:
                bstat["err"] = str(e)[:100]; good = []
                for i in todo:
                    try: await client.forward_messages(bk, i, from_peer=ent); good.append(i)
                    except Exception: pass
                if not good:
                    await asyncio.sleep(300); continue
                bad.update(set(todo) - set(good)); todo = good
            else:
                bstat["err"] = ""
            DB.executemany("INSERT OR IGNORE INTO backup VALUES(?)", [(i,) for i in todo]); DB.commit(); bdone.update(todo)
        except Exception as e:
            bstat["err"] = str(e)[:100]; print("backup error:", e, flush=True); await asyncio.sleep(120)


@app.on_event("startup")
async def start():
    global ent
    await client.start()          # first run: asks for phone + login code in the terminal
    await client.get_dialogs()
    ent = await client.get_entity(CHANNEL)
    for r in DB.execute("SELECT * FROM items"):
        store[r[0]] = dict(zip(ICOLS, r))
    client.add_event_handler(on_new, events.NewMessage(chats=ent))
    client.add_event_handler(on_new, events.MessageEdited(chats=ent))
    client.add_event_handler(on_del, events.MessageDeleted(chats=ent))
    if os.getenv("BOT_TOKEN") and os.getenv("OWNER_ID"):        # fast mode: save what you send the bot via your own account
        try:
            bot_ent = await client.get_entity(int(os.environ["BOT_TOKEN"].split(":")[0]))
            client.add_event_handler(on_bot_media, events.NewMessage(chats=bot_ent, outgoing=True))
            asyncio.create_task(fast_worker())
            print("fast upload mode ready", flush=True)
        except Exception as e:
            print("fast mode unavailable:", e, flush=True)
    asyncio.create_task(rescan_bg()); asyncio.create_task(purge_loop())
    if BK: asyncio.create_task(backup_loop())

# ---------------------------------------------------------------- fast bot mode
BSTATE = os.path.join(BASE, "bot_state.json")
fq = asyncio.Queue()


def bstate():
    try: return json.load(open(BSTATE))
    except Exception: return {}


async def bot_say(text):
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage"
    await asyncio.to_thread(requests.post, url, json={"chat_id": int(os.environ["OWNER_ID"]), "text": text}, timeout=30)


async def on_bot_media(ev):
    m = ev.message
    if m.media and m.file and bstate().get("fast", True): await fq.put(m)


def dest_for(m, s):
    f, mime = s.get("folder", ""), m.file.mime_type or ""
    if s.get("split", True):
        sub = "Videos" if m.video or mime.startswith("video/") else "Photos" if m.photo or mime.startswith("image/") else "Files"
        f = (f + "/" if f else "") + sub
    return f


async def fast_worker():
    while True:
        batch = [await fq.get()]
        await asyncio.sleep(1.5)
        while not fq.empty() and len(batch) < 200: batch.append(fq.get_nowait())
        s, ok, nok, dests = bstate(), 0, 0, set()
        for key in ("media", "doc"):
            grp = [m for m in batch if ("media" if m.photo or m.video else "doc") == key]
            for i in range(0, len(grp), 10):
                chunk, caps = grp[i:i + 10], []
                for m in chunk:
                    d = dest_for(m, s); dests.add(d or "Home"); caps.append(f"{TAG} {d}".strip())
                try:
                    if len(chunk) == 1: await client.send_file(ent, chunk[0], caption=caps[0])
                    else: await client.send_file(ent, chunk, caption=caps)
                    ok += len(chunk)
                except Exception as e:
                    nok += len(chunk); print("fast send failed:", e, flush=True)
        if not s.get("quiet"):
            try: await bot_say(f"Saved {ok} file(s) to {', '.join(sorted(dests))}" + (f" ({nok} failed)" if nok else ""))
            except Exception as e: print("summary failed:", e, flush=True)

# ---------------------------------------------------------------- API
@app.get("/api/items")
async def items():
    await ensure()
    return [i for i in store.values() if not i["trashed"]]


@app.get("/api/trash")
async def trash():
    return sorted((i for i in store.values() if i["trashed"] and i["kind"] != "folder"), key=lambda i: -i["trashed"])


@app.post("/api/delete")                 # moves to Trash (nothing is deleted from Telegram yet)
async def delete(d: dict):
    set_trash(d["ids"], int(time.time())); return {"ok": True}


@app.post("/api/restore")
async def restore(d: dict):
    set_trash(d["ids"], 0); return {"ok": True}


@app.post("/api/purge")                  # permanent delete, from Trash only
async def purge(d: dict):
    ids = [i["id"] for i in store.values() if i["trashed"]] if d.get("all") else [i for i in d.get("ids", []) if i in store and store[i]["trashed"]]
    if ids:
        await client.delete_messages(ent, ids); drop(ids)
    return {"ok": True, "n": len(ids)}


@app.post("/api/folder")
async def new_folder(d: dict):
    it = to_item(await client.send_message(ent, f"{TAG} {d['name'].strip('/ ')}"))
    if it: put(it)
    return {"ok": True}


@app.post("/api/move")
async def move(d: dict):
    folder = d["folder"].strip("/ ")
    for mid in d["ids"]:
        m = await client.get_messages(ent, ids=mid)
        await client.edit_message(ent, m, f"{TAG} {folder}".strip())
        if mid in store: store[mid]["folder"] = folder; put(store[mid])
    return {"ok": True}


@app.post("/api/upload")
async def upload(folder: str = Form(""), original: bool = Form(True), files: list[UploadFile] = File(...)):
    folder = folder.strip("/ ")
    for f in files:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, os.path.basename(f.filename))
        with open(path, "wb") as o:
            while chunk := await f.read(1024 * 1024): o.write(chunk)
        mime = mimetypes.guess_type(path)[0] or ""
        try:
            async with up_sem:
                msg = await client.send_file(ent, path, caption=f"{TAG} {folder}".strip(),
                                             force_document=original and mime.startswith("image/"), supports_streaming=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        it = to_item(msg)
        if it: put(it)
    return {"ok": True}


@app.get("/api/thumb/{mid}")
async def thumb(mid: int):
    p = os.path.join(THUMBS, f"{mid}.jpg")
    if not os.path.exists(p):
        async with sem:
            m = await client.get_messages(ent, ids=mid)
            b = await client.download_media(m, bytes, thumb=-1) if m else None
        if not b: raise HTTPException(404)
        open(p, "wb").write(b)
    return FileResponse(p, headers={"Cache-Control": "max-age=31536000"})


@app.get("/api/file/{mid}")
async def file(mid: int, request: Request):
    m = await client.get_messages(ent, ids=mid)
    if not m or not m.file: raise HTTPException(404)
    size = m.file.size
    start, end, status = 0, size - 1, 200
    r = request.headers.get("range")
    if r:
        a, b = r.replace("bytes=", "").split("-")
        start, end, status = int(a or 0), int(b) if b else size - 1, 206

    async def gen():
        left = end - start + 1
        async for ch in client.iter_download(m.media, offset=start, request_size=512 * 1024):
            ch = ch[:left]; left -= len(ch)
            yield ch
            if left <= 0: break

    hdr = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Content-Range": f"bytes {start}-{end}/{size}"}
    if request.query_params.get("dl"):
        hdr["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(m.file.name or f"{m.id}{m.file.ext or ''}")
    return StreamingResponse(gen(), status_code=status, headers=hdr, media_type=m.file.mime_type or "application/octet-stream")


class Sink:                              # lets zipfile write to a stream that cannot seek
    def __init__(self): self.buf, self.pos = bytearray(), 0
    def write(self, b): self.buf += b; self.pos += len(b); return len(b)
    def flush(self): pass
    def tell(self): return self.pos


def safe(n):
    return re.sub(r'[\\/:*?"<>|]+', "_", n).strip() or "file"


async def make_zip(folder, ids, title=None):
    await ensure()
    live = {i["id"]: i for i in store.values() if i["kind"] != "folder" and not i["trashed"]}
    sel = [i for i in live.values() if i["folder"] == folder or i["folder"].startswith(folder + "/")] if folder else [live[i] for i in ids if i in live]
    if not sel: raise HTTPException(404, "Nothing to download")
    root = safe(title or (folder.split("/")[-1] if folder else "Drive selection"))
    used, entries = set(), []
    for it in sel:
        rel = it["folder"][len(folder):].strip("/") if folder else ""
        parts = ([root] if folder else []) + [safe(x) for x in rel.split("/") if x]
        base = safe(it["name"]); name, n = base, 1
        while "/".join(parts + [name]) in used:
            n += 1; stem, ext = os.path.splitext(base); name = f"{stem} ({n}){ext}"
        path = "/".join(parts + [name]); used.add(path); entries.append((path, it["id"]))

    async def gen():
        sink = Sink()
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
        msgs = {}
        for i in range(0, len(entries), 100):
            for m in await client.get_messages(ent, ids=[e[1] for e in entries[i:i + 100]]):
                if m and m.media: msgs[m.id] = m
        for path, mid in entries:
            m = msgs.get(mid)
            if not m: continue
            zi = zipfile.ZipInfo(path, date_time=m.date.timetuple()[:6]); zi.compress_type = zipfile.ZIP_STORED
            with zf.open(zi, "w", force_zip64=True) as w:
                async for ch in client.iter_download(m.media, request_size=512 * 1024):
                    w.write(ch)
                    if sink.buf: yield bytes(sink.buf); sink.buf.clear()
        zf.close()
        yield bytes(sink.buf)

    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(root + ".zip")})


@app.post("/api/zip")
async def zip_dl(request: Request):
    form = await request.form()
    return await make_zip((form.get("folder") or "").strip("/ "), [int(x) for x in (form.get("ids") or "").split(",") if x])

# ---------------------------------------------------------------- share links
@app.post("/api/share")
async def share_new(d: dict):
    folder, ids, days = (d.get("folder") or "").strip("/ "), d.get("ids") or [], int(d.get("days", 7))
    if not folder and not ids: raise HTTPException(400, "Nothing to share")
    tok, now = secrets.token_urlsafe(16), int(time.time())
    DB.execute("INSERT INTO shares VALUES(?,?,?,?,?,?,?,0)",
               (tok, "folder" if folder else "files", folder, json.dumps(ids), folder.split("/")[-1] if folder else f"{len(ids)} file(s)",
                now, now + days * 86400 if days else 0)); DB.commit()
    return {"token": tok}


@app.get("/api/shares")
async def shares():
    return [dict(token=r[0], kind=r[1], name=r[2], created=r[3], expires=r[4], hits=r[5])
            for r in DB.execute("SELECT token,kind,name,created,expires,hits FROM shares ORDER BY created DESC")]


@app.post("/api/share/revoke")
async def share_revoke(d: dict):
    DB.execute("DELETE FROM shares WHERE token=?", (d["token"],)); DB.commit(); return {"ok": True}


def share_of(tok):                        # public: returns (row, items) or 404
    r = DB.execute("SELECT kind,folder,ids,name,expires FROM shares WHERE token=?", (tok,)).fetchone()
    if not r or (r[4] and r[4] < time.time()): raise HTTPException(404, "Link expired or not found")
    ok = [i for i in store.values() if i["kind"] != "folder" and not i["trashed"]]
    if r[0] == "folder": return r, [i for i in ok if i["folder"] == r[1] or i["folder"].startswith(r[1] + "/")]
    s = set(json.loads(r[2])); return r, [i for i in ok if i["id"] in s]


@app.get("/s/{tok}")
async def share_page(tok: str):
    return FileResponse(os.path.join(BASE, "static", "share.html"))


@app.get("/s/{tok}/items")
async def share_items(tok: str):
    await ensure()
    r, its = share_of(tok)
    DB.execute("UPDATE shares SET hits=hits+1 WHERE token=?", (tok,)); DB.commit()
    pre = r[1] + "/" if r[0] == "folder" else ""
    return {"name": r[3], "expires": r[4], "items": [dict(id=i["id"], name=i["name"], kind=i["kind"], size=i["size"], w=i["w"], h=i["h"],
                                                           folder=i["folder"][len(r[1]):].strip("/") if r[0] == "folder" else "") for i in its]}


@app.get("/s/{tok}/thumb/{mid}")
async def share_thumb(tok: str, mid: int):
    if not any(i["id"] == mid for i in share_of(tok)[1]): raise HTTPException(404)
    return await thumb(mid)


@app.get("/s/{tok}/file/{mid}")
async def share_file(tok: str, mid: int, request: Request):
    if not any(i["id"] == mid for i in share_of(tok)[1]): raise HTTPException(404)
    return await file(mid, request)


@app.get("/s/{tok}/zip")
async def share_zip(tok: str):
    r, its = share_of(tok)
    return await make_zip(r[1], [i["id"] for i in its], r[3])

# ---------------------------------------------------------------- colors, backup status, page
COLORS = os.path.join(BASE, "colors.json")


@app.get("/api/colors")
async def colors_get():
    return json.load(open(COLORS)) if os.path.exists(COLORS) else {}


@app.post("/api/colors")
async def colors_set(d: dict):
    c = await colors_get()
    if d.get("color"): c[d["folder"]] = d["color"]
    else: c.pop(d["folder"], None)
    json.dump(c, open(COLORS, "w")); return c


@app.get("/api/backup")
async def backup_status():
    return {"on": bool(BK), "done": len(bdone & store.keys()), "total": len(store), "err": bstat["err"]}


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 8000)))
