import tempfile
from datetime import datetime
from pathlib import Path
import app
from app import clean_suggestion


def isolate():  # every file the app reads or writes goes to a fresh temp folder, never the real data/
    d = Path(tempfile.mkdtemp())
    app.DATA, app.DB, app.CONFIG, app.BRIEFED = d, d / "todos.json", d / "config.json", d / "briefed"
    app.MAILS, app.INBOX, app.SHOTS = d / "mails", d / "inbox", d / "shots"
    app.HUB_GONE = d / "hub_deletes.json"


isolate()
now = datetime(2026, 10, 17, 9, 0)
assert clean_suggestion('x {"labels": ["A", " B ", "", "C", "D"], "due": "2026-10-20T17:00"} y', now) == {"labels": ["A", "B", "C"], "due": "2026-10-20T17:00", "title": "", "actions": []}
assert clean_suggestion('{"title": " Review draft ", "actions": ["only one"]}', now) == {"labels": [], "due": "", "title": "Review draft", "actions": []}  # 1 action = no split
assert clean_suggestion('{"actions": ["Send slides", "", "Book room"]}', now)["actions"] == ["Send slides", "Book room"]
assert clean_suggestion('{"labels": [], "due": "2026-10-01T17:00"}', now)["due"] == ""  # past
assert clean_suggestion('{"due": "next friday"}', now)["due"] == ""                      # malformed
assert clean_suggestion("no json", now) == {"labels": [], "due": "", "title": "", "actions": []}
assert app.first_json('x {bad} {"a": 1} {"b": 2}') == {"a": 1} and app.first_json("no json [1]") is None

# Argo down -> task marked for retry; Argo back -> retry fills it in and clears the mark
app.config = lambda: {"argo_user": "x"}
app.ARGO = "http://127.0.0.1:9/"  # nothing listens here: connection refused
t = {**app.blank(), "title": "review draft by fri", "body": "review draft by fri", "sender": "", "date": "", "eml": False}
app.save([t])
app.enrich(t)
assert app.load()[0]["retry"] == "enrich" and app.argo_ok is False and not +app.busy  # busy count released on failure
def back(prompt, max_tokens, **kw):
    app.argo_ok = True
    return '{"title": "Review draft", "labels": ["Review"], "due": ""}'
app.argo = back
app.retry_argo()
x = app.load()[0]
assert "retry" not in x and x["title"] == "Review draft" and x["labels"] == ["Review"] and x["body"] == "", x
assert app.all_labels() == ["Review"]
legacy = {**app.blank(), "title": "Read: A paper", "body": "", "sender": "", "date": "", "eml": False, "retry": "summary"}  # Scout's, gone
app.save([legacy])
app.retry_argo()
x = app.load()[0]
assert "retry" not in x and x["title"] == "Read: A paper" and not x.get("labels"), x  # mark dropped, task left as it was

# Settings: saved to config.json, blank text = default, unknown keys and default values aren't stored
app.config = lambda: __import__("json").loads(app.CONFIG.read_text()) if app.CONFIG.exists() else {}  # undo earlier patch
app.set_settings({"argo_user": " bob ", "retry_minutes": "30", "bogus": 1, "argo_model": app.MODEL})
c = app.get_settings()
assert c == {"argo_user": "bob", "argo_model": app.MODEL, "retry_minutes": 30, "briefing_hour": 8, "hub_url": "",
             "hub_token_set": False}, c
assert "argo_model" not in app.config() and "bogus" not in app.config()
app.set_settings({"retry_minutes": 9999, "argo_user": ""})
assert app.config() == {"retry_minutes": 1440}, app.config()
print("ok")

# the pet: /api/pets sums it up (overdue badge, busy, new bubbles); ask about one email
app.save([{**app.blank(), "title": "x", "due": "2020-01-01T00:00"}])
s = app.pets()
assert (s["overdue"], s["busy"]) == (1, False), s
app.say("Good morning: 1 overdue", "Start with: x")
assert app.pets()["bubbles"][-1]["text"] == "Start with: x" and app.pets(after=app.bubbles[-1]["id"])["bubbles"] == []
seen = []
app.argo = lambda prompt, n, **kw: seen.append(prompt) or "Friday."
assert app.ask_mail({**app.blank(), "title": "Beamtime", "body": "Slot is Friday", "sender": "Bob", "date": ""}, "when?") == "Friday."
assert "Question: when?" in seen[-1] and "Slot is Friday" in seen[-1]
print("ok")

# screenshot -> task: Argo's JSON fills title/sender/text, then the usual labels/due step runs; Argo down -> retry later
import struct, zlib
def png(w, h):
    chunk = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + b"\xff" * 3 * w for _ in range(h)))) + chunk(b"IEND", b""))
