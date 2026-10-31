#!/usr/bin/env python3
"""Survey Corps: local todo list fed by emails dragged from Outlook. Run: python3 app.py"""
from collections import Counter
import base64, html, http.client, json, os, re, socket, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
DATA = Path(__file__).resolve().parent / "data"
MAILS = DATA / "mails"   # original .eml copies, opened in Outlook via "Open mail"
INBOX = DATA / "inbox"   # fallback: drag emails from Outlook into this Finder folder
SHOTS = DATA / "shots"   # screenshots turned into tasks
IMAGE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
DB = DATA / "todos.json"
CONFIG = DATA / "config.json"  # optional: {"argo_user": "<ANL username>", "argo_model": "claudesonnet55"}
ARGO = "https://apps.inside.anl.gov/argoapi/v1/chat/completions"
MODEL, BACKUP = "claudesonnet55", "claudesonnet5"  # default Argo model, and the one tried when it errors
BRIEFED = DATA / "briefed"  # date of the last morning briefing
HUB_GONE = DATA / "hub_deletes.json"  # keys of deleted tasks the hub still has a copy of, deleted there on the next pass
lock, config_lock = threading.Lock(), threading.Lock()
retry_busy = threading.Lock()
argo_ok, argo_at = None, 0.0  # last Argo call: outcome (None = not tried yet) and when; shown in the UI
busy, busy_lock = Counter(), threading.Lock()  # Argo calls in flight, by tag: task id, "new:<id>", "ask", "briefing", "check"
bubbles, pets_seen = [], 0.0  # speech bubbles for the Mac app's pet [{"id", "title", "text"}]; when it last polled


def load():
    return json.loads(DB.read_text()) if DB.exists() else []


def save(todos, kick=True):
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(todos, indent=1, ensure_ascii=False))
    tmp.replace(DB)  # atomic, so a crash never leaves a half-written db
    if kick:  # the hub sync's own snapshot writes don't, or it would wake itself forever
        hub_kick.set()


def email_text(msg):
    try:
        part = msg.get_body(("plain", "html"))
        text = part.get_content() if part else ""
    except Exception:
        return "(could not decode body)"
    if part and part.get_content_type() == "text/html":
        text = re.sub(r"<(style|script|head)\b.*?</\1>", "", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r".*ZjQcmQRYFpfpt.*\n?", "", text)  # Proofpoint banner markers
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\s*\n\s*\n\s*", "\n\n", text).strip()


def first_json(text):
    """The first JSON object in a model's reply, or None. Not a greedy {.*}: a reply with two objects (or a stray brace
    in its prose) would span them all and fail to parse."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            d = dec.raw_decode(text, m.start())[0]
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def clean_suggestion(text, now):
    """Pull {"labels", "due", "title", "actions"} out of the model's reply, dropping anything malformed or already past."""
    d = first_json(text) or {}
    labels = [str(x).strip() for x in d.get("labels") or [] if str(x).strip()][:3]
    due = str(d.get("due") or "")
    try:
        ok = datetime.strptime(due, "%Y-%m-%dT%H:%M") > now
    except ValueError:
        ok = False
    actions = [str(x).strip()[:200] for x in d.get("actions") or [] if str(x).strip()][:8]
    return {"labels": [l for l in labels if len(l) <= 24], "due": due if ok else "",
            "title": str(d.get("title") or "").strip()[:200], "actions": actions if len(actions) > 1 else []}


def config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


SETTINGS = {  # editable in the app's Settings dialog -> (default, max for ints / None for text)
    "argo_user": ("", None), "argo_model": (MODEL, None), "retry_minutes": (10, 1440),
    "briefing_hour": (8, 24), "hub_url": ("", None), "hub_token": ("", None)}


def get_settings():
    cfg = config()
    s = {k: cfg.get(k, d) for k, (d, _) in SETTINGS.items()}
    s["hub_token_set"] = bool(s.pop("hub_token"))  # the page only learns whether there is one, never the token
    return s


def set_settings(new):
    with config_lock:
        cfg = config()
        for k, v in new.items():
            if k not in SETTINGS:
                continue
            d, hi = SETTINGS[k]
            v = max(0, min(hi, int(v))) if hi else str(v).strip()
            if k == "hub_url":
                v = v.rstrip("/")
            cfg[k] = v if v != "" else d
            if v == "" or v == d and k not in ("retry_minutes", "briefing_hour"):
                cfg.pop(k, None)  # back to the default
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
        tmp.replace(CONFIG)
    hub_kick.set()  # a new address or token gets tried straight away


def argo_on():
    return bool(config().get("argo_user"))


