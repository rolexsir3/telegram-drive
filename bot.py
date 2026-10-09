"""Telegram Drive upload bot: send photos/videos/albums to this bot and they land in your drive.
Env: BOT_TOKEN, OWNER_ID (your numeric Telegram id), CHANNEL_ID. The bot must be an admin of the channel with 'Post messages'.
Commands: /folder name (create + switch), /home, /where, /split on|off, /help
"""
import os, json, time, socket, queue, threading, requests
import urllib3.util.connection as _uc

_uc.allowed_gai_family = lambda: socket.AF_INET   # IPv4 only: avoids slow or broken IPv6 routes
http = requests.Session()                          # reuse the connection instead of a new TLS handshake per call

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
CHANNEL = int(os.environ["CHANNEL_ID"])
API = f"https://api.telegram.org/bot{TOKEN}/"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
st = json.load(open(STATE)) if os.path.exists(STATE) else {"folder": "", "split": True}
groups = []


def save():
    json.dump(st, open(STATE, "w"))


def call(method, **p):
    while True:
        t = time.time()
        r = http.post(API + method, json=p, timeout=70).json()
        if time.time() - t > 2 and method != "getUpdates":
            print(f"slow {method}: {time.time() - t:.1f}s", flush=True)
        if r.get("ok"):
            return r["result"]
        if r.get("error_code") == 429:
            print(f"rate limited, waiting {r['parameters']['retry_after']}s", flush=True)
            time.sleep(r["parameters"]["retry_after"] + 1)
            continue
        raise RuntimeError(r.get("description", r))


def say(chat, text):
    call("sendMessage", chat_id=chat, text=text)


def kind_of(m):
    d = m.get("document") or {}
    mime = d.get("mime_type", "")
    if "video" in m or "video_note" in m or "animation" in m or mime.startswith("video/"): return "video"
    if "photo" in m or mime.startswith("image/"): return "photo"
    return "file"


def target(kind):
    f = st["folder"]
    if st["split"]:
        sub = {"video": "Videos", "photo": "Photos"}.get(kind, "Files")
        f = (f + "/" if f else "") + sub
    return f


def use(name, create):
    st["folder"] = name
    k = st.setdefault("known", [])
    if name not in k:
        k.append(name)
    save()
    if create:
        call("sendMessage", chat_id=CHANNEL, text=f"📁 {name}")


HELP = ("Send photos, videos or albums and I store them in your drive.\n\n"
        "/newfolder name - create a folder and use it (a/b for nested)\n"
        "/setfolder name - switch to an existing folder\n"
        "/folders - pick from folders I know\n"
        "/home - go back to the top level\n/where - show current folder\n"
        "/quiet on|off - skip the saving/summary replies\n"
        "/split on|off - keep Photos and Videos in separate subfolders\n\n"
        "Tip: send as File to keep original photo quality.")


q = queue.Queue()
stat = {"ok": 0, "bad": 0, "chat": None, "dests": set()}


def worker():
    """Copies files into the channel in the background so commands are never blocked by Telegram's rate limits."""
    while True:
        try:
            chat, mid, dest = q.get(timeout=3)
        except queue.Empty:
            if (stat["ok"] or stat["bad"]) and not st.get("quiet"):
                where = ", ".join(sorted(stat["dests"]))
                try:
                    say(stat["chat"], f"Saved {stat['ok']} file(s) to {where}" + (f" ({stat['bad']} failed)" if stat["bad"] else ""))
                except Exception as e:
                    print("summary failed:", e, flush=True)
            stat.update(ok=0, bad=0, dests=set())
            continue
        stat["chat"] = chat
        stat["dests"].add(dest or "Home")
        try:
            call("copyMessage", chat_id=CHANNEL, from_chat_id=chat, message_id=mid, caption=f"📁 {dest}".strip())
            stat["ok"] += 1
        except Exception as e:
            stat["bad"] += 1
            print("copy failed:", e, flush=True)


def handle(m):
    chat = m["chat"]["id"]
    if m.get("from", {}).get("id") != OWNER:
        return
    text = (m.get("text") or "").strip()
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip().strip("/ ")
        if cmd in ("/start", "/help"): say(chat, HELP)
        elif cmd in ("/folder", "/newfolder"):
            if not arg: return say(chat, "Usage: /newfolder name")
            use(arg, create=True)
            say(chat, f"Folder ready: {arg}\nNow send photos, videos or albums.")
        elif cmd == "/setfolder":
            if not arg: return say(chat, "Usage: /setfolder name  (or tap /folders)")
            use(arg, create=False)
            say(chat, f"Now saving to: {arg}")
        elif cmd == "/folders":
            fl = st.get("known", [])
            if not fl: return say(chat, "No folders yet. Create one with /newfolder name")
            call("sendMessage", chat_id=chat, text="Pick a folder:",
                 reply_markup={"inline_keyboard": [[{"text": f, "callback_data": f"f:{i}"}] for i, f in enumerate(fl[:40])]})
        elif cmd == "/home": st["folder"] = ""; save(); say(chat, "Now saving to the top level.")
        elif cmd == "/split":
            st["split"] = arg.lower() != "off"; save()
            say(chat, "Photos and Videos go to separate subfolders." if st["split"] else "Everything goes straight into the folder.")
        elif cmd == "/quiet":
            st["quiet"] = arg.lower() != "off"; save()
            say(chat, "Quiet mode on: I only reply on errors." if st["quiet"] else "Quiet mode off.")
        elif cmd == "/where": say(chat, f"Folder: {st['folder'] or 'Home'}\nSplit photos/videos: {'on' if st['split'] else 'off'}\nQueued: {q.qsize()}")
        else: say(chat, HELP)
        return
    if not any(k in m for k in ("photo", "video", "document", "animation", "video_note")):
        return
    dest = target(kind_of(m))
    if q.empty() and not st.get("quiet"):
        say(chat, "Got it, saving…")
    q.put((chat, m["message_id"], dest))

threading.Thread(target=worker, daemon=True).start()
offset = None
print("bot running")
while True:
    try:
        for u in call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"]):
            offset = u["update_id"] + 1
            if "callback_query" in u:
                q = u["callback_query"]
                fl = st.get("known", [])
                if q["from"]["id"] == OWNER and q.get("data", "").startswith("f:") and int(q["data"][2:]) < len(fl):
                    use(fl[int(q["data"][2:])], create=False)
                    call("answerCallbackQuery", callback_query_id=q["id"], text=f"Folder: {st['folder']}")
                    say(q["message"]["chat"]["id"], f"Now saving to: {st['folder']}")
                continue
            if "message" in u:
                try:
                    handle(u["message"])
                except Exception as e:
                    print("error:", e)
                    try: say(u["message"]["chat"]["id"], f"Failed: {e}")
                    except Exception: pass
    except Exception as e:
        print("poll error:", e)
        time.sleep(5)