app.SHOTS = Path(tempfile.mkdtemp())
cfg, app.config = app.config, lambda: {"argo_user": "x"}
(app.SHOTS / "big.png").write_bytes(png(3000, 200))
u = app.data_url(app.SHOTS / "big.png")
assert u.startswith("data:image/jpeg;base64,") and not list(app.SHOTS.glob("*.send.jpg")), u[:40]  # shrunk, temp removed
assert app.clean_shot('Sure: {"title": "Book travel", "text": "Flight by Fri"}') == {"title": "Book travel", "sender": "", "date": "", "text": "Flight by Fri"}
assert app.clean_shot("no json here") is None
t = {**app.blank(), "title": "Screenshot", "sender": "", "date": "", "body": "", "eml": False, "shot": ".png"}
(app.SHOTS / f"{t['id']}.png").write_bytes(png(2, 2))
app.save([t])
app.argo = lambda prompt, n, **kw: None
app.read_shot(t)
assert app.load()[0]["retry"] == "shot"
seen = []
app.argo = lambda prompt, n, **kw: seen.append(kw.get("image")) or '{"title": "Book travel", "sender": "Ann", "text": "Flight by Fri"}'
app.read_shot(t)
x = app.load()[0]
assert (x["title"], x["sender"], x["body"], "retry" in x) == ("Book travel", "Ann", "Flight by Fri", False), x
assert seen[0] == app.SHOTS / f"{t['id']}.png" and seen[1] is None  # image for the read, plain text for labels/due
app.config = cfg
# main model errors -> the backup model answers
import http.server, importlib, json, threading
importlib.reload(app)  # undo the earlier patches of app.argo / app.config
isolate()              # ...which also put the paths back on the real data/
asked = []
class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        m = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"]
        asked.append(m)
        body = b'{"error": "no such model"}' if m == app.MODEL else b'{"choices": [{"message": {"content": "hi"}}]}'
        self.send_response(400 if m == app.MODEL else 200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
app.ARGO, app.config = f"http://127.0.0.1:{srv.server_port}/", lambda: {"argo_user": "x"}
assert app.argo("hello", 5) == "hi" and asked == [app.MODEL, app.BACKUP], asked
srv.shutdown()
# no deadline in an email -> due 17:00, 3 days after its Date header (or after it was added, if unreadable)
assert app.default_due({"date": "Tue, 20 Oct 2026 09:14:00 -0500", "added": "2026-10-24T10:00"}).startswith("2026-10-23T17:00")
assert app.default_due({"date": "Tue Oct 20, 2026 9:14 AM", "added": "2026-10-24T10:00"}) == "2026-10-23T17:00"  # screenshot-style
assert app.default_due({"date": "yesterday", "added": "2026-10-24T10:00"}) == "2026-10-27T17:00"
print("ok")

# DrXAS Hub sync: one way, only changed fields, never the email text
hits, answer, whos = [], {}, []  # requests the fake hub got: (method, key, headers, body); answer: key -> status to give
class Hub(http.server.BaseHTTPRequestHandler):
    def go(self):
        key = self.path.rsplit("/", 1)[1]
        n = int(self.headers.get("Content-Length") or 0)
        hits.append((self.command, key, dict(self.headers), json.loads(self.rfile.read(n)) if n else None))
        code = answer.get(key, answer.get("*", 200))
        plain = code == "404plain"  # a 404 the hub didn't mark: some other server's, or a route it doesn't have
        code = 404 if plain else code
        self.send_response(code)
        if code in (404, 409) and not plain:  # the hub marks the answers that stop an item for good
            self.send_header("X-Push-Answer", "gone" if code == 404 else "left")
        self.end_headers()
        self.wfile.write(json.dumps({"id": "h-" + key, "created": True} if code == 200 else {"error": "x"}).encode())
    def do_GET(self):  # /api/push/account, asked before every pass
        whos.append(self.headers.get("Authorization"))
        code = answer.get("account", answer.get("*", 200))
        self.send_response(code); self.end_headers()
        self.wfile.write(json.dumps({"name": "Bob"} if code == 200 else {"detail": "x"}).encode())
    do_POST = do_DELETE = go
    def log_message(self, *a): pass
hub = http.server.HTTPServer(("127.0.0.1", 0), Hub)
threading.Thread(target=hub.serve_forever, daemon=True).start()
HUB = f"http://127.0.0.1:{hub.server_port}"
hub2 = http.server.HTTPServer(("127.0.0.1", 0), Hub)  # a second hub (the real one after a test one): same handler
threading.Thread(target=hub2.serve_forever, daemon=True).start()
HUB2 = f"http://127.0.0.1:{hub2.server_port}"
class Other(http.server.BaseHTTPRequestHandler):  # a web server that isn't the hub: 404 for everything
    def go(self):
        hits.append((self.command, "other", {}, None)); self.send_response(404); self.end_headers(); self.wfile.write(b"<h1>Not Found</h1>")
    do_GET = do_POST = do_DELETE = go
    def log_message(self, *a): pass
other = http.server.HTTPServer(("127.0.0.1", 0), Other)
threading.Thread(target=other.serve_forever, daemon=True).start()
OTHER = f"http://127.0.0.1:{other.server_port}"
app.config = lambda: json.loads(app.CONFIG.read_text()) if app.CONFIG.exists() else {}
app.CONFIG.unlink(missing_ok=True)
by = lambda tid: next(x for x in app.load() if x["id"] == tid)
def edit(tid, **kw):
    todos = app.load()
    next(x for x in todos if x["id"] == tid).update(kw)
    app.save(todos)
def run():  # one pass; the requests it made
    n = len(hits)
    app.hub_pass()
    return hits[n:]
mail = {**app.blank(), "title": "Beamtime slot", "sender": "Ann Lee <ann@lab.gov>", "date": "Tue, 20 Oct 2026 12:00:00 +0000",
        "body": "SECRET mail text", "eml": True, "note": "bring slides", "labels": ["Beamtime"], "actions": ["Book room", "Send plan"],
        "due": "2026-10-30T17:00"}
typed = {**app.blank(), "title": "Order nitrogen", "sender": "", "date": "", "body": "Order nitrogen", "eml": False}
shot = {**app.blank(), "title": "Screenshot", "sender": "", "date": "", "body": "SECRET screen text", "eml": False, "shot": ".png",
        "retry": "shot"}
old = {**app.blank(), "title": "Long done", "sender": "", "date": "", "body": "", "eml": False, "done": True}
app.save([mail, typed, shot, old])
assert run() == [] and app.hub_status()["state"] == "off"                 # nothing set: off
app.set_settings({"hub_url": HUB + "/"})
assert run() == [] and app.hub_status()["state"] == "off"                 # an address without a token: still off
app.set_settings({"hub_token": " tok123 "})
c = app.get_settings()
assert c["hub_url"] == HUB and c["hub_token_set"] is True and "tok123" not in json.dumps(c), c  # the page never gets it back
first = run()
assert sorted(h[1] for h in first) == sorted([mail["id"], typed["id"]]), first  # not the screenshot Argo is reading, not old done
m = next(h for h in first if h[1] == mail["id"])
assert m[0] == "POST" and m[3] == {"title": "Beamtime slot", "notes": "From Ann Lee, Oct 20, 2026\n\nbring slides", "due": "2026-10-30",
                                   "done": False, "tags": ["Beamtime"], "items": ["Book room", "Send plan"], "from": "Survey Corps",
                                   "new": True}, m  # only a first push says new
assert "new" not in by(mail["id"])["hub_sent"]
assert m[2]["Authorization"] == "Bearer tok123" and m[2]["Content-Type"] == "application/json"
assert "Origin" not in m[2] and "Referer" not in m[2], m[2]
assert next(h for h in first if h[1] == typed["id"])[3]["notes"] == ""
assert by(mail["id"])["hub_sent"]["title"] == "Beamtime slot" and app.hub_status()["state"] == "ok"
assert app.hub_status()["who"] == "Bob" and whos[-1] == "Bearer tok123"  # asked whose list before sending
assert run() == []                                                         # nothing changed: nothing sent
edit(mail["id"], title="Beamtime slot (Fri)")
assert [h[3] for h in run()] == [{"title": "Beamtime slot (Fri)"}]         # only what changed
edit(mail["id"], done=True)
assert [h[3] for h in run()] == [{"done": True}]
edit(shot["id"], retry="", title="Renew badge")                            # Argo has read the screenshot
assert [(h[1], h[3]["title"], h[3]["notes"]) for h in run()] == [(shot["id"], "Renew badge", "")]
# 409: moved off the private list in the hub -> never pushed again
answer[typed["id"]] = 409
edit(typed["id"], note="two tanks")
assert len(run()) == 1 and by(typed["id"])["hub_off"] is True
edit(typed["id"], note="three tanks")
assert run() == []
# 404: deleted in the hub -> not brought back, never pushed again
typed2 = {**app.blank(), "title": "Call the vendor", "sender": "", "date": "", "body": "", "eml": False}
app.save(app.load() + [typed2])
assert [h[3].get("new") for h in run()] == [True]
answer[typed2["id"]] = 404
edit(typed2["id"], title="Call the vendor today")
assert [h[3] for h in run()] == [{"title": "Call the vendor today"}] and by(typed2["id"])["hub_off"] is True
assert app.hub_status()["state"] == "ok" and run() == []
# 400: marked for that payload, no spinning
answer[shot["id"]] = 400
edit(shot["id"], due="2026-11-02T09:00")
assert [h[3] for h in run()] == [{"due": "2026-11-02"}] and run() == []
del answer[shot["id"]]
# a 404 the hub didn't mark is not its word that the item has gone: an error, and the task stays live
answer[shot["id"]] = "404plain"
edit(shot["id"], note="badge photo")
assert len(run()) == 1 and app.hub_status()["state"] == "error" and not by(shot["id"]).get("hub_off")
del answer[shot["id"]]
assert [h[3].get("notes") for h in run()] == ["badge photo"] and app.hub_status()["state"] == "ok"
# a wrong address (some other server) sends nothing at all and says so
app.set_settings({"hub_url": OTHER})
edit(shot["id"], note="badge photo, front")
assert [h[0] for h in run()] == ["GET"] and app.hub_status()["state"] == "error" and not by(shot["id"]).get("hub_off")
app.set_settings({"hub_url": HUB})
assert [h[3].get("notes") for h in run()] == ["badge photo, front"]
# delete and split, through the app's own routes (a throwaway server on a free port)
import urllib.request
srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
api = lambda path, body=b"{}": urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", data=body))
api(f"/api/todo/{mail['id']}/split", json.dumps({"title": "Send plan"}).encode())
new = app.load()[0]
assert new["title"] == "Send plan" and not any(k.startswith("hub_") for k in new), new  # its own item on the hub
sent = {h[1]: h[3] for h in run()}
assert sent[mail["id"]] == {"items": ["Book room"]} and sent[new["id"]]["title"] == "Send plan" and len(sent[new["id"]]) == 8, sent
assert sent[new["id"]]["new"] is True
api(f"/api/todo/{old['id']}/delete")                                        # done before sync: never sent, no DELETE
lost = {**app.blank(), "title": "Answer lost", "sender": "", "date": "", "body": "", "eml": False}
app.save(app.load() + [lost], kick=False)                                   # open, never confirmed (say the reply was lost)
api(f"/api/todo/{lost['id']}/delete")
assert json.loads(app.HUB_GONE.read_text()) == [lost["id"]]
assert [(h[0], h[1]) for h in run()] == [("DELETE", lost["id"])] and json.loads(app.HUB_GONE.read_text()) == []
api(f"/api/todo/{mail['id']}/delete")
assert json.loads(app.HUB_GONE.read_text()) == [mail["id"]]
app.set_settings({"hub_url": "http://127.0.0.1:9"})                         # hub down: the delete waits
assert run() == [] and app.hub_status()["state"] == "unreachable" and json.loads(app.HUB_GONE.read_text()) == [mail["id"]]
app.set_settings({"hub_url": HUB})
assert [(h[0], h[1]) for h in run()] == [("DELETE", mail["id"])] and json.loads(app.HUB_GONE.read_text()) == []
srv.shutdown()
# 401: stop until the settings change
answer["*"] = 401
edit(new["id"], note="v2")
assert run() == [] and app.hub_status()["state"] == "refused" and app.hub_status()["who"] == ""  # stopped at "whose list?"
assert run() == []
del answer["*"]
app.set_settings({"hub_token": "tok456"})
assert [h[3] for h in run()] == [{"notes": "From Ann Lee, Oct 20, 2026\n\nv2"}] and app.hub_status()["state"] == "ok"
assert hits and all("SECRET" not in json.dumps(h[3]) for h in hits)      # the email and screenshot text never went
# another hub (a test one, then the real one): every live task is a new item there, whatever the first one said
answer.clear()  # the second hub has said nothing about any of them
app.set_settings({"hub_url": HUB2})
moved = [h for h in run() if h[0] == "POST"]
assert moved and all(h[3].get("new") is True and len(h[3]) == 8 for h in moved), moved
assert by(typed2["id"])["hub_at"] == HUB2 and not by(typed2["id"]).get("hub_off")  # turned off by the first hub only
assert run() == []
app.set_settings({"hub_token": ""})
assert app.get_settings()["hub_token_set"] is False and run() == [] and app.hub_status()["state"] == "off"
hub.shutdown(); hub2.shutdown(); other.shutdown()
print("ok")
