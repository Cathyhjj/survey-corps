# Survey Corps

A small local task manager for macOS that turns Outlook emails into to-dos.

> **This app is for my personal use.** It is shared as-is, with no support, and is tailored to my own setup
> (a work MacBook with Outlook for Mac). Feel free to read or borrow from the code.

This is a fork of [yijiang1/survey-corps](https://github.com/yijiang1/survey-corps), reduced to its email feature: emails (and screenshots and typed notes) become tasks, with one pet, Erwin, to drop them on.

## Features

**Emails to tasks**
- Drag an email from Outlook and drop it anywhere in the window, or on Erwin, to create a task. The subject, sender, date and body are saved, along with a copy of the original email.
- **Open in Outlook** button re-opens the saved email in Outlook.
- Links in email bodies are clickable. Outlook Safe Links / URL Defense wrappers are unwrapped for display (the click still goes through the scanner) and open in your default browser.
- Optional: auto-fill labels and a due date for each dropped email using an LLM (Argonne's Argo gateway; needs the lab network/VPN). Turn it on in Settings with your ANL username (or `data/config.json`: `{"argo_user": "<ANL username>"}`, optional `"argo_model"`, default `claudesonnet55`; if that model errors, it retries once on `claudesonnet5`). If an email (or screenshot) states no deadline, it's due at 5 PM three days after it was sent (hover the due date to see that it was a default). While Argo reads a new task, a status line shows under the search bar; when it's done, the list scrolls to the task and flashes it. It never overwrites labels or a due date you set, and does nothing if the gateway is unreachable. The email text is sent to that gateway.
- When Argo is down or you're off VPN, the work it missed waits: the Argo pill in the header says how much, **Retry now** redoes it, and it's retried automatically every few minutes (Settings). Click the pill to check the connection.
- **Screenshots to tasks** (Argo's vision model): click the camera button next to the add box, or right-click Erwin → Screenshot to Task…. The app hides, you drag over an area (Space picks a window, Esc cancels), and Argo reads the image into a task: title, sender, date, the text, plus labels, due date and action items. You can also drop an image file on the window or on Erwin, or ⌘V an image (e.g. one copied with ⌃⇧⌘4). The screenshot shows under **Screenshot & notes**; images live in `data/shots/` and are sent to Argo. The first capture asks for Screen Recording permission (System Settings → Privacy & Security).
- Add plain tasks by typing in the box at the top and pressing Enter. With Argo, plain language works: "review Bob's draft by Fri 3pm" becomes the task "Review Bob's draft", due Friday 15:00, with labels.
- When an email asks for several things, Argo lists them as **action items** under the task; click one to make it its own task (it shares the email).
- **Draft reply** (arrow button): type optional instructions in the field that opens on the card ("decline politely"), then **Draft in Outlook**; Argo writes the reply following them and your notes on the task. It opens as a new Outlook message for you to edit and send, and is also copied to the clipboard. Nothing is sent automatically. It's a new message, not a threaded reply; to keep the thread, open the email, hit Reply and paste.
- **Ask about one email**: under **Email & notes**, type a question in the ✨ Ask Argo box and press Enter ("what's the deadline?"). The email view hides Outlook's external-sender banners and folds the quoted thread behind **Show earlier messages**.
- **Ask**: type a question ending in `?` in the search box and press Enter ("what did Bob ask me about the beamtime?"), or right-click Erwin → Ask About My Tasks…. Argo answers from your saved emails and tasks.
- **Morning briefing**: once a day after 8:00 (set the hour, or turn it off, in Settings), Erwin says what's overdue and due today, and which three to start with.

**Settings** (Survey Corps menu → Settings…, or ⌘,), in its own window; changes save as you make them: Argo username and model, how often to retry work Argo missed, the DrXAS Hub address and token (below), and the morning briefing time (or off). Saved to `data/config.json`.

**Organising**
- Due dates with friendly labels ("Tomorrow 12:33 AM", "Fri 11:26 PM"). Tasks are grouped into Overdue, Today, Tomorrow, Next 7 days, Later and No due date, or shown on a month calendar (drag a task to another day to move its due date).
- Labels: create new ones or reuse existing ones; each gets its own colour. Click a label to filter by it.
- Search across title, sender, email body, notes and labels (press `/` to focus, `Esc` to clear).
- Notes on each task, editable titles, and a collapsible Done section.

**macOS integration**
- macOS notification when a task comes due.
- Native app with a Dock icon, light and dark mode.
- **Erwin**, a floating pet (an original chibi character holding a letter), sits on every desktop: click him to show/hide the app, drag him to move him. A red badge counts overdue tasks.
- **Drop things on Erwin**: an email, an image or text makes a task. He leans in while you drag, hops when it worked and shakes when it didn't.
- **Busy**: a "…" thought bubble while Argo works, and letters fly out of his envelope and turn into ticks.
- **Speech bubbles**: the morning briefing, answers and what a drop added appear above him (click one to open the app), with a sound (Glass; Funk when something failed). Turn the sounds off under right-click → Sounds. Without the Mac app they fall back to macOS notifications.
- **Right-click** (or ctrl-click) Erwin: Quick Add Task…, Screenshot to Task…, Ask About My Tasks…. Answers come back as a bubble.
- **Placing and hiding**: dropped within 24 pt of a screen edge, he snaps to it. **Tuck Into the Edge When the Mouse Is Away** (right-click) slides him into the nearer side edge after 3 s, leaving a tab with his face and the overdue count showing. If another screen touches that edge, he uses the far side instead (then the bottom, then the top), so he never ends up on the other screen. He slides out when the mouse comes near or he speaks. **Hide Erwin** hides him; ⇧⌘P (Survey Corps menu), the Dock icon or a new speech bubble brings him back.

**Sync to DrXAS Hub** (optional)
- Pushes your tasks one way into your private list (**My Lists**) on DrXAS Hub. It's off until both the hub address (e.g. `http://drxas.xray.aps.anl.gov:1234`) and a token are set in Settings. Get the token on the hub: **My Lists → Connect an app**. Settings shows only whether a token is set (paste a new one to replace it, or Clear), and a line saying whose list it fills and when it last synced, or why it can't. Before each pass it asks the hub whose list the token fills, so a mistyped address or a wrong token sends nothing.
- What is sent, per task: its title, due date (the day only), done, labels, remaining action items (a checklist on the hub), and a note. For an email the note is "From <sender>, <date>" plus your own note on the task; otherwise just your note. **The email text never leaves the Mac, nor does a screenshot or its text.** A screenshot isn't sent until Argo has given it a title, and a task that was already done when sync was turned on isn't sent at all.
- It's one-way: ticks and edits made in the hub are not copied back, and a field you change here overwrites the hub's copy of that field (fields you didn't change here are left alone). Deleting a task here deletes it on the hub. If you delete an item in the hub, or move it off your private list there, this app stops sending it rather than bringing it back. Point it at a different hub address (from a test hub to the real one, say) and every open task goes there as a new item.
- Only what changed is sent, about 2 s after a change and every 5 minutes; when the hub can't be reached it simply waits for the next pass. What was last sent is remembered in `data/todos.json`, and deletes still owed to the hub in `data/hub_deletes.json`.
- The hub is plain HTTP on the lab network (VPN off site), so the token and the fields above travel unencrypted there.

**Privacy**
- Everything stays on the Mac. Data lives in `data/` next to `app.py` (ignored by git); nothing is sent to any cloud service. Only what's described above goes to Argo, and only the task fields listed under Sync to DrXAS Hub go to the hub, if you turn it on.
- The server only listens on `127.0.0.1` and rejects requests from other websites.

## How it works

- `app.py`: a dependency-free Python 3 server (standard library only) that stores tasks in `data/todos.json` (email copies in `data/mails/`, screenshots in `data/shots/`), parses `.eml` files, serves the single-page UI and the Settings page, and sends due-date notifications via `osascript`. `.eml` files dropped into `data/inbox/` in Finder become tasks too.
- `mac/SurveyCorps.swift`: a small AppKit app that starts `app.py`, shows the UI in a WebKit window, and receives Outlook drags natively (Outlook hands over emails as file promises, which Chromium browsers don't accept). It also draws Erwin.
- `mac/build.sh`: compiles the app and installs it to `~/Applications/Survey Corps.app`.
- `test_app.py`: run `python3 test_app.py`; it works in its own temporary data folder, never in `data/`.

## Setup

Requirements: macOS, Xcode Command Line Tools (`xcode-select --install`, which provides `python3` and `swiftc`), Outlook for Mac.

```bash
bash mac/build.sh
open ~/Applications/"Survey Corps.app"
```

Optional: put any image at `mac/icon-source.png` before building to use it as the app icon (not included in the repo).

Then right-click the Dock icon → **Options → Keep in Dock**, and **Assign To: All Desktops** if you want the window on every desktop.

Without the Mac app, you can also run `python3 app.py`, which opens the UI in Safari (Safari accepts Outlook drags; Chrome/Brave do not).

The app looks for `app.py` relative to where the Swift source was compiled, so rebuild after moving the folder.
