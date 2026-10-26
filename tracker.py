#!/usr/bin/env python3
"""Screen-time tracker daemon for Hyprland.

Every `poll_interval` seconds it asks Hyprland which client is focused (the
same JSON `hyprctl clients -j` / `hyprctl activewindow -j` print), works out a
friendly app name plus a detail (browser site, terminal program, editor
project, ...) and adds the elapsed time to usage.json.

Only the *focused* window is ever counted, so apps parked in special
workspaces (special:minimize, special:network_social, ...) only count while
that special workspace is open and the app has focus.

Apps that are playing audio (PipeWire / PulseAudio, via `pactl`) are counted
too while they are not focused, e.g. YouTube in a background workspace. That
time goes into a separate "audio" tree in usage.json.

Nothing is counted while a screen locker (hyprlock, swaylock, ...) is running or while all screens are off
(DPMS). Focus time also stops while no window is focused.

If Hyprland isn't running, the daemon waits for it (checking `hyprctl
clients` every 5s), and after Hyprland exits it saves and waits again. So it
can be started from a systemd user service at TTY login.

Usage:  tracker.py            run the daemon (single instance)
        tracker.py --once     print what would be recorded right now
"""

import configparser
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

import common

SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu", "xonsh", "elvish", "tcsh", "ksh"}
INTERPRETERS = {"python", "python3", "node", "bun", "deno", "ruby", "perl", "lua", "java"}
BROWSER_SUFFIX = re.compile(
    r"\s+[—–-]\s+(Mozilla Firefox|Firefox|Chromium|Google Chrome|Brave|Zen Browser|Zen|"
    r"LibreWolf|Vivaldi|Floorp|Waterfox|Thorium|Microsoft Edge)( Private Browsing)?$"
)
EDITOR_SUFFIX = re.compile(r"\s+-\s+(Code - OSS|Visual Studio Code|VSCodium|Cursor|Zed)$")
TITLE_SPLIT = re.compile(r"\s+(?:-|—|–|\||·|•)\s+")
CHROME_APP = re.compile(r"^(?:chrome|chromium|brave|msedge)-(.+?)__")
# stream names that say nothing about the tab (Chromium and friends)
GENERIC_STREAM_NAMES = {"", "playback", "audiostream", "audio stream", "audio-stream", "playstream",
                        "output", "audiocallbackdriver", "simple dmb"}


# ------------------------------------------------------------------ Hyprland IPC


class Hyprland:
    def __init__(self):
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        self.sock = None
        if sig:
            for base in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}", "/tmp"):
                if base and (Path(base) / "hypr" / sig).is_dir():
                    self.sock = Path(base) / "hypr" / sig / ".socket.sock"
                    break

    def alive(self):
        return self.sock is not None and self.sock.exists()

    def query(self, cmd):
        """Same as `hyprctl -j <cmd>` but over the socket (no process spawn)."""
        if self.sock is None:
            out = subprocess.run(["hyprctl", "-j", cmd], capture_output=True, text=True, timeout=2).stdout
            return json.loads(out or "null")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(self.sock))
            s.sendall(f"j/{cmd}".encode())
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        return json.loads(b"".join(chunks) or b"null")


# ------------------------------------------------------------------ /proc helpers


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def proc_stat(pid):
    """(comm, ppid, session, tty_nr, tpgid, starttime) or None."""
    raw = read(f"/proc/{pid}/stat")
    if not raw:
        return None
    comm = raw[raw.find("(") + 1: raw.rfind(")")]
    f = raw[raw.rfind(")") + 2:].split()
    # f[0]=state f[1]=ppid f[2]=pgrp f[3]=session f[4]=tty_nr f[5]=tpgid ... f[19]=starttime
    return comm, int(f[1]), int(f[3]), int(f[4]), int(f[5]), int(f[19])


def children(pid):
    out = []
    for task in os.listdir(f"/proc/{pid}/task") if os.path.isdir(f"/proc/{pid}/task") else []:
        out += [int(c) for c in read(f"/proc/{pid}/task/{task}/children").split()]
    return out


def process_label(pid):
    comm = read(f"/proc/{pid}/comm").strip()
    if comm in INTERPRETERS:
        # `python3 foo.py` -> foo.py
        argv = read(f"/proc/{pid}/cmdline").split("\0")
        for arg in argv[1:]:
            if arg and not arg.startswith("-"):
                return os.path.basename(arg)
    return comm


def is_running(name):
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and read(f"/proc/{entry.name}/comm").strip() == name:
            return True
    return False