def argo(prompt, max_tokens, timeout=120, tag="", image=None):
    """Ask the lab's Argo LLM (with an optional image file it can see), using the model in Settings.
    Returns the reply text, or None if unconfigured, down or unreachable (e.g. no VPN)."""
    global argo_ok, argo_at
    cfg = config()
    if not cfg.get("argo_user"):
        return None
    u = urlparse(ARGO)
    try:  # off VPN the host resolves but connections hang until the timeout: find out in 3 s instead of minutes
        socket.create_connection((u.hostname, u.port or 443), timeout=3).close()
    except OSError:
        argo_ok, argo_at = False, time.time()
        return None
    content = prompt if not image else [{"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": data_url(image)}}]
    models = list(dict.fromkeys([cfg.get("argo_model") or MODEL, BACKUP]))
    with busy_lock:
        busy[tag] += 1
    try:
        for i, model in enumerate(models):
            req = urllib.request.Request(ARGO, json.dumps({
                "model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}).encode(),
                {"Content-Type": "application/json", "Authorization": "Bearer " + cfg["argo_user"]})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    text = json.load(r)["choices"][0]["message"]["content"]
                break
            except (urllib.error.HTTPError, KeyError, IndexError, ValueError):  # model errored: try the backup
                if i == len(models) - 1:
                    raise
    except Exception:  # down, 5xx, timeout, off VPN, malformed reply: all the same to callers
        argo_ok = False
        return None
    finally:
        argo_at = time.time()
        with busy_lock:
            busy[tag] -= 1
    argo_ok = True
    return text


def data_url(path):
    """A screenshot as a data: URL, shrunk to the 1568 px Claude reads anyway (so big Retina shots stay small)."""
    small = Path(str(path) + ".send.jpg")
    if subprocess.run(["sips", "-Z", "1568", "-s", "format", "jpeg", str(path), "--out", str(small)],
                      capture_output=True).returncode == 0:
        data, mime = small.read_bytes(), "image/jpeg"
        small.unlink()
    else:  # no sips (not a Mac): send it as is
        data, mime = Path(path).read_bytes(), IMAGE.get(Path(path).suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def check_argo():
    argo("Reply with OK.", 5, timeout=20, tag="check")


def suggest(t, known):
    """Labels, due date and action items for a dropped email (or a cleaned title for a typed task).
    None if Argo didn't answer; {} if it answered nonsense."""
    now = datetime.now()
    labels = ("- labels: up to 3 short labels (1-2 words, Title Case). Reuse an existing label whenever one fits; "
              "invent a new one only if none does.\n")
    if t["eml"] or t.get("shot"):  # a screenshot reads like an email: its date, sender and asks
        prompt = (f"Today is {now:%A %Y-%m-%d %H:%M} (local time). Existing labels: {json.dumps(known)}.\n"
                  "Below is an email I saved as a to-do. Reply with ONLY a JSON object "
                  '{"labels": [...], "due": "...", "actions": [...]}.\n' + labels +
                  '- due: when I must act by, as local YYYY-MM-DDTHH:MM. Resolve relative dates ("by Friday") against the '
                  'email\'s own Date header; if only a date is given use 17:00. Use "" if the email states no deadline '
                  "(do not invent one).\n"
                  "- actions: if the email asks ME to do several distinct things, each one as a short imperative to-do "
                  "(max 12 words); otherwise [].\n\n"
                  f"Subject: {t['title']}\nFrom: {t['sender']}\nDate: {t['date']}\n\n{t['body'][:3000]}")
    else:
        prompt = (f"Today is {now:%A %Y-%m-%d %H:%M} (local time). Existing labels: {json.dumps(known)}.\n"
                  "Below is a to-do I typed. Reply with ONLY a JSON object "
                  '{"title": "...", "labels": [...], "due": "..."}.\n'
                  "- title: the task restated cleanly, without the date/time words, first letter capitalised.\n" + labels +
                  '- due: the deadline it mentions, as local YYYY-MM-DDTHH:MM resolved against today; if only a date is '
                  'given use 17:00. Use "" if it mentions no time (do not invent one).\n\n' + t["body"][:3000])
    reply = argo(prompt, 800, tag="new:" + t["id"])  # "new:": the page shows a status line, then scrolls to the task
    return None if reply is None else clean_suggestion(reply, now)


def new_input(fn):
    """Marks a new task's whole Argo job (read, then labels/due) as one busy "new:<id>" stretch, for the page's status line."""
    def run(t):
        tag = "new:" + t["id"]
        with busy_lock:
            busy[tag] += 1
        try:
            return fn(t)
        finally:
            with busy_lock:
                busy[tag] -= 1
    return run


@new_input
def enrich(t):
    """Background: fill in labels/due on a freshly dropped email, unless the user already set them."""
    s = suggest(t, all_labels())
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if s is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "enrich"
            return save(todos)
        cur.pop("retry", None)
        s = s or {"labels": [], "due": "", "title": "", "actions": []}  # nonsense reply: still give emails a default due
        if s["labels"] and not cur.get("labels"):
            cur["labels"] = sorted(set(s["labels"]))
        if s["due"] and not cur["due"]:
            cur["due"], cur["notified"] = s["due"], False
        if not cur["due"] and (t["eml"] or t.get("shot")):  # it states no deadline: 3 days after it was sent
            cur["due"], cur["notified"], cur["due_auto"] = default_due(cur), False, True
        if s["title"] and not t["eml"] and cur["title"] == t["title"]:  # typed task the user hasn't renamed
            cur["title"], cur["body"] = s["title"], "" if cur["body"] == t["title"] else cur["body"]
        if s["actions"] and not cur.get("actions"):
            cur["actions"] = s["actions"]
        save(todos)


def default_due(t):
    """17:00, 3 days after the email was sent (its Date header); when that's unreadable, after it was added."""
    try:
        sent = parsedate_to_datetime(t["date"]).astimezone().replace(tzinfo=None)  # in local time
    except (TypeError, ValueError, IndexError):
        sent = datetime.fromisoformat(t["added"])
    return (sent + timedelta(days=3)).strftime("%Y-%m-%dT17:00")


def blank():
    return {"id": uuid.uuid4().hex[:12], "done": False, "due": "", "notified": False, "note": "",
            "added": datetime.now().isoformat(timespec="minutes")}


def make_todo(raw, name):
    t = blank()
    tid = t["id"]
    ext = Path(name).suffix.lower()
    if ext in IMAGE:  # a screenshot: Argo reads it in the background
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{tid}{ext}").write_bytes(raw)
        t.update(title="Screenshot", sender="", date="", body="", eml=False, shot=ext)
        threading.Thread(target=read_shot, args=(t,), daemon=True).start()
        return t
    msg = message_from_bytes(raw, policy=policy.default)
    if name.lower().endswith(".eml") or (msg["From"] and msg["Subject"] is not None):
        (MAILS / f"{tid}.eml").write_bytes(raw)
        text = email_text(msg)
        t.update(title=str(msg["Subject"] or "(no subject)"), sender=str(msg["From"] or ""),
                 date=str(msg["Date"] or ""), body=text.strip()[:3000], eml=True)
    else:  # plain text (manual todo, or Outlook only handed over text)
        text = raw.decode("utf-8", "replace").strip()
        t.update(title=(text.splitlines() or ["(empty)"])[0][:200], sender="", date="",
                 body=text[:3000], eml=False)
    threading.Thread(target=enrich, args=(t,), daemon=True).start()
    return t


def clean_shot(text):
    """{"title", "sender", "date", "text"} from the model's reply; None if it isn't that JSON."""
    d = first_json(text)
    if d is None:
        return None
    return {k: str(d.get(k) or "").strip()[:3000 if k == "text" else 200] for k in ("title", "sender", "date", "text")}


@new_input
def read_shot(t):
    """Background: Argo reads a screenshot (an email, chat, calendar invite, web page...) into the task's title and text,
    then labels/due/action items are filled in as for any other task."""
    reply = argo("This is a screenshot I took to make a to-do from. Reply with ONLY a JSON object "
                 '{"title": "...", "sender": "...", "date": "...", "text": "..."}.\n'
                 "- title: what I need to do, as a short imperative to-do (max 12 words); if it's just a message, its subject.\n"
                 '- sender: who it is from (name <email> if shown), or "".\n'
                 '- date: when it was sent or posted, as shown, or "".\n'
                 "- text: all the readable text that matters (message body, deadlines, links, names), transcribed as "
                 "plain text; skip window chrome, menus and toolbars.", 3000, tag="new:" + t["id"], image=SHOTS / f"{t['id']}{t['shot']}")
    s = None if reply is None else clean_shot(reply)
    with lock:
        todos = load()
        cur = next((x for x in todos if x["id"] == t["id"]), None)
        if not cur or not argo_on():
            return
        if reply is None:  # Argo down: retry_argo() tries again later
            cur["retry"] = "shot"
            return save(todos)
        cur.pop("retry", None)
        if s:
            if cur["title"] == "Screenshot" and s["title"]:  # unless the user renamed it meanwhile
                cur["title"] = s["title"]
            cur.update(sender=s["sender"], date=s["date"], body=s["text"])
        save(todos)
        t = dict(cur)
    if s:
        enrich(t)


def retry_argo():
    """Redo Argo work that failed while it was down. Stops at the first failure: still down, try next round."""
    if not retry_busy.acquire(blocking=False):
        return
    try:
        kinds = {"enrich": enrich, "shot": read_shot}
        with lock:
            todos = load()
            # a kind this app no longer does (Scout's "summary"): drop the mark rather than redo the task as a typed to-do
            stale = [t for t in todos if t.get("retry") and t["retry"] not in kinds]
            for t in stale:
                t.pop("retry")
            if stale:
                save(todos)
            jobs = [(kinds[t["retry"]], t) for t in todos if t.get("retry") and not t["done"]]
        for fn, arg in jobs:
            fn(arg)
            if not argo_ok:
                break
        if argo_on() and time.time() - argo_at > 600:  # idle: ping it so the status in the UI stays current
            check_argo()
    finally:
        retry_busy.release()


def draft_reply(t, how=""):
    """Argo drafts a reply (following `how`, if given); it opens as a new Outlook message (never sent) and is copied to the clipboard."""
    msg = message_from_bytes((MAILS / f"{t.get('mail', t['id'])}.eml").read_bytes(), policy=policy.default)
    text = argo("Draft my reply to the email below. Write only the reply body: plain text, no subject line, no signature, "
                "match the sender's tone and language, keep it short."
                + (f" My notes on how to answer: {t['note']}" if t["note"] else "")
                + (f" Instructions for this reply: {how}" if how else "")
                + f"\n\nFrom: {t['sender']}\nDate: {t['date']}\nSubject: {msg['Subject']}\n\n{t['body']}", 1500, timeout=90, tag=t["id"])
    if not text:
        return None
    text = text.strip()
    subprocess.run(["pbcopy"], input=text.encode())
    subj = str(msg["Subject"] or "")
    subj = subj if subj.lower().startswith("re:") else "Re: " + subj
    url = (f"mailto:{urllib.parse.quote(parseaddr(str(msg['Reply-To'] or msg['From'] or ''))[1])}?"
           + urllib.parse.urlencode({"subject": subj, "body": text}, quote_via=urllib.parse.quote))
    if subprocess.run(["open", "-a", "Microsoft Outlook", url]).returncode:
        subprocess.run(["open", url])
    return text


STOP = set("a an the and or of to in on at for with about what who whom when where which how did does do is are was "
           "were be been i me my we our you your it its this that these those from by any all there have has had "
           "ask asked tell told say said".split())


def top_hits(items, q, hay, n=12):
    """The n items whose text (hay) mentions the question's keywords most."""
    words = [w for w in re.findall(r"\w+", q.lower()) if w not in STOP and len(w) > 2]
    # ponytail: keyword-overlap ranking over <= a few thousand items; embeddings if it misses obvious matches
    return sorted(items, key=lambda t: -sum(hay(t).lower().count(w) for w in words))[:n]


def ask(q):
    """Answer a question from saved emails/tasks: keyword-pick the likeliest ones, then let Argo read them."""
    with lock:
        todos = load()
    hits = top_hits(todos, q, lambda t: " ".join([t["title"], t["sender"], t["date"], t["body"], t["note"], *t.get("labels", [])]))
    ctx = "\n\n".join(f"[{t['title']}] from {t['sender'] or '-'} | {t['date'] or t['added']} | due {t['due'] or 'none'} | "
                       f"{'done' if t['done'] else 'open'}\n{t['body'][:1500]}" + (f"\nMy notes: {t['note']}" if t["note"] else "")
                       for t in hits)
    return argo(f"Today is {datetime.now():%A %Y-%m-%d}. Answer my question using only the saved emails/tasks below. "
                "Be brief and plain text; name the item (by its [subject]) the answer comes from. "
                f"If they don't contain the answer, say so.\n\nQuestion: {q}\n\n{ctx}", 2000, timeout=90, tag="ask")


def ask_mail(t, q):
    """Answer a question about one saved email."""
    return argo(f"Today is {datetime.now():%A %Y-%m-%d}. Answer my question about the email below. Be brief and plain text. "
                f"If the email doesn't say, say so.\n\nQuestion: {q}\n\nFrom: {t['sender']}\nDate: {t['date']}\n"
                f"Subject: {t['title']}\n\n{t['body']}", 1500, timeout=90, tag=t["id"])


def briefing():
    """Once a day after briefing_hour (default 8): one notification with what's overdue/due today and where to start."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < config().get("briefing_hour", 8) or (BRIEFED.exists() and BRIEFED.read_text() == today):
        return
    BRIEFED.write_text(today)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    with lock:
        todos = [t for t in load() if not t["done"] and t["due"]]
    late = sorted((t for t in todos if t["due"] <= stamp), key=lambda t: t["due"])
    due = sorted((t for t in todos if t["due"] > stamp and t["due"].startswith(today)), key=lambda t: t["due"])
    if not late and not due:
        return
    head = ", ".join(x for x in (f"{len(late)} overdue" if late else "", f"{len(due)} due today" if due else "") if x)
    lines = "\n".join(f"- {t['title']} (due {t['due']}{', OVERDUE' if t in late else ''})" for t in (late + due)[:30])
    def send():  # in its own thread, so a slow or down Argo never holds up due-date notifications
        tip = argo(f"It's {now:%A %H:%M}. These are my overdue and due-today tasks. In under 25 words, tell me which 3 to "
                   "start with (short names only). Plain text, no preamble.\n" + lines, 300, timeout=60, tag="briefing")
        say(f"Good morning: {head}", (tip or "Start with: " + "; ".join(t["title"] for t in (late + due)[:3])).strip())
    threading.Thread(target=send, daemon=True).start()


def say(title, text):
    """Speech bubble from the pet; a macOS notification if the Mac app isn't running."""
    for _ in range(20):  # right after launch the app hasn't polled yet: give it 10 s
        if pets_seen:
            break
        time.sleep(0.5)
    if time.time() - pets_seen > 60:
        return notify(title, text)
    with busy_lock:
        bubbles.append({"id": (bubbles[-1]["id"] + 1) if bubbles else 1, "title": title, "text": text})
        del bubbles[:-20]


def notify(title, msg):
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "default"',
                    "-e", "end run", title, msg])


def exit_with_parent(ppid):
    while os.getppid() == ppid:
        time.sleep(2)
    os._exit(0)


def watcher():
    last_retry = 0
    while True:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        with lock:
            todos, changed = load(), False
            for f in sorted(INBOX.glob("*.eml")):
                todos.insert(0, make_todo(f.read_bytes(), f.name))
                f.unlink()
                changed = True
            for t in todos:
                if t["due"] and not t["done"] and not t["notified"] and t["due"] <= now:
                    notify("Todo due", t["title"])
                    t["notified"] = changed = True
            if changed:
                save(todos)
        briefing()
        mins = config().get("retry_minutes", 10)  # 0 = automatic retry off (the header's Retry button still works)
        if mins and time.time() - last_retry > mins * 60:  # and right after launch
            last_retry = time.time()
            threading.Thread(target=retry_argo, daemon=True).start()
        time.sleep(20)


HUB_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HUB_FIELDS = ("title", "notes", "due", "done", "tags", "items", "from")
hub_kick, hub_busy = threading.Event(), threading.Lock()
hub_state = {"state": "", "at": 0.0, "who": ""}  # last pass: "ok" / "unreachable" / "refused" / "error" ("" = not yet),
# when, and whose list the token fills
hub_refused = None  # the (url, token) the hub answered 401 to: no more passes until Settings change either


def hub_conf():
    c = config()
    url, token = c.get("hub_url", ""), c.get("hub_token", "")
    return (url, token) if token and re.match(r"https?://", url) else None


def hub_status():
    conf = hub_conf()
    state = "off" if not conf else "refused" if conf == hub_refused else hub_state["state"]
    return {"state": state, "at": hub_state["at"], "who": hub_state["who"] if state == "ok" else ""}


def hub_fields(t):
    """What the hub gets for a task. Never the email body or a screenshot's text: those stay on the Mac."""
    notes = t.get("note", "").strip()
    if t.get("eml"):
        name, addr = parseaddr(t.get("sender", ""))
        line = "From " + (name or addr or "unknown sender")
        try:
            d = parsedate_to_datetime(t["date"]).astimezone()
            line += f", {d:%b} {d.day}, {d.year}"
        except (TypeError, ValueError, IndexError, KeyError):
            pass
        notes = line + ("\n\n" + notes if notes else "")
    return {"title": t["title"], "notes": notes, "due": (t.get("due") or "")[:10], "done": bool(t["done"]),
            "tags": list(t.get("labels", [])), "items": list(t.get("actions", [])), "from": "Survey Corps"}


def hub_sent(t, url):
    """What was last pushed of this task to the hub at `url`, or None. The record (hub_sent, hub_off, hub_bad) holds for
    the address in hub_at only: a task pushed to a test hub is a new item to the real one."""
    return t.get("hub_sent") if t.get("hub_at") == url else None


def hub_diff(t, url):
    """The fields that changed since this task was last pushed (all of them the first time), or None for nothing to send."""
    same = t.get("hub_at") == url
    if same and t.get("hub_off") or not HUB_KEY.match(t["id"]):
        return None
    sent = hub_sent(t, url)
    if sent is None and t["done"]:  # done before the sync knew it: nothing to tick off on the hub
        return None
    if t.get("shot") and t["title"] == "Screenshot":  # Argo is still reading it: wait for a real title
        with busy_lock:
            if t.get("retry") == "shot" or busy["new:" + t["id"]] > 0:
                return None
    d = {k: v for k, v in hub_fields(t).items() if sent is None or sent.get(k) != v}
    if not d or same and json.dumps(d, sort_keys=True) == t.get("hub_bad"):  # the hub turned exactly this down already
        return None
    return d


def hub_forget(key):
    """Queue a DELETE for a deleted task the hub has. Call under `lock`."""
    gone = json.loads(HUB_GONE.read_text()) if HUB_GONE.exists() else []
    if key not in gone:
        HUB_GONE.write_text(json.dumps(gone + [key]))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):  # a redirect would carry the token to another address: stop at the 3xx instead
        return None


