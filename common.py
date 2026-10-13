"""Shared bits for the tracker daemon and the GUI: paths, config, storage, aggregation."""

import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

APP_ID = "hypr-screentime"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
# per-user overrides on top of the repo's config.json, so `git pull` never conflicts
USER_CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID / "config.json"
DATA_FILE = SCRIPT_DIR / "usage.json"
OLD_DATA_FILE = Path.home() / ".local/share" / APP_ID / "usage.json"  # pre-move location
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
STATUS_FILE = RUNTIME_DIR / f"{APP_ID}.status.json"
LOCK_FILE = RUNTIME_DIR / f"{APP_ID}.lock"

DEFAULT_CONFIG = {
    # seconds between samples of the focused window
    "poll_interval": 1.0,
    # seconds between writes of usage.json (also flushed on exit / SIGUSR1)
    "flush_interval": 30,
    # days of history kept; anything older is deleted from usage.json
    "retention_days": 30,
    # window classes / workspace names that are never counted, even when focused
    "ignore_classes": [],
    "ignore_workspaces": [],
    # screen lockers: nothing is counted while one of these processes runs
    "lockers": ["hyprlock", "swaylock", "gtklock", "waylock"],
    # When hyprlock starts after a period with no cursor movement, the idle
    # time before the lock is removed again. "auto" reads the lock timeout
    # from ~/.config/hypr/hypridle.conf, a number sets it, 0 disables it.
    "idle_rollback_seconds": "auto",
    # Also count apps that are playing audio (PipeWire / PulseAudio sink inputs)
    # while they are not focused. Paused, muted and 0% volume streams don't count.
    "count_audio": True,
    # Browsers don't say which tab plays audio, so it goes to a window showing one of these
    # sites (names from "sites" / "extra_sites"); otherwise to the browser with no detail.
    "audio_sites": ["YouTube Music", "YouTube", "Anikoto", "HiAnime", "Crunchyroll", "Netflix",
                    "Prime Video", "Twitch", "Spotify", "Discord", "WhatsApp", "Telegram",
                    "Instagram", "Facebook", "X", "Reddit"],
    # application names / binaries whose audio never counts
    "audio_ignore": ["speech-dispatcher", "easyeffects"],
    # class -> display name overrides (otherwise taken from .desktop files)
    "app_names": {},
    "browsers": [
        "firefox", "firefox-developer-edition", "librewolf", "zen", "zen-alpha", "zen-beta",
        "floorp", "waterfox", "chromium", "google-chrome", "brave-browser", "vivaldi-stable",
        "thorium-browser", "microsoft-edge",
    ],
    "terminals": [
        "kitty", "alacritty", "foot", "footclient", "org.wezfurlong.wezterm",
        "com.mitchellh.ghostty", "org.kde.konsole", "konsole", "xterm",
        "org.gnome.terminal", "gnome-terminal-server", "Alacritty",
    ],
    # editors: the detail is the project/workspace (last " - " part of the title)
    "editors": ["code-oss", "code", "code-url-handler", "vscodium", "codium", "cursor", "dev.zed.zed"],
    # apps whose window title itself is the detail (e.g. the file playing in mpv)
    "title_detail_classes": ["mpv", "vlc", "org.pwmt.zathura", "imv"],
    # your own [site name, regex] rules, checked before the built-in "sites" list
    "extra_sites": [],
    # [site name, regex]. Checked in order against the browser tab title
    # (and against the domain of chromium --app windows). Case-insensitive.
    "sites": [
        ["YouTube Music", r"YouTube Music"],
        ["YouTube", r"\bYouTube\b"],
        ["Anikoto", r"anikoto"],
        ["HiAnime", r"hianime"],
        ["Crunchyroll", r"crunchyroll"],
        ["MyAnimeList", r"MyAnimeList"],
        ["AniList", r"\bAniList\b"],
        ["Netflix", r"\bNetflix\b"],
        ["Prime Video", r"Prime Video"],
        ["Twitch", r"\bTwitch\b"],
        ["Reddit", r"\breddit\b|^r/\w+"],
        ["GitHub", r"\bGitHub\b"],
        ["Stack Overflow", r"Stack Overflow|stackoverflow"],
        ["ArchWiki", r"ArchWiki"],
        ["Wikipedia", r"Wikipedia"],
        ["ChatGPT", r"ChatGPT|chatgpt\.com"],
        ["Claude", r"(^|[-|—] )Claude$|claude\.ai"],
        ["Gemini", r"(^|[-|—] )Gemini$|gemini\.google"],
        ["Gmail", r"\bGmail\b|mail\.google"],
        ["Google Docs", r"Google (Docs|Sheets|Slides)"],
        ["Google Search", r" - Google Search$"],
        ["WhatsApp", r"WhatsApp|web\.whatsapp\.com"],
        ["Discord", r"\bDiscord\b"],
        ["Telegram", r"Telegram|web\.telegram\.org"],
        ["Instagram", r"Instagram"],
        ["Facebook", r"Facebook"],
        ["X", r"( on X|/ X)( |$)|^X$|x\.com|twitter"],
        ["LinkedIn", r"LinkedIn"],
        ["Spotify", r"Spotify"],
        ["Amazon", r"\bAmazon\b"],
    ],
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in (CONFIG_FILE, USER_CONFIG_FILE):  # later files win
        if not path.exists():
            continue
        try:
            user = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"[screentime] ignoring broken {path}: {e}")
            continue
        for k, v in user.items():
            if not k.startswith("_"):
                cfg[k] = v
    return cfg