# ------------------------------------------------------------------ classification


class Classifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sites = [(name, re.compile(rx, re.I)) for name, rx in cfg["extra_sites"] + cfg["sites"]]
        self.browsers = {c.lower() for c in cfg["browsers"]}
        self.terminals = {c.lower() for c in cfg["terminals"]}
        self.editors = {c.lower() for c in cfg["editors"]}
        self.title_detail = {c.lower() for c in cfg["title_detail_classes"]}
        self.app_names = {k.lower(): v for k, v in cfg["app_names"].items()}
        self.audio_sites = set(cfg["audio_sites"])
        self.desktop_names = self._scan_desktop_files()

    @staticmethod
    def _scan_desktop_files():
        dirs = [Path.home() / ".local/share/applications", Path("/usr/share/applications"),
                Path("/usr/local/share/applications"), Path("/var/lib/flatpak/exports/share/applications"),
                Path.home() / ".local/share/flatpak/exports/share/applications"]
        by_file, by_wmclass = {}, {}
        for d in reversed(dirs):  # earlier dirs win
            if not d.is_dir():
                continue
            for f in d.glob("*.desktop"):
                cp = configparser.RawConfigParser(strict=False, interpolation=None)
                try:
                    cp.read(f, encoding="utf-8")
                    entry = cp["Desktop Entry"]
                except (configparser.Error, KeyError, UnicodeDecodeError):
                    continue
                name = entry.get("Name")
                if not name or entry.get("NoDisplay", "").lower() == "true":
                    continue
                by_file[f.stem.lower()] = name
                if entry.get("StartupWMClass"):
                    by_wmclass[entry["StartupWMClass"].lower()] = name
        by_file.update(by_wmclass)
        return by_file

    def site(self, text):
        for name, rx in self.sites:
            if rx.search(text):
                return name
        return None

    def app_name(self, cls):
        low = cls.lower()
        if low in self.app_names:
            return self.app_names[low]
        if low in self.desktop_names:
            return self.desktop_names[low]
        tail = low.rsplit(".", 1)[-1]  # org.kde.dolphin -> dolphin
        if tail in self.desktop_names:
            return self.desktop_names[tail]
        return cls[:1].upper() + cls[1:]

    def browser_detail(self, title):
        page = BROWSER_SUFFIX.sub("", title).strip()
        if not page or page in {"Mozilla Firefox", "Chromium", "New Tab", "New Private Tab"}:
            return "New Tab"
        if page == "Picture-in-Picture":
            return page
        if s := self.site(page):
            return s
        parts = TITLE_SPLIT.split(page)
        if len(parts) > 1 and 1 < len(parts[-1]) <= 32:
            return parts[-1]
        return "Other pages"

    @staticmethod
    def terminal_detail(pid, title):
        candidates = []
        for child in children(pid):
            st = proc_stat(child)
            # a shell owning a tty: session leader with a foreground process group
            if not st or st[3] == 0 or st[2] != child or st[4] <= 0:
                continue
            fg_st = proc_stat(st[4])
            if label := process_label(st[4]):
                candidates.append((label, fg_st[5] if fg_st else 0))
        if not candidates:
            word = title.split()[0] if title.split() else ""
            return word if word and not word.startswith(("~", "/")) else "shell"
        low_title = title.lower()
        for label, _ in candidates:
            if label.lower() in low_title and label not in SHELLS:
                return label
        if len(candidates) == 1:
            return candidates[0][0]
        # several tabs and the title doesn't tell: the most recently started foreground job
        return max(candidates, key=lambda c: c[1])[0]

    def classify(self, win):
        cls = win.get("class") or win.get("initialClass") or "unknown"
        title = win.get("title") or ""
        low = cls.lower()

        if m := CHROME_APP.match(cls):  # chromium --app=https://web.whatsapp.com
            domain = m.group(1)
            return self.site(domain) or domain, ""
        if low.startswith("steam_app_"):
            return title or cls, ""
        if low in self.browsers:
            return self.app_name(cls), self.browser_detail(title)
        if low in self.terminals:
            return self.app_name(cls), self.terminal_detail(win["pid"], title)
        if low in self.editors:
            parts = TITLE_SPLIT.split(EDITOR_SUFFIX.sub("", title))
            return self.app_name(cls), parts[-1].lstrip("● ") if parts and parts[-1] else ""
        if low in self.title_detail:
            return self.app_name(cls), title[:80]
        return self.app_name(cls), ""

    def classify_audio(self, pid, binary, media, clients):
        """(app, detail) for a playing stream, attributed to the window whose
        process (or an ancestor of it, e.g. a browser's audio subprocess) owns
        it. None when that window is ignored."""
        by_pid = {}
        for c in clients:
            by_pid.setdefault(c.get("pid"), []).append(c)
        cur, owner = pid, None
        for _ in range(40):
            if cur in by_pid:
                owner = cur
                break
            st = proc_stat(cur) if cur > 1 else None
            if not st:
                break
            cur = st[1]
        if owner is None:  # no window, e.g. mpd or a pipewire client daemon
            return self.app_name(binary), ""

        wins = by_pid[owner]
        win = next((w for w in wins if media and media in (w.get("title") or "")), wins[0])
        ws = (win.get("workspace") or {}).get("name", "")
        if win.get("class") in self.cfg["ignore_classes"] or ws in self.cfg["ignore_workspaces"]:
            return None
        low = (win.get("class") or "").lower()
        if low in self.terminals:  # whatever in the terminal plays it (mpv, spotify_player, ...)
            return self.app_name(win["class"]), process_label(pid) or binary
        if low in self.browsers:
            if media.lower() not in GENERIC_STREAM_NAMES:  # some browsers name the stream after the tab
                return self.app_name(win["class"]), self.browser_detail(media)
            # Firefox/Chromium say "AudioStream"/"Playback": take the window showing a known
            # site (YouTube, ...). Without one we can't tell which tab plays, so no detail.
            for w in wins:
                app, detail = self.classify(w)
                if detail in self.audio_sites:
                    return app, detail
            return self.app_name(win["class"]), ""
        return self.classify(win)


