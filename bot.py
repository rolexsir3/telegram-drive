"""Telegram Drive upload bot: send photos/videos/albums to this bot and they land in your drive.
Env: BOT_TOKEN, OWNER_ID (your numeric Telegram id), CHANNEL_ID. The bot must be an admin of the channel with 'Post messages'.
Commands: /folder name (create + switch), /home, /where, /split on|off, /help
"""
import os, json, time, requests

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
        r = requests.post(API + method, json=p, timeout=70).json()
        if r.get("ok"):
            return r["result"]
        if r.get("error_code") == 429:
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
        "/split on|off - keep Photos and Videos in separate subfolders\n\n"
        "Tip: send as File to keep original photo quality.")


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
        elif cmd == "/where": say(chat, f"Folder: {st['folder'] or 'Home'}\nSplit photos/videos: {'on' if st['split'] else 'off'}")
        else: say(chat, HELP)
        return
    if not any(k in m for k in ("photo", "video", "document", "animation", "video_note")):
        return
    dest = target(kind_of(m))
    call("copyMessage", chat_id=CHANNEL, from_chat_id=chat, message_id=m["message_id"], caption=f"📁 {dest}".strip())
    g = m.get("media_group_id")
    if g:
        if g in groups: return
        groups.append(g); del groups[:-50]
        say(chat, f"Saving album to {dest or 'Home'}")
    else:
        say(chat, f"Saved to {dest or 'Home'}")


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