# ----------------------------------------------------------------- storage
#
# usage.json layout:
#   {"version": 1,
#    "days": {"2026-10-04": {"Firefox": {"YouTube": {"14": 312.0, ...}, ...}, ...}}}
#   day -> app -> detail ("" when the app has no breakdown) -> hour -> seconds
#   "audio" has the same layout: time an app was playing audio while it was
#   NOT the focused window. The GUI adds both trees together.


def empty_data():
    return {"version": 1, "days": {}, "audio": {}}


def trees(data):
    return data["days"], data.setdefault("audio", {})


def migrate_old_data():
    """Move usage.json from ~/.local/share/hypr-screentime into the script folder (once)."""
    if DATA_FILE.exists() or not OLD_DATA_FILE.exists():
        return
    shutil.move(OLD_DATA_FILE, DATA_FILE)
    try:
        OLD_DATA_FILE.parent.rmdir()
    except OSError:
        pass
    print(f"[screentime] moved {OLD_DATA_FILE} -> {DATA_FILE}", flush=True)


def load_data(path=None):
    path = path or DATA_FILE
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        print(f"[screentime] could not read {path}: {e}")
    return empty_data()


def atomic_write_json(path, obj, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_data(data, path=None):
    for tree in trees(data):
        for apps in tree.values():
            for details in apps.values():
                for hours in details.values():
                    for h, s in hours.items():
                        hours[h] = round(s, 1)
    atomic_write_json(path or DATA_FILE, data, indent=1)


def prune(data, retention_days, today=None):
    """Delete every day older than the retention window."""
    today = today or date.today()
    cutoff = (today - timedelta(days=retention_days - 1)).isoformat()
    for tree in trees(data):
        for day in [d for d in tree if d < cutoff]:
            del tree[day]


# ------------------------------------------------------------- aggregation


def _day_entries(data, days, audio=True):
    """Yield (app, details) for each day, from the focus tree and optionally the audio tree."""
    focus, sound = trees(data)
    for tree in (focus, sound) if audio else (focus,):
        for day in days:
            yield from tree.get(day, {}).items()


def app_totals(data, days):
    out = {}
    for app, details in _day_entries(data, days):
        out[app] = out.get(app, 0) + sum(sum(h.values()) for h in details.values())
    return out


def audio_total(data, days):
    """Seconds counted only because an unfocused app was playing audio."""
    audio = trees(data)[1]
    return sum(sum(h.values()) for day in days for details in audio.get(day, {}).values()
               for h in details.values())


def detail_totals(data, days):
    """{(app, detail): seconds} for every non-empty detail."""
    out = {}
    for app, details in _day_entries(data, days):
        for detail, hours in details.items():
            if detail:
                out[(app, detail)] = out.get((app, detail), 0) + sum(hours.values())
    return out


def hourly(data, day):
    """[{app: seconds}] * 24 for one day."""
    out = [dict() for _ in range(24)]
    for app, details in _day_entries(data, [day]):
        for hours in details.values():
            for h, s in hours.items():
                slot = out[int(h)]
                slot[app] = slot.get(app, 0) + s
    return out


def fmt_duration(seconds, short=False):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m" if not short or m else f"{h}h"
    if m:
        return f"{m}m"
    return f"{s}s"