hub_open = urllib.request.build_opener(NoRedirect).open


def hub_call(conf, method, key, body=None):
    """One request to the hub: its status code, or None when it can't be reached. urllib sends no Origin or Referer."""
    req = urllib.request.Request(f"{conf[0]}/api/push/tasks/{key}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + conf[1], "Content-Type": "application/json"})
    try:
        with hub_open(req, timeout=15) as r:
            d = json.loads(r.read() or b"{}")
            return 200 if isinstance(d, dict) and (method == "DELETE" or d.get("id")) else 502  # not the hub answering
    except urllib.error.HTTPError as e:
        # a 404 or 409 stops an item for good, so only the hub's own word counts, which it marks; any other server's
        # 404 (a mistyped address, a hub without these routes yet) is just an error
        return e.code if e.code not in (404, 409) or e.headers.get("X-Push-Answer") else 502
    except ValueError:  # a 200 that isn't JSON (some other web page), or a malformed address
        return 502
    except (OSError, http.client.HTTPException):  # URLError, timeout, refused, a dropped connection
        return None


def hub_who(conf):
    """Whose list the token fills: the name, or a status code, or None when the hub can't be reached. Asked before
    every pass, so a wrong address or token sends nothing, and Settings says so even when nothing is waiting."""
    req = urllib.request.Request(f"{conf[0]}/api/push/account", headers={"Authorization": "Bearer " + conf[1]})
    try:
        with hub_open(req, timeout=15) as r:
            d = json.loads(r.read() or b"{}")
            return d["name"] if isinstance(d, dict) and isinstance(d.get("name"), str) else 502
    except urllib.error.HTTPError as e:
        return e.code
    except ValueError:
        return 502
    except (OSError, http.client.HTTPException):
        return None


def hub_pass():
    """Push what changed to the member's private list on DrXAS Hub, and the queued deletes. One way: nothing comes back."""
    global hub_refused
    with hub_busy:
        conf = hub_conf()
        if not conf or conf == hub_refused:
            return
        url, who = conf[0], hub_who(conf)
        if not isinstance(who, str):
            if who == 401:
                hub_refused = conf
            hub_state.update(state="refused" if who == 401 else "unreachable" if who is None else "error",
                             at=time.time(), who="")
            return
        with lock:
            gone = json.loads(HUB_GONE.read_text()) if HUB_GONE.exists() else []
            # the first push of a task says so: only that one makes an item on the hub (see push_task there)
            jobs = [(t["id"], d, hub_sent(t, url) is None) for t in load() if (d := hub_diff(t, url)) is not None]
        state, deleted, results = "ok", [], {}
        for key in gone:
            code = hub_call(conf, "DELETE", key)
            if code == 401:
                hub_refused, state = conf, "refused"
                break
            if code in (200, 409):  # 409: it left the private list, so it isn't ours to delete any more
                deleted.append(key)
            elif code is None:
                state = "unreachable"
                break
            else:
                state = "error"
        for key, d, first in jobs if state in ("ok", "error") else ():
            code = hub_call(conf, "POST", key, {**d, "new": True} if first else d)
            if code == 401:
                hub_refused, state = conf, "refused"
                break
            if code is None:  # down or off the lab network: everything stays dirty for the next pass
                state = "unreachable"
                break
            if code == 400:
                print(f"hub turned down task {key}: {sorted(d)}", file=sys.stderr)
            elif code not in (200, 404, 409):
                state = "error"
            results[key] = (code, d)  # 5xx and the like: left alone below, so it stays dirty
        with lock:
            if deleted:
                left = [k for k in (json.loads(HUB_GONE.read_text()) if HUB_GONE.exists() else []) if k not in deleted]
                HUB_GONE.write_text(json.dumps(left))
            todos, ids = load(), set()
            for t in todos:  # re-read: only the hub_* keys are written, so edits made meanwhile survive
                code, d = results.get(t["id"], (0, None))
                ids.add(t["id"])
                if code in (200, 400, 404, 409) and t.get("hub_at") != url:  # this hub's first answer: a fresh record
                    for k in ("hub_sent", "hub_off", "hub_bad"):
                        t.pop(k, None)
                    t["hub_at"] = url
                if code == 200:
                    t["hub_sent"] = {**t.get("hub_sent", {}), **d}
                    t.pop("hub_bad", None)
                elif code in (404, 409):  # deleted in the hub, or moved off the private list: the hub's call now
                    t["hub_off"] = True
                elif code == 400:
                    t["hub_bad"] = json.dumps(d, sort_keys=True)
            for key, (code, _) in results.items():
                if code == 200 and key not in ids:  # deleted while its first push was on the way
                    hub_forget(key)
            if results:
                save(todos, kick=False)
        hub_state.update(state=state, at=time.time(), who=who)


def hub_sync():
    """Background: a pass ~2 s after tasks or settings are saved (so a burst of edits is one pass), and every 5 minutes."""
    while True:
        if hub_kick.wait(300):
            time.sleep(2)
        hub_kick.clear()
        try:
            hub_pass()
        except Exception as e:  # never let one bad pass end the thread
            print("hub sync:", e, file=sys.stderr)


def all_labels():
    """Every label in use on a task: the vocabulary Argo reuses and the add-label box suggests."""
    with lock:
        return sorted({l for t in load() for l in t.get("labels", [])})


def pets(after=0):
    """Everything the Mac app's pet shows: its overdue badge, whether Argo is working, new bubbles."""
    global pets_seen
    pets_seen = time.time()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    with lock:
        todos = load()
    with busy_lock:
        working = any(n > 0 and k != "check" for k, n in busy.items())
        said = [b for b in bubbles if b["id"] > after]
    return {"overdue": sum(1 for t in todos if not t["done"] and t["due"] and t["due"] <= now),
            "busy": working, "bubbles": said}


class H(BaseHTTPRequestHandler):
    def reply(self, code, body=b"{}", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/todos":
            with lock:
                self.reply(200, json.dumps(load()).encode())
        elif self.path == "/api/settings":
            self.reply(200, json.dumps(get_settings()).encode())
        elif self.path.startswith("/api/shot/"):  # a screenshot task's image
            f = next(SHOTS.glob(re.sub(r"\W", "", self.path.rsplit("/", 1)[1]) + ".*"), None)
            self.reply(200, f.read_bytes(), IMAGE.get(f.suffix, "application/octet-stream")) if f else self.reply(404)
        elif self.path == "/api/status":
            with busy_lock:
                working = sorted(k for k, n in busy.items() if n > 0)
            self.reply(200, json.dumps({"configured": argo_on(), "argo": argo_ok, "checked": argo_at, "busy": working,
                                        "retry_minutes": config().get("retry_minutes", 10), "hub": hub_status(),
                                        "rev": DB.stat().st_mtime if DB.exists() else 0}).encode())
        elif self.path.startswith("/api/pets"):  # /api/pets?after=<last bubble id seen>
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0] or 0)
            self.reply(200, json.dumps(pets(after)).encode())
        elif self.path == "/settings":
            self.reply(200, SETTINGS_PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.reply(404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, ORIGIN):  # block other websites
            return self.reply(403)
        u = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path == "/api/argo/check":
            check_argo()
            if argo_ok:
                threading.Thread(target=retry_argo, daemon=True).start()  # clicking the pill also clears the waiting queue
            return self.reply(200)
        if u.path == "/api/argo/retry":  # manual retry: ping first so a dead Argo fails fast, then redo the waiting work
            check_argo()
            if argo_ok:
                retry_argo()
            return self.reply(200)
        if u.path == "/api/settings":
            try:
                new = json.loads(raw or b"{}")
                set_settings(new)
            except (ValueError, TypeError):
                return self.reply(400)
            return self.reply(200)
        if u.path == "/api/ask":  # {"q"}: across all tasks, or {"q", "id"}: about that one email
            d = json.loads(raw or b"{}")
            with lock:
                t = next((x for x in load() if x["id"] == d.get("id")), None)
            if d.get("id") and not t:
                return self.reply(404)
            answer = ((ask_mail(t, d.get("q", "")) if t else ask(d.get("q", ""))) or "").replace("**", "")  # plain-text box
            return self.reply(200, json.dumps({"answer": answer}).encode()) if answer else self.reply(503)
        if u.path.startswith("/api/reply/"):
            with lock:
                t = next((x for x in load() if x["id"] == u.path.rsplit("/", 1)[1]), None)
            if not t or not t.get("eml"):
                return self.reply(404)
            text = draft_reply(t, str(json.loads(raw or b"{}").get("how") or "").strip())
            return self.reply(200, json.dumps({"draft": text}).encode()) if text else self.reply(503)
        parts = u.path.strip("/").split("/")  # api/drop | api/todo/<id>/<action>
        with lock:
            todos = load()
            if u.path == "/api/drop":
                t = make_todo(raw, parse_qs(u.query).get("name", [""])[0])
                todos.insert(0, t)
                save(todos)
                return self.reply(200, json.dumps({"title": t["title"], "id": t["id"]}).encode())
            elif len(parts) == 4 and parts[1] == "todo":
                t = next((x for x in todos if x["id"] == parts[2]), None)
                if not t:
                    return self.reply(404)
                if parts[3] == "update":
                    patch = json.loads(raw or b"{}")
                    for k in ("title", "due", "done", "note", "actions"):
                        if k in patch:
                            t[k] = patch[k]
                    if "labels" in patch:
                        t["labels"] = sorted({str(x).strip() for x in patch["labels"] if str(x).strip()})
                    if "due" in patch:
                        t["notified"] = False
                        t.pop("due_auto", None)  # the user set it
                elif parts[3] == "split":  # one suggested action item -> its own task, sharing the email
                    title = json.loads(raw or b"{}").get("title", "").strip()
                    t["actions"] = [a for a in t.get("actions", []) if a != title]
                    if title:
                        new = {**t, **blank(), "title": title, "actions": [], "due": t["due"], "retry": "",
                               "mail": t.get("mail", t["id"])}
                        todos.insert(0, {k: v for k, v in new.items() if not k.startswith("hub_")})
                elif parts[3] == "delete":
                    todos.remove(t)
                    # an open task may be on the hub though the answer to its first push was lost; a DELETE of
                    # nothing there is fine. Not a task done before sync was on: that one was never sent.
                    if not t.get("hub_off") and (t.get("hub_sent") is not None or hub_conf() and not t["done"]):
                        hub_forget(t["id"])
                    mid = t.get("mail", t["id"])
                    if not any(x.get("mail", x["id"]) == mid for x in todos):  # split-off tasks may share the email
                        (MAILS / f"{mid}.eml").unlink(missing_ok=True)
                        for f in SHOTS.glob(f"{mid}.*"):
                            f.unlink()
                elif parts[3] == "open":
                    eml = MAILS / f"{t.get('mail', t['id'])}.eml"
                    if subprocess.run(["open", "-a", "Microsoft Outlook", eml]).returncode:
                        subprocess.run(["open", eml])  # Outlook missing: default mail app
            else:
                return self.reply(404)
            save(todos)
        self.reply(200)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Task Manager</title>
<style>
:root{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e2e2e7;--hover:#f0f0f4;
  --accent:#0a84ff;--red:#d70015;--redbg:#ff3b3014;--chipL:32%}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;
  --hover:#323236;--red:#ff6961;--redbg:#ff453a1f;--chipL:76%}}
*{box-sizing:border-box}
body{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:var(--bg);color:var(--text)}
button,input,select,textarea{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 85%,transparent);backdrop-filter:blur(12px);
  padding:14px 20px 10px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
h1{font-size:20px;margin:0;font-weight:700}
#stats{color:var(--muted)}
.bar{display:flex;gap:8px}#shot{opacity:1;width:34px;height:auto;border:1px solid var(--line);background:var(--card);border-radius:8px}
#shot[hidden]{display:none}
.shotimg{display:block;max-width:100%;max-height:320px;margin-top:8px;border:1px solid var(--line);border-radius:10px;cursor:zoom-in}
.bar input,.bar select{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
.bar input:focus,.bar select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff33}
#new,#q{flex:1}.bar{flex-wrap:wrap;row-gap:3px}
#status{margin-top:6px;padding:6px 12px;border-radius:8px;font-size:13px;background:color-mix(in srgb,var(--accent) 12%,var(--card));
  border:1px solid color-mix(in srgb,var(--accent) 35%,transparent)}
#status[hidden]{display:none}#status.done{background:color-mix(in srgb,#30d158 14%,var(--card));border-color:color-mix(in srgb,#30d158 40%,transparent)}
.t.flash{animation:flash 2.4s ease-out}@keyframes flash{0%,30%{box-shadow:0 0 0 3px var(--accent)}100%{box-shadow:0 0 0 0 transparent}}.bar .brk{flex-basis:100%;height:0}
main{padding:6px 20px 40px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:18px 4px 6px;font-weight:600}
h2 span{font-weight:400;margin-left:4px}h2.late{color:var(--red)}
.empty{text-align:center;color:var(--muted);padding:60px 0;font-size:15px}
.t{display:flex;gap:10px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;margin:6px 0;transition:background .1s}
.t:hover{background:var(--hover)}
.t{flex-wrap:wrap;row-gap:0}.t>details{flex:0 0 calc(100% - 28px);margin-left:28px;min-width:0}  /* full-width row, lined up with the title */
.t.late{border-left:3px solid var(--red)}
.cb{appearance:none;flex:none;width:18px;height:18px;margin:2px 0 0;border:1.5px solid var(--muted);border-radius:50%;cursor:pointer;display:grid;place-content:center}
.cb:hover{border-color:var(--accent)}
.cb:checked{background:var(--accent);border-color:var(--accent)}
.cb:checked::after{content:"";width:5px;height:9px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.main{flex:1;min-width:0}
.title{width:100%;border:0;background:none;font-weight:600;font-size:14px;padding:0;outline:none;text-overflow:ellipsis}
.title:focus{text-overflow:clip}
.done .title{text-decoration:line-through;color:var(--muted)}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:3px;font-size:12px}
.info{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
#lbar{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.lb{border:1px solid transparent;border-radius:10px;padding:1px 9px;font-size:12px;cursor:pointer;font-weight:500;
  background:hsl(var(--h,210) 70% 50% / .14);color:hsl(var(--h,210) 65% var(--chipL))}
.lb b{font-weight:400;opacity:.6;margin-left:5px}.lb:hover{border-color:hsl(var(--h,210) 65% 50% / .5)}
.lb.on{background:hsl(var(--h,210) 70% 50%);color:#fff}.lb.on b{opacity:.85}
.lb:first-child{background:none;border-color:var(--line);color:var(--muted)}.lb:first-child.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.chips{display:contents}.chip{border-radius:10px;padding:1px 8px;cursor:pointer;background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL));font-weight:500}
.chip b{display:inline-block;vertical-align:bottom;max-width:0;overflow:hidden;font-weight:400;opacity:0}.chip:hover b{max-width:1em;margin-left:3px;opacity:.7}.chip b:hover{opacity:1}
.addl{border:0;background:none;width:0;padding:1px 0;color:var(--muted);outline:none;opacity:0}
.t:hover .addl,.addl:focus{opacity:1;width:58px;padding:1px 2px}.t .addl:focus,.c .addl:focus{width:180px;color:var(--text)}
details summary{display:inline-block;font-size:12px;color:var(--muted);cursor:pointer;margin-top:4px;list-style:none;user-select:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"›";display:inline-block;margin-right:5px;transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 -apple-system,sans-serif;max-height:280px;overflow:auto;
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0 0}
.quoted{display:block;margin-top:10px;padding-left:10px;border-left:2px solid var(--line);color:var(--muted)}
.more{border:0;background:none;color:var(--accent);cursor:pointer;padding:0;margin-top:8px;font-size:12px}
.lbl{display:block;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:12px 0 4px}
pre a{color:var(--accent)}
textarea{width:100%;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:8px 10px;resize:none;outline:none;overflow:hidden;min-height:38px}
textarea:focus,.askm:focus{border-color:var(--accent)}
.side{flex:none;display:flex;align-items:center;gap:2px}
.due{border:0;border-radius:6px;padding:3px 8px;background:none;color:var(--muted);cursor:pointer;font-size:12px;white-space:nowrap}
.due.set{background:var(--bg);color:var(--text)}.late .due.set{background:var(--redbg);color:var(--red);font-weight:600}
.due:hover{background:var(--line)}.due.unset{opacity:0}.t:hover .due.unset{opacity:1}
.due b{font-weight:400;margin-left:4px;opacity:0}.due:hover b{opacity:.6}.due b:hover{opacity:1}
.side input{background:var(--card);border:1px solid var(--accent);border-radius:6px;padding:2px 6px;font-size:12px}
[hidden]{display:none!important}  /* .icon/.pill set display, which would otherwise beat the hidden attribute */
.icon{border:0;background:none;width:28px;height:28px;border-radius:6px;color:var(--muted);cursor:pointer;display:grid;place-content:center;opacity:.55}
.t:hover .icon{opacity:1}.icon:hover{background:var(--line);color:var(--text)}.icon.x:hover{color:var(--red)}
.icon svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
#donebox{margin-top:18px}#donebox>summary{font-size:12px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin:0 4px 6px}
.done{opacity:.65}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--card);padding:6px 12px;cursor:pointer}.seg .on{background:var(--accent);color:#fff}
.calhd{display:flex;align-items:center;gap:8px;margin:14px 0 8px}.calhd h3{margin:0;font-size:16px;flex:1}
.calhd button{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:4px 10px;cursor:pointer}
.calhd small{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.dow{background:var(--bg);padding:4px 8px;font-size:11px;color:var(--muted);text-transform:uppercase}
.day{background:var(--card);min-height:96px;padding:4px;cursor:pointer;overflow:hidden}
.day.out{background:var(--bg)}.day.out .dn{opacity:.5}.day.sel{box-shadow:inset 0 0 0 2px var(--accent)}.day.over{background:var(--hover)}
.dn{font-size:12px;color:var(--muted);margin:0 2px 2px}.day.today .dn{background:var(--accent);color:#fff;border-radius:9px;padding:0 6px;display:inline-block}
.ev{font-size:11.5px;border-radius:4px;padding:1px 5px;margin:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:grab;
  background:hsl(var(--h) 70% 50% / .18);color:hsl(var(--h) 65% var(--chipL))}
.ev.late{background:var(--redbg);color:var(--red);font-weight:600}.ev.done{text-decoration:line-through;opacity:.55}
.more{font-size:11px;color:var(--muted);padding:0 5px}
.askm{width:100%;box-sizing:border-box;background:var(--bg);border:1px solid transparent;border-radius:8px;padding:7px 10px;outline:none}
details .askm{border-color:color-mix(in srgb,var(--accent) 35%,transparent)}
.rbox{display:flex;gap:6px;margin-top:6px;align-items:flex-end}.rbox[hidden]{display:none}
.rbox .askm{margin:0;flex:1;font:inherit;resize:vertical;min-height:5.5em;max-height:60vh;line-height:1.4}.rgo,.rno{height:32px}
.rgo,.rno{border:0;border-radius:8px;padding:0 12px;background:var(--accent);color:#fff;cursor:pointer;white-space:nowrap}
.rno{background:var(--line);color:var(--text)}
.askans{white-space:pre-wrap;margin-top:6px;padding:6px 10px;border-left:3px solid var(--accent);font-size:13px}
.ai{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin-top:5px;font-size:12px;color:var(--muted)}
.act{border:1px dashed var(--line);border-radius:10px;padding:1px 8px;cursor:pointer;color:var(--text)}
.act:hover{border-color:var(--accent);color:var(--accent)}.act b{font-weight:400;margin-left:4px;opacity:.5}.act b:hover{opacity:1;color:var(--red)}
.icon.busy{opacity:1;animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.25}}
#ans{position:relative;background:var(--card);border:1px solid var(--accent);border-radius:10px;padding:10px 32px 10px 12px;margin:12px 0 0;white-space:pre-wrap}
#ans>button{position:absolute;top:6px;right:6px;border:0;background:none;cursor:pointer;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:10;background:var(--text);color:var(--bg);
  border-radius:8px;padding:8px 14px;font-size:12.5px;opacity:0;transition:opacity .2s;pointer-events:none}#toast.on{opacity:.92}
.pill[hidden]{display:none}.pill+.pill{margin-left:6px}.pill{margin-left:auto;align-self:center;display:flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
  border-radius:12px;padding:2px 10px;font-size:12px;color:var(--muted);cursor:pointer}.pill:hover{border-color:var(--accent)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.pill.ok .dot{background:#34c759}.pill.down{color:var(--red)}.pill.down .dot{background:var(--red)}
.pill.busy{color:var(--accent)}.pill.busy .dot{background:none;border:2px solid var(--accent);border-right-color:transparent;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.ev.thinking{outline:1.5px solid var(--accent);animation:pulse 1s infinite}
.argoing{display:none;color:var(--accent)}.t.thinking .argoing{display:inline}
.t.thinking{border-color:var(--accent);background:linear-gradient(90deg,var(--card) 30%,color-mix(in srgb,var(--accent) 12%,var(--card)) 50%,var(--card) 70%) 0 0/300% 100%;
  animation:shimmer 1.8s linear infinite}@keyframes shimmer{from{background-position:100% 0}to{background-position:0 0}}
#drop{display:none;position:fixed;inset:8px;z-index:9;pointer-events:none;align-items:center;justify-content:center;
  border:3px dashed var(--accent);border-radius:14px;background:#0a84ff1a;color:var(--accent);font-size:20px;font-weight:600}
#drop.over{display:flex}
</style>
<div id=drop>Drop email to add a task</div><div id=toast></div>
<header>
  <div class=top><h1>Task Manager</h1><span id=stats></span><button id=argo class=pill></button>
    <button id=rt class=pill hidden title="Retry the work Argo missed, now">Retry now</button></div>
  <div class=bar>
    <input id=new placeholder="Add a task (&quot;review Bob's draft by Fri 3pm&quot;), or drag in an email or image">
    <button id=shot class=icon hidden title="Screenshot to task: drag over an area (Space picks a window, Esc cancels). Already have one copied? Paste it here with ⌘V."><svg viewBox="0 0 24 24"><path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/></svg></button>
    <div class=brk></div><input id=q type=search placeholder="Search, or ask a question ending in ?  ( / )">
    <span class=seg><button id=vl>List</button><button id=vc>Calendar</button></span>
  </div>
  <div id=status hidden></div>
  <div id=lbar></div>
</header>
<datalist id=labels></datalist>
<main><div id=ans hidden></div><div id=cal hidden></div><div id=list></div><details id=donebox><summary id=donehdr></summary><div id=donelist></div></details></main>
<script>
const api=(p,b)=>fetch(p,{method:'POST',body:typeof b==='string'||b instanceof Blob?b:JSON.stringify(b)}).then(load);
const nowStr=()=>new Date(Date.now()-new Date().getTimezoneOffset()*6e4).toISOString().slice(0,16);
const DAY=864e5, dayDiff=d=>Math.round((new Date(d).setHours(0,0,0,0)-new Date().setHours(0,0,0,0))/DAY);
const MAIL='<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6 8.5-6"/></svg>';
const REPLY='<svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v4"/></svg>';
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('on'),4000)}
const TRASH='<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M5.5 7l1 12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';
function fmtDue(s){
  const d=new Date(s),n=dayDiff(s),time=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
  if(n===0)return 'Today '+time; if(n===1)return 'Tomorrow '+time; if(n===-1)return 'Yesterday '+time;
  if(n>1&&n<7)return d.toLocaleDateString([],{weekday:'short'})+' '+time;
  return d.toLocaleDateString([],{month:'short',day:'numeric',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined})+' '+time;
}
function group(t){
  if(!t.due)return 'No due date'; if(t.due<=nowStr())return 'Overdue';
  const n=dayDiff(t.due); return n===0?'Today':n===1?'Tomorrow':n<7?'Next 7 days':'Later';
}
const GROUPS=['Overdue','Today','Tomorrow','Next 7 days','Later','No due date'];
function senderName(s){return (s||'').replace(/<[^>]*>/,'').replace(/"/g,'').trim()||s||''}
function shortDate(s){const d=new Date(s);return isNaN(d)?s:d.toLocaleDateString([],{month:'short',day:'numeric',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined})}
function item(t){
  const d=document.createElement('div'),late=t.due&&!t.done&&t.due<=nowStr();
  d.className='t'+(t.done?' done':'')+(late?' late':'')+(isBusy(t.id)?' thinking':''); d.dataset.id=t.id;
  d.innerHTML=`<input type=checkbox class=cb title="Mark done">
    <div class=main><input class=title type=text spellcheck=false>
      <div class=meta><span class=argoing>✨ Argo is working on this…</span><span class=info></span><span class=chips></span><input class=addl list=labels placeholder="+ label"></div>
      <div class=rbox hidden><textarea class=askm rows=4 placeholder="Reply instructions or the points to make (optional, e.g. &quot;decline politely&quot;). ⌘Enter drafts, Esc cancels."></textarea><button class=rgo>Draft in Outlook</button><button class=rno>Cancel</button></div>
</div>
    <div class=side><button class=due></button><input type=datetime-local hidden>
      <button class="icon r" title="Draft a reply with Argo">${REPLY}</button><button class="icon o" title="Open in Outlook">${MAIL}</button><button class="icon x" title="Delete">${TRASH}</button></div>
      <details><summary>Email & notes</summary><pre></pre>
        <div class=askw><span class=lbl>✨ Ask Argo</span><input class=askm placeholder="e.g. What's the deadline? (press Enter)"><div class=askans hidden></div></div>
        <span class=lbl>Notes</span><textarea rows=1 placeholder="Your notes (Argo also uses these when drafting a reply)…"></textarea></details>`;
  const $=s=>d.querySelector(s), up=p=>api(`/api/todo/${t.id}/update`,p);
  const cb=$('.cb'); cb.checked=t.done; cb.onchange=()=>up({done:cb.checked});
  const ti=$('.title'); ti.value=t.title; ti.title=t.title; ti.onchange=()=>up({title:ti.value});
  ti.onkeydown=e=>{if(e.key==='Enter')ti.blur()};
  // friendly due label; the raw date input only appears while editing
  const db=$('.due'),du=$('.side input');
  if(t.due){db.className='due set';db.textContent=fmtDue(t.due);db.title=t.due.replace('T',' ')+(t.due_auto?'\nNo deadline in the email: set to 3 days after it was sent':'');
    const c=document.createElement('b');c.textContent='×';c.title='Clear due date';
    c.onclick=e=>{e.stopPropagation();up({due:''})};db.append(c)}
  else{db.className='due unset';db.textContent='Set due'}
  du.value=t.due; du.onchange=()=>{if(du.value)up({due:du.value})};
  db.onclick=()=>{db.hidden=true;du.hidden=false;du.focus();try{du.showPicker()}catch{}};
  du.onblur=()=>{if(du.value===t.due){du.hidden=true;db.hidden=false}};
  const r=$('.r'); r.hidden=!t.eml;
  const rb=$('.rbox'),ri=$('.rbox textarea');
  ri.oninput=()=>{ri.style.height='auto';ri.style.height=ri.scrollHeight+2+'px'};
  r.onclick=()=>{if(r.disabled)return;rb.hidden=!rb.hidden;if(!rb.hidden)ri.focus()};
  ri.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();draft()}if(e.key==='Escape'){rb.hidden=true;ri.blur()}};
  $('.rgo').onclick=()=>draft(); $('.rno').onclick=()=>rb.hidden=true;
  const draft=async()=>{if(r.disabled)return;const how=ri.value.trim();rb.hidden=true;ri.blur();
    r.disabled=true;r.classList.add('busy');toast('Drafting a reply…');
    const res=await fetch(`/api/reply/${t.id}`,{method:'POST',body:JSON.stringify({how})}).catch(()=>({}));r.disabled=false;r.classList.remove('busy');
    load();toast(res.ok?'Draft opened in Outlook (also copied to the clipboard)':'Argo is unavailable right now (down, or not on VPN). Try again later.')};
  if(t.actions?.length){
    const a=document.createElement('div');a.className='ai';a.append('Action items:');
    for(const s of t.actions){
      const b=document.createElement('span');b.className='act';b.textContent='+ '+s;b.title='Add as its own task';
      b.onclick=()=>api(`/api/todo/${t.id}/split`,{title:s});
      const x=document.createElement('b');x.textContent='×';x.title='Dismiss';
      x.onclick=e=>{e.stopPropagation();up({actions:t.actions.filter(y=>y!==s)})};
      b.append(x);a.append(b);
    }
    $('.meta').after(a);
  }
  const am=$('details .askm'),aa=$('.askans'); $('.askw').hidden=!t.body;
  am.onkeydown=async e=>{const v=am.value.trim();if(e.key!=='Enter'||!v)return;
    aa.hidden=false;aa.textContent='Asking Argo…';
    const res=await fetch('/api/ask',{method:'POST',body:JSON.stringify({q:v,id:t.id})}).catch(()=>({}));
    aa.textContent=res.ok?(await res.json()).answer:'Argo is unavailable right now (down, or not on VPN). Try again later.'};
  const o=$('.o'); o.hidden=!t.eml; o.onclick=()=>api(`/api/todo/${t.id}/open`);
  $('.x').onclick=()=>confirm(`Delete "${t.title}"?`)&&api(`/api/todo/${t.id}/delete`);
  const info=$('.info'); info.textContent=[senderName(t.sender),t.date&&shortDate(t.date)].filter(Boolean).join(' · ');
  info.title=[t.sender,t.date].filter(Boolean).join('\n');
  const labels=t.labels||[];
  for(const l of labels){
    const c=document.createElement('span');c.className='chip';c.style.setProperty('--h',hue(l));
    c.textContent=l;c.title='Show only this label';c.onclick=()=>{lf=l;render()};
    const x=document.createElement('b');x.textContent='×';x.title='Remove label';
    x.onclick=e=>{e.stopPropagation();up({labels:labels.filter(y=>y!==l)})};
    c.append(x);$('.chips').append(c);
  }
  const al=$('.addl'); al.onchange=()=>{const v=al.value.trim();if(v)up({labels:[...labels,v]})};
  const hasBody=t.body&&t.body!==t.title, pre=$('pre'); if(hasBody)showMail(pre,t.body); else pre.remove();
  const n=$('textarea'); n.value=t.note; n.onchange=()=>up({note:n.value});
  const grow=()=>{n.style.height='auto';n.style.height=n.scrollHeight+2+'px'}; n.oninput=grow;
  $('details').ontoggle=grow;
  if(t.shot){const a=document.createElement('a'),i=document.createElement('img');a.href=i.src=`/api/shot/${t.mail||t.id}`;a.target='_blank';
    i.className='shotimg';i.title='Open full size';a.append(i);$('details summary').after(a)}
  $('summary').textContent=(t.shot?'Screenshot & notes':hasBody?'Email & notes':'Notes')+(t.note?' ✎':'');
  return d;
}
// Outlook Safe Links / urldefense wrap every URL; show the real one, keep the wrapped href for the security scan
function pretty(u){
  try{const x=new URL(u);if(x.hostname.endsWith('safelinks.protection.outlook.com'))u=x.searchParams.get('url')||u}catch{}
  const d=u.match(/urldefense\.\w+\/v3\/__(.+?)__;/);if(d)u=d[1];
  return u.length>90?u.slice(0,87)+'…':u;
}
// display only (Argo still reads the full text): drop Outlook's external-sender banners, fold the quoted thread
function showMail(el,text){
  text=text.replace(/^(This Message Is From an External Sender|This message came from outside your organization\.?)\s*$/gim,'').replace(/\n{3,}/g,'\n\n').trim();
  const q=text.search(/^(From: .+|On .+wrote:|-{2,} ?Original Message ?-{2,})$/m);
  if(q<=0)return linkify(el,text);
  linkify(el,text.slice(0,q).trim());
  const old=document.createElement('span');old.className='quoted';old.hidden=true;linkify(old,text.slice(q));
  const b=document.createElement('button');b.className='more';b.textContent='Show earlier messages';
  b.onclick=()=>{old.hidden=!old.hidden;b.textContent=old.hidden?'Show earlier messages':'Hide earlier messages'};
  el.append(document.createElement('br'),b,old);
}
function linkify(el,text){
  const re=/(?:https?:\/\/|mailto:)[^\s<>"]*[^\s<>".,;)]/g;let i=0,m;
  while((m=re.exec(text))){
    el.append(text.slice(i,m.index));
    const a=document.createElement('a');a.href=m[0];a.target='_blank';a.title=m[0];a.textContent=pretty(m[0]);
    el.append(a);i=re.lastIndex;
  }
  el.append(text.slice(i));
}
const hue=s=>[...s].reduce((h,c)=>(h*31+c.charCodeAt(0))%360,7);  // each label its own colour, the same every time
let all=[], view=localStorage.view||'list', cm=new Date(), sel='', dragId=null;
cm.setDate(1);
const ymd=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
function renderCal(shown){
  const el=(tag,cls,txt)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
  const by={};for(const t of shown.filter(t=>t.due).sort((a,b)=>a.due.localeCompare(b.due)))(by[t.due.slice(0,10)]||=[]).push(t);
  const y=cm.getFullYear(),m=cm.getMonth(),lead=new Date(y,m,1).getDay(),days=new Date(y,m+1,0).getDate();
  const go=n=>{cm=n?new Date(y,m+n,1):new Date(new Date().getFullYear(),new Date().getMonth(),1);render()};
  const hd=el('div','calhd'),title=el('h3',0,new Date(y,m,1).toLocaleDateString([],{month:'long',year:'numeric'}));
  const undated=shown.filter(t=>!t.due&&!t.done).length;
  hd.append(title,...(undated?[el('small',0,`${undated} without a due date`)]:[]));
  for(const [l,n] of [['‹',-1],['Today',0],['›',1]]){const b=el('button',0,l);b.onclick=()=>go(n);hd.append(b)}
  const grid=el('div','grid');
  for(const d of ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'])grid.append(el('div','dow',d));
  const today=ymd(new Date());
  for(let i=0;i<Math.ceil((lead+days)/7)*7;i++){
    const d=new Date(y,m,1-lead+i),k=ymd(d),c=el('div','day'+(d.getMonth()!==m?' out':'')+(k===today?' today':'')+(k===sel?' sel':''));
    c.append(el('div','dn',d.getDate()));
    const ts=by[k]||[];
    for(const t of ts.slice(0,3)){
      const e=el('div','ev'+(t.done?' done':'')+(!t.done&&t.due<=nowStr()?' late':''),t.title);
      e.style.setProperty('--h',(t.labels||[])[0]?hue(t.labels[0]):210);e.title=t.title+'\n'+fmtDue(t.due);
      e.dataset.id=t.id;if(isBusy(t.id))e.classList.add('thinking');
      e.draggable=true;e.ondragstart=ev=>{dragId=t.id;ev.dataTransfer.setData('application/x-todo',t.id)};e.ondragend=()=>dragId=null;
      c.append(e);
    }
    if(ts.length>3)c.append(el('div','more',`+${ts.length-3} more`));
    c.onclick=()=>{sel=sel===k?'':k;render()};
    c.ondragover=e=>{if(dragId){e.preventDefault();c.classList.add('over')}};
    c.ondragleave=()=>c.classList.remove('over');
    c.ondrop=e=>{if(!dragId)return;e.preventDefault();e.stopPropagation();c.classList.remove('over');
      const t=all.find(x=>x.id===dragId);dragId=null;if(t&&t.due.slice(0,10)!==k)api(`/api/todo/${t.id}/update`,{due:k+t.due.slice(10)})};
    grid.append(c);
  }
  const out=[hd,grid];
  if(sel){const ts=by[sel]||[];out.push(el('h2',0,new Date(sel+'T00:00').toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'})),
    ...(ts.length?ts.map(item):[el('div','empty','Nothing due this day.')]))}
  cal.replaceChildren(...out);
}
let lf='';  // label filter ('' = all), set by the label row under the search bar or by clicking a label
function render(){
  const names=[...new Set(all.flatMap(t=>t.labels||[]))].sort();
  labels.replaceChildren(...names.map(n=>new Option(n)));
  if(!names.includes(lf))lf='';
  const lchip=(name,text,n)=>{const c=document.createElement('button');c.className='lb'+(lf===name?' on':'');
    if(name)c.style.setProperty('--h',hue(name));c.textContent=text;if(n){const b=document.createElement('b');b.textContent=n;c.append(b)}
    c.onclick=()=>{lf=lf===name?'':name;render()};return c};
  const undone=all.filter(t=>!t.done);
  lbar.replaceChildren(lchip('','All labels'),...names.map(n=>lchip(n,n,undone.filter(t=>(t.labels||[]).includes(n)).length||'')));
  const words=q.value.trim().endsWith('?')?[]:q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const shown=all.filter(t=>(!lf||(t.labels||[]).includes(lf))&&words.every(w=>
    [t.title,t.sender,t.body,t.note,...(t.labels||[])].join(' ').toLowerCase().includes(w)));
  const open=shown.filter(t=>!t.done).sort((a,b)=>(a.due||'z').localeCompare(b.due||'z'));
  const done=shown.filter(t=>t.done);
  const out=[];
  for(const g of GROUPS){
    const ts=open.filter(t=>group(t)===g); if(!ts.length)continue;
    const h=document.createElement('h2');h.className=g==='Overdue'?'late':'';h.textContent=g;
    const c=document.createElement('span');c.textContent=ts.length;h.append(c);
    out.push(h,...ts.map(item));
  }
  if(!open.length){
    const e=document.createElement('div');e.className='empty';
    e.textContent=all.some(t=>!t.done)?'No tasks match your search.':'All clear. Drag an email from Outlook here to add a task.';
    out.push(e);
  }
  list.replaceChildren(...out); donelist.replaceChildren(...done.map(item));
  const isCal=view==='cal'; cal.hidden=!isCal; list.hidden=isCal; vl.className=isCal?'':'on'; vc.className=isCal?'on':'';
  if(isCal)renderCal(shown);
  donebox.hidden=isCal||!done.length; donehdr.textContent=`Done · ${done.length}`;
  const openAll=all.filter(t=>!t.done), late=openAll.filter(t=>t.due&&t.due<=nowStr()).length;
  stats.textContent=`${openAll.length} open`+(late?` · ${late} overdue`:'');
  document.title=`Task Manager (${openAll.length})`;
}
let st={busy:[]}, rev=0;
async function load(){
  [all,st]=await Promise.all(['/api/todos','/api/status'].map(u=>fetch(u).then(r=>r.json())));rev=st.rev;render();pill()}
function pill(){  // Argo status in the header: off / reachable / unavailable / working
  const work=st.busy.filter(x=>x!=='check'), checking=st.busy.includes('check'), waiting=all.filter(t=>t.retry&&!t.done).length;
  const ago=st.checked?Math.round(Date.now()/6e4-st.checked/60):null;
  argo.className='pill'+(!st.configured?'':work.length||checking?' busy':st.argo?' ok':st.argo===false?' down':'');
  const dot=document.createElement('span');dot.className='dot';
  argo.replaceChildren(dot,!st.configured?'Argo off':work.length?`Argo working${work.length>1?` on ${work.length}`:''}…`
    :checking?'Checking Argo…':st.argo===false?'Argo unavailable'+(waiting?` · ${waiting} waiting`:''):'Argo');
  rt.hidden=!st.configured||!waiting;rt.textContent=`Retry now · ${waiting}`;
  rt.title=`Retry the work Argo missed, now: ${waiting} task${waiting===1?'':'s'}`;
  argo.title=!st.configured?'LLM features are off. Set "argo_user" in data/config.json to turn them on.'
    :(st.argo===false?'Argo is down, or you are off VPN. Waiting work retries '+(st.retry_minutes?'every '+st.retry_minutes+' min':'only when you press Retry now')+'.':st.argo?'Argo is reachable.':'Not checked yet.')
    +(ago!=null?` Last contact ${ago<1?'just now':ago<60?ago+' min ago':Math.round(ago/60)+' h ago'}.`:'')+' Click to check now.';
}
async function tick(){
  try{st=await (await fetch('/api/status')).json()}catch{return}
  pill();
  for(const e of document.querySelectorAll('[data-id]'))e.classList.toggle('thinking',isBusy(e.dataset.id));
  // new inputs (typed, dropped, pasted, screenshots): a status line while Argo reads them, then jump to the finished task
  const now=st.busy.filter(b=>b.startsWith('new:')).map(b=>b.slice(4)), ae=document.activeElement,
    idle=(ae===document.body||ae.id==='new'&&!ae.value)&&!document.querySelector('.t details[open]');  // just pressed Enter in the add box counts as idle
  for(const id of reading)if(!now.includes(id))arrived.push(id);
  reading=now;
  if(reading.length){const t=all.find(t=>t.id===reading[0]);
    showStatus(`✨ Argo is reading ${!t?'a new task':t.shot&&t.title==='Screenshot'?'your screenshot':`“${t.title}”`}…`+(reading.length>1?` (+${reading.length-1} more)`:''))}
  else if(!arrived.length&&!statusEl.classList.contains('done'))statusEl.hidden=true;
  if(arrived.length&&idle){await load();jump(arrived.splice(0))}
  // data changed on the server (LLM fill-ins, retries): reload, unless the user is in the middle of something
  else if(st.rev!==rev&&idle)load();
}
const isBusy=id=>st.busy.includes(id)||st.busy.includes('new:'+id);
let reading=[], arrived=[];
const statusEl=document.getElementById('status');  // not window.status: that's a built-in string
function showStatus(text,done){statusEl.textContent=text;statusEl.classList.toggle('done',!!done);statusEl.hidden=false;
  clearTimeout(showStatus.t);if(done)showStatus.t=setTimeout(()=>{statusEl.hidden=true;statusEl.classList.remove('done')},4000)}
function jump(ids){  // scroll to the last finished task and flash it
  const t=all.find(t=>t.id===ids.at(-1)), e=document.querySelector(`.t[data-id="${ids.at(-1)}"]`);
  if(t)showStatus(`✓ Ready: “${t.title}”`+(ids.length>1?` and ${ids.length-1} more`:''),true);
  if(!e)return;  // filtered out, or in the calendar view
  e.scrollIntoView({block:'center',behavior:'smooth'});e.classList.remove('flash');void e.offsetWidth;e.classList.add('flash');
}
argo.onclick=()=>{fetch('/api/argo/check',{method:'POST'}).then(tick);setTimeout(tick,150)};
rt.onclick=()=>{rt.disabled=true;fetch('/api/argo/retry',{method:'POST'}).then(()=>{rt.disabled=false;tick();load()});setTimeout(tick,150)};

setInterval(tick,2000);
q.oninput=render;
vl.onclick=()=>{view='list';localStorage.view=view;render()};vc.onclick=()=>{view='cal';localStorage.view=view;render()};
document.getElementById('new').onkeydown=e=>{
  const v=e.target.value.trim();
  if(e.key==='Enter'&&v){e.target.value='';api('/api/drop?name=todo.txt',v)}
  if(e.key==='Escape')e.target.blur();
};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}});
function answer(text){const b=document.createElement('button');b.textContent='×';b.title='Close';b.onclick=()=>ans.hidden=true;
  ans.replaceChildren(text,b);ans.hidden=false}
q.onkeydown=async e=>{
  if(e.key==='Escape'){q.value='';ans.hidden=true;render();q.blur()}
  const v=q.value.trim();
  if(e.key==='Enter'&&v.endsWith('?')){
    answer('Asking Argo…');
    const r=await fetch('/api/ask',{method:'POST',body:JSON.stringify({q:v})}).catch(()=>({}));
    load();answer(r.ok?(await r.json()).answer:'Argo is unavailable right now (down, or not on VPN). Try again later.');
  }
};
addEventListener('dragover',e=>{e.preventDefault();if(!dragId)drop.classList.add('over')});
addEventListener('dragleave',e=>{if(!e.relatedTarget)drop.classList.remove('over')});  // only when leaving the window
addEventListener('drop',async e=>{
  e.preventDefault(); if(dragId)return; drop.classList.remove('over');
  const files=[...e.dataTransfer.files];
  for(const f of files) await fetch('/api/drop?name='+encodeURIComponent(f.name),{method:'POST',body:f});
  const txt=!files.length&&(e.dataTransfer.getData('text/plain')||e.dataTransfer.getData('text/uri-list'));
  if(txt) await fetch('/api/drop?name=drop.txt',{method:'POST',body:txt});
  load();
});
// screenshots: the camera button (Mac app: it hides the window and runs the area picker), or ⌘V an image copied with ⌃⇧⌘4
const shotBtn=document.getElementById('shot');shotBtn.hidden=!window.webkit?.messageHandlers?.shot;
shotBtn.onclick=()=>webkit.messageHandlers.shot.postMessage('');
addEventListener('paste',async e=>{
  const f=[...e.clipboardData.files].find(f=>f.type.startsWith('image/'));if(!f)return;e.preventDefault();
  toast('Reading the screenshot…');
  await fetch('/api/drop?name=paste.'+(f.type.split('/')[1]||'png'),{method:'POST',body:f});load();
});
setInterval(()=>{if(!document.querySelector('.t details[open]')&&document.activeElement===document.body)load()},60000);  // due-date groups move with the clock
load();
</script>"""

SETTINGS_PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Settings</title>
<style>
:root{color-scheme:light dark;--bg:#ececec;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#dcdce0;--accent:#0a84ff;--red:#d70015;--green:#248a3d}
@media (prefers-color-scheme:dark){:root{--bg:#1e1e1e;--card:#2a2a2d;--text:#f2f2f7;--muted:#98989f;--line:#3a3a3d;--red:#ff6961;--green:#30d158}}
*{box-sizing:border-box}
body{font:13px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:18px 20px;background:var(--bg);color:var(--text);-webkit-user-select:none;cursor:default}
h2{font-size:13px;font-weight:600;margin:16px 0 6px}h2:first-child{margin-top:0}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px}
.r{display:flex;align-items:center;gap:12px;padding:8px 12px;min-height:38px}.r+.r{border-top:1px solid var(--line)}
.r>label{flex:1}.r small{display:block;color:var(--muted);font-size:11.5px}
input,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 7px;width:220px;-webkit-user-select:text}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:6px;padding:3px 12px}
.ok{color:var(--green)}.bad{color:var(--red)}#res{font-size:12px}
.foot{color:var(--muted);font-size:11.5px;margin-top:12px;display:flex;justify-content:space-between}
</style>
<h2>Argo</h2>
<div class=group>
  <div class=r><label for=argo_user>ANL username<small>Blank turns all LLM features off</small></label><input id=argo_user autocomplete=off spellcheck=false></div>
  <div class=r><label for=argo_model>Model</label><input id=argo_model spellcheck=false placeholder=claudesonnet55></div>
  <div class=r><label for=retry_minutes>Retry missed work<small>When Argo was down or off VPN</small></label><select id=retry_minutes></select></div>
  <div class=r><label>Connection<small id=res>&nbsp;</small></label><button id=test>Test Connection</button></div>
</div>
<h2>DrXAS Hub</h2>
<div class=group>
  <div class=r><label for=hub_url>Hub address<small>Tasks go one way to your My Lists; email text never leaves the Mac</small></label><input id=hub_url spellcheck=false autocomplete=off placeholder=http://drxas.xray.aps.anl.gov:1234></div>
  <div class=r><label for=hub_token>Token<small id=tokstate>From the hub: My Lists → Connect an app</small></label><input id=hub_token type=password autocomplete=off spellcheck=false><button id=tokclear hidden>Clear</button></div>
  <div class=r><label>Sync<small id=hubres>&nbsp;</small></label></div>
</div>
<h2>Notifications</h2>
<div class=group>
  <div class=r><label for=briefing_hour>Morning briefing<small>What's overdue and due today, once a day</small></label><select id=briefing_hour></select></div>
</div>
<div class=foot><span>Changes save automatically.</span><span id=msg></span></div>
<script>
const $=id=>document.getElementById(id), post=(u,b)=>fetch(u,{method:'POST',body:JSON.stringify(b)});
const fill=(sel,opts)=>{for(const [v,l] of opts)sel.add(new Option(l,v))};
fill($('retry_minutes'),[[0,'Never (use Retry now)'],[5,'Every 5 min'],[10,'Every 10 min'],[30,'Every 30 min'],[60,'Every hour']]);
fill($('briefing_hour'),[[24,'Off'],...[...Array(24).keys()].map(h=>[h,'Daily after '+h+':00'])]);
async function load(){
  const c=await (await fetch('/api/settings')).json();
  for(const k in c){const e=$(k);if(e&&document.activeElement!==e)e.value=c[k]}
  $('hub_token').placeholder=c.hub_token_set?'Set (paste a new one to replace)':'Paste token';
  $('tokclear').hidden=!c.hub_token_set;
  hubStatus();
}
async function hubStatus(){  // one line: when it last synced, or why it can't
  const h=(await (await fetch('/api/status')).json()).hub, e=$('hubres'), m=Math.round((Date.now()/1000-h.at)/60);
  const t={off:'Off',refused:'Hub refused the token',unreachable:"Can't reach the hub",error:'The hub had a problem; will retry',
    ok:'Synced'+(h.who?' to '+h.who+"'s list":'')+' '+(m<1?'just now':m<60?m+' min ago':Math.round(m/60)+' h ago'),
    '':'Not synced yet'}[h.state];
  e.className=h.state==='ok'?'ok':['refused','unreachable','error'].includes(h.state)?'bad':'';e.textContent=t;
}
let pending=Promise.resolve();
async function save(b){  // macOS style: every change is saved as soon as it's made
  const p=post('/api/settings',b);pending=p;const r=await p, m=$('msg');
  m.className=r.ok?'ok':'bad';m.textContent=r.ok?'Saved':'Could not save';setTimeout(()=>m.textContent='',1500);
  load();setTimeout(hubStatus,3000);  // the sync pass runs ~2 s after a save
}
for(const e of document.querySelectorAll('input,select'))e.onchange=()=>save({[e.id]:e.value});
$('hub_token').onchange=e=>{const v=e.target.value.trim();e.target.value='';if(v)save({hub_token:v})};  // never shown again
$('tokclear').onclick=()=>save({hub_token:''});
setInterval(hubStatus,10000);
$('test').onclick=async()=>{
  const res=$('res');res.className='';res.textContent='Checking…';
  await pending;  // a just-edited username saves (on blur) before the check
  await post('/api/argo/check',{});
  const st=await (await fetch('/api/status')).json();
  res.className=st.argo?'ok':'bad';
  res.textContent=!st.configured?'No username set':st.argo?'Argo is reachable':'Unreachable: Argo is down, or you are off VPN';
};
window.onfocus=load;load();
</script>"""


if __name__ == "__main__":
    for d in (MAILS, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=hub_sync, daemon=True).start()
    if "--no-browser" in sys.argv:  # started by the Mac app: if it's force-quit or crashes, go too
        threading.Thread(target=exit_with_parent, args=(os.getppid(),), daemon=True).start()
    print(f"Survey Corps on {ORIGIN}  (data in {DATA}, Finder fallback inbox: {INBOX})")
    if "--no-browser" not in sys.argv:  # the Mac app passes this and shows its own window
        subprocess.run(["open", "-a", "Safari", ORIGIN])  # Chromium browsers drop Outlook drags
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