# ------------------------------------------------------------------ helpers


def hypridle_lock_timeout():
    conf = read(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hypr/hypridle.conf")
    best = None
    for block in re.findall(r"listener\s*\{(.*?)\}", conf, re.S):
        lines = [ln.split("#", 1)[0] for ln in block.splitlines()]
        body = "\n".join(lines)
        t = re.search(r"timeout\s*=\s*(\d+)", body)
        cmd = re.search(r"on-timeout\s*=\s*(.*)", body)
        if t and cmd and "lock" in cmd.group(1):
            best = min(best or 10**9, int(t.group(1)))
    return best


def audio_streams(cfg):
    """Sink inputs that are really playing: [(pid, binary, media_name)]."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"],
                             capture_output=True, text=True, timeout=2)
        streams = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    ignore = {x.lower() for x in cfg["audio_ignore"]}
    found = []
    for st in streams if isinstance(streams, list) else []:
        if st.get("corked") or st.get("mute"):
            continue
        volumes = [ch.get("value", 1) for ch in (st.get("volume") or {}).values() if isinstance(ch, dict)]
        if volumes and not any(volumes):
            continue
        props = st.get("properties") or {}
        binary = props.get("application.process.binary") or props.get("application.name") or "unknown"
        if {binary.lower(), (props.get("application.name") or "").lower()} & ignore:
            continue
        try:
            pid = int(props.get("application.process.id"))
        except (TypeError, ValueError):
            pid = 0
        found.append((pid, binary, props.get("media.name") or ""))
    return found


def focused_window(hypr, cfg):
    """Return the focused client dict if it should be counted, else (None, reason)."""
    monitors = hypr.query("monitors") or []
    if monitors and not any(m.get("dpmsStatus", True) for m in monitors):
        return None, "screens off"
    win = hypr.query("activewindow") or {}
    if not win.get("address"):
        return None, "no window"
    ws = (win.get("workspace") or {}).get("name", "")
    if ws.startswith("special:"):
        # only while that special workspace is actually shown on some monitor
        shown = {(m.get("specialWorkspace") or {}).get("name") for m in monitors}
        if ws not in shown:
            return None, "hidden special workspace"
    if ws in cfg["ignore_workspaces"] or win.get("class") in cfg["ignore_classes"]:
        return None, "ignored"
    return win, ""


# ------------------------------------------------------------------ daemon


class Tracker:
    def __init__(self):
        self.cfg = common.load_config()
        self.hypr = Hyprland()
        self.classifier = Classifier(self.cfg)
        self.data = common.load_data()
        common.prune(self.data, self.cfg["retention_days"])
        self.pruned_on = date.today()
        self.dirty = False
        self.flush_requested = False
        self.stop = False

        rb = self.cfg["idle_rollback_seconds"]
        self.rollback = (hypridle_lock_timeout() or 0) if rb == "auto" else int(rb or 0)
        self.recent = deque()  # (monotonic, day, app, detail, hour, seconds, audio_too)
        self.last_input = time.monotonic()
        self.last_cursor = None
        self.last_address = None
        self.inhibiting = False
        self.locked = False
        self.status = {}
        self.status_written = 0.0
        self.since_key = None  # (state, app) the `since` timestamp belongs to
        self.since = time.time()

    def add_active(self, day, hour, secs):
        """Real screen time: called once per tick in which anything was counted."""
        node = self.data.setdefault("active", {}).setdefault(day, {})
        node[hour] = max(0.0, node.get(hour, 0) + secs)
        self.dirty = True

    def add(self, day, app, detail, hour, secs, now, tree="days", audio_too=False):
        node = self.data.setdefault(tree, {}).setdefault(day, {}).setdefault(app, {}).setdefault(detail, {})
        node[hour] = node.get(hour, 0) + secs
        self.dirty = True
        if self.rollback and tree == "days":  # only focus time is idle-rolled-back
            self.recent.append((now, day, app, detail, hour, secs, audio_too))
            while self.recent and self.recent[0][0] < now - self.rollback - 5:
                self.recent.popleft()

    def undo_idle(self, now):
        """hyprlock just started: if it was hypridle (no input for the lock
        timeout), remove the idle stretch that was counted before it."""
        idle_for = now - self.last_input
        if not self.rollback or self.inhibiting or idle_for < self.rollback - 15:
            self.recent.clear()
            return
        removed = 0.0
        while self.recent and self.recent[-1][0] > now - min(idle_for, self.rollback):
            _, day, app, detail, hour, secs, audio_too = self.recent.pop()
            node = self.data["days"].get(day, {}).get(app, {}).get(detail, {})
            if hour in node:
                node[hour] = max(0.0, node[hour] - secs)
                removed += secs
                if not audio_too:  # audio was playing, so that second still counts as screen time
                    self.add_active(day, hour, -secs)
        self.recent.clear()
        self.dirty = True
        print(f"[screentime] idle lock detected, removed {removed:.0f}s of idle time", flush=True)

    def write_status(self, **status):
        now = time.time()
        key = (status.get("state"), status.get("app"))
        if key != self.since_key:  # `since`: when the current app (or pause state) began
            self.since_key, self.since = key, now
        status["since"] = round(self.since, 1)
        if status != self.status or now - self.status_written > 5:
            self.status = status
            self.status_written = now
            try:
                common.atomic_write_json(common.STATUS_FILE, {**status, "ts": now, "pid": os.getpid()})
            except OSError:
                pass

    def flush(self):
        if date.today() != self.pruned_on:
            common.prune(self.data, self.cfg["retention_days"])
            self.pruned_on = date.today()
        if self.dirty:
            common.save_data(self.data)
            self.dirty = False

    def sample(self):
        """Return (app, detail, state) for right now."""
        if any(is_running(name) for name in self.cfg["lockers"]):
            return None, None, "locked"
        win, reason = focused_window(self.hypr, self.cfg)

        cur = self.hypr.query("cursorpos")
        address = win.get("address") if win else None
        if cur != self.last_cursor or address != self.last_address:
            self.last_input = time.monotonic()
        self.last_cursor, self.last_address = cur, address
        self.inhibiting = bool(win and win.get("inhibitingIdle"))

        if not win:
            return None, None, reason
        app, detail = self.classifier.classify(win)
        return app, detail, "active"

    def sample_audio(self, focused):
        """[(app, detail)] playing audio right now, minus the focused one."""
        if not self.cfg["count_audio"]:
            return []
        streams = audio_streams(self.cfg)
        if not streams:
            return []
        clients = self.hypr.query("clients") or []
        out = []
        for pid, binary, media in streams:
            hit = self.classifier.classify_audio(pid, binary, media, clients)
            if hit and hit != focused and hit not in out:
                out.append(hit)
        return out

    def run(self):
        interval = float(self.cfg["poll_interval"])
        flush_every = float(self.cfg["flush_interval"])
        last = time.monotonic()
        last_flush = last
        dead_since = None
        print(f"[screentime] tracking -> {common.DATA_FILE} (idle rollback {self.rollback}s)", flush=True)

        while not self.stop:
            time.sleep(max(0.05, interval - (time.monotonic() - last)))
            now = time.monotonic()
            # monotonic does not advance during suspend; cap anyway for safety
            elapsed = min(now - last, interval * 3)
            last = now

            try:
                if not self.hypr.alive():
                    raise ConnectionError("Hyprland socket is gone")
                app, detail, state = self.sample()
                quiet = state in ("locked", "screens off")
                audio = [] if quiet else self.sample_audio((app, detail) if app else None)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                if dead_since is None:
                    print(f"[screentime] can't reach Hyprland: {e}", flush=True)
                dead_since = dead_since or now
                if now - dead_since > 10:
                    print("[screentime] Hyprland is gone, saving and waiting for the next session", flush=True)
                    break
                continue
            dead_since = None

            if state == "locked" and not self.locked:
                self.undo_idle(now)
            elif state != "locked" and self.locked:
                self.last_input = now  # unlocking takes input
            self.locked = state == "locked"

            stamp = datetime.now()
            day, hour = stamp.date().isoformat(), str(stamp.hour)
            if app:
                self.add(day, app, detail, hour, elapsed, now, audio_too=bool(audio))
            for a_app, a_detail in audio:
                self.add(day, a_app, a_detail, hour, elapsed, now, tree="audio")
            if app or audio:
                self.add_active(day, hour, elapsed)
            self.write_status(state=state, app=app, detail=detail,
                              audio=[f"{a} › {d}" if d else a for a, d in audio])

            if self.flush_requested or now - last_flush >= flush_every:
                self.flush_requested = False
                last_flush = now
                self.flush()

        self.flush()
        self.locked = False
        self.recent.clear()
        try:
            common.STATUS_FILE.unlink()
        except OSError:
            pass


def hyprctl_json(*args, env=None):
    try:
        out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=5, env=env)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_hyprland():
    """True if `hyprctl clients` answers. A service started from the TTY has no
    (or a stale) HYPRLAND_INSTANCE_SIGNATURE, so ask `hyprctl instances` for the
    running one and adopt it."""
    if isinstance(hyprctl_json("clients"), list):
        return True
    instances = hyprctl_json("instances")
    for inst in sorted(instances if isinstance(instances, list) else [], key=lambda i: -i.get("time", 0)):
        sig = inst.get("instance")
        if sig and isinstance(hyprctl_json("clients", env={**os.environ, "HYPRLAND_INSTANCE_SIGNATURE": sig}), list):
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = sig
            return True
    return False


def main():
    if "--once" in sys.argv:
        if not find_hyprland():
            print("[screentime] Hyprland is not running")
            return
        cfg = common.load_config()
        hypr = Hyprland()
        classifier = Classifier(cfg)
        win, reason = focused_window(hypr, cfg)
        if not win:
            print(f"not counted: {reason}")
        else:
            app, detail = classifier.classify(win)
            print(f"class={win.get('class')!r} title={win.get('title')!r}")
            print(f"  -> app={app!r} detail={detail!r} workspace={win['workspace']['name']!r}")
        streams = audio_streams(cfg) if cfg["count_audio"] else []
        clients = hypr.query("clients") or []
        for pid, binary, media in streams:
            hit = classifier.classify_audio(pid, binary, media, clients)
            print(f"audio: pid={pid} {binary!r} {media!r} -> {hit}")
        if not streams:
            print("audio: nothing playing")
        return

    # As a system service this can start at boot, before /run/user/<uid> exists.
    if not common.RUNTIME_DIR.is_dir():
        print(f"[screentime] waiting for {common.RUNTIME_DIR} (not logged in yet)...", flush=True)
        while not common.RUNTIME_DIR.is_dir():
            time.sleep(5)

    lock = open(common.LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Don't exit: under Restart=always that would loop. Take over when the other one ends.
        print("[screentime] another tracker is running, waiting for it to exit...", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)

    common.migrate_old_data()
    tracker = Tracker()

    def on_stop(*_):
        tracker.stop = True

    def on_flush(*_):
        tracker.flush_requested = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, on_stop)
    signal.signal(signal.SIGUSR1, on_flush)

    # Never exit on our own: wait for Hyprland, track it, and when it goes
    # away (logout) save and wait for the next session.
    waiting = False
    while not tracker.stop:
        if not find_hyprland():
            if not waiting:
                print("[screentime] Hyprland is not running, waiting for it...", flush=True)
                waiting = True
            for _ in range(5):
                if tracker.stop:
                    break
                time.sleep(1)
            continue
        waiting = False
        tracker.hypr = Hyprland()
        tracker.run()


if __name__ == "__main__":
    main()
