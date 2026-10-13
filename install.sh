#!/usr/bin/env bash
# Installer for the Hyprland screen-time tracker.
#
#   ./install.sh                 user service + launcher entry (no sudo needed)
#   ./install.sh --system        system service that runs as you (needs sudo)
#   ./install.sh --no-service    only check dependencies and add the launcher entry
#   ./install.sh --deps          also install missing packages with your package manager
#   ./install.sh --no-gui        skip the GUI (PySide6 and the launcher entry)
#   ./install.sh --yes           don't ask questions (for other installers)
#   ./install.sh --dry-run       print what would be done, change nothing
#   ./install.sh --uninstall     remove the service and launcher entry (keeps usage.json)
#
# The tracker runs from this folder, so don't move it after installing
# (or re-run ./install.sh if you do).

set -euo pipefail

APP_ID=hypr-screentime
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=user GUI=1 DEPS=0 YES=0 DRY=0 UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --system) MODE=system ;;
        --user) MODE=user ;;
        --no-service) MODE=none ;;
        --no-gui) GUI=0 ;;
        --deps) DEPS=1 ;;
        --yes|-y) YES=1 ;;
        --dry-run) DRY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'; else B= G= Y= R= N=; fi
info() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
ask()  { [ "$YES" = 1 ] && return 0; read -r -p "  $1 [Y/n] " a </dev/tty || return 1; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

[ "$(id -u)" = 0 ] && die "run this as your normal user, not root (it uses sudo itself for --system)"

USER_NAME="$(id -un)"
USER_UID="$(id -u)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
USER_UNIT="$CONFIG_HOME/systemd/user/$APP_ID.service"
SYSTEM_UNIT="/etc/systemd/system/$APP_ID.service"
DESKTOP_FILE="$DATA_HOME/applications/$APP_ID.desktop"
HAS_SYSTEMD=0
[ -d /run/systemd/system ] && command -v systemctl >/dev/null && HAS_SYSTEMD=1

# ------------------------------------------------------------------ uninstall

if [ "$UNINSTALL" = 1 ]; then
    info "Uninstalling (usage.json and config.json are kept)"
    if [ -f "$USER_UNIT" ]; then
        run systemctl --user disable --now "$APP_ID.service" || true
        run rm -f "$USER_UNIT"
        run systemctl --user daemon-reload
        ok "removed user service"
    fi
    if [ -f "$SYSTEM_UNIT" ]; then
        run sudo systemctl disable --now "$APP_ID.service" || true
        run sudo rm -f "$SYSTEM_UNIT"
        run sudo systemctl daemon-reload
        ok "removed system service"
    fi
    [ -f "$DESKTOP_FILE" ] && run rm -f "$DESKTOP_FILE" && ok "removed launcher entry"
    exit 0
fi

# ------------------------------------------------------------------ dependencies

info "Checking dependencies"

PM=""
for p in pacman apt-get dnf zypper xbps-install apk emerge; do
    command -v "$p" >/dev/null && { PM=$p; break; }
done

# package names per manager: <python> <pyside6> <pactl>
pkgs() {
    case "$PM" in
        pacman)       echo "python python-pyside6 libpulse" ;;
        apt-get)      echo "python3 python3-pyside6.qtwidgets pulseaudio-utils" ;;
        dnf)          echo "python3 python3-pyside6 pulseaudio-utils" ;;
        zypper)       echo "python3 python3-pyside6 pulseaudio-utils" ;;
        xbps-install) echo "python3 python3-pyside6 pulseaudio-utils" ;;
        apk)          echo "python3 py3-pyside6 pulseaudio-utils" ;;
        emerge)       echo "dev-lang/python dev-python/pyside media-sound/pulseaudio" ;;
    esac
}
pm_install() {
    case "$PM" in
        pacman)       run sudo pacman -S --needed --noconfirm "$@" ;;
        apt-get)      run sudo apt-get install -y "$@" ;;
        dnf)          run sudo dnf install -y "$@" ;;
        zypper)       run sudo zypper --non-interactive install "$@" ;;
        xbps-install) run sudo xbps-install -y "$@" ;;
        apk)          run sudo apk add "$@" ;;
        emerge)       run sudo emerge --noreplace "$@" ;;
    esac
}
read -r PKG_PY PKG_QT PKG_PACTL <<<"$(pkgs)" || true
missing=()

PY="$(command -v python3 || true)"
if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    ok "python3 $("$PY" -c 'import platform; print(platform.python_version())')"
else
    warn "python3 3.8 or newer is required"
    [ -n "$PKG_PY" ] && missing+=("$PKG_PY")
fi

if command -v hyprctl >/dev/null; then
    ok "hyprctl"
else
    warn "hyprctl not found: the tracker only works under Hyprland (it will wait for it)"
fi

if command -v pactl >/dev/null; then
    ok "pactl (background audio is counted)"
else
    warn "pactl not found: background audio won't be counted (focused windows still are)"
    [ -n "$PKG_PACTL" ] && missing+=("$PKG_PACTL")
fi

if [ "$GUI" = 1 ]; then
    if [ -n "$PY" ] && "$PY" -c 'import PySide6.QtWidgets' 2>/dev/null; then
        ok "PySide6 (GUI)"
    else
        warn "PySide6 not found: the GUI won't start (the tracker doesn't need it)"
        [ -n "$PKG_QT" ] && missing+=("$PKG_QT")
    fi
fi

if [ "${#missing[@]}" -gt 0 ]; then
    if [ -z "$PM" ]; then
        warn "unknown package manager: install python3 3.8+, pactl and PySide6 with your distro's packages"
    elif [ "$DEPS" = 1 ] || ask "Install ${missing[*]} with $PM?"; then
        # a failed package install (wrong name, no sudo) must not stop the rest of the install
        pm_install "${missing[@]}" || warn "package install failed; install ${missing[*]} yourself"
        PY="$(command -v python3 || true)"
    else
        warn "skipped; install later with: $PM ${missing[*]}"
    fi
fi
[ -n "$PY" ] || PY=/usr/bin/python3
[ "$DRY" = 1 ] || [ -x "$PY" ] || die "python3 is still missing"

chmod +x "$DIR/tracker.py" "$DIR/gui.py"

# ------------------------------------------------------------------ service

unit_body() {  # $1 = user | system
    cat <<EOF
[Unit]
Description=Hyprland screen time tracker
Documentation=file://$DIR/README.md
# never give up restarting, however often it dies
StartLimitIntervalSec=0

[Service]
Type=simple
EOF
    if [ "$1" = system ]; then
        echo "User=$USER_NAME"
        echo "Environment=XDG_RUNTIME_DIR=/run/user/$USER_UID"
    fi
    cat <<EOF
Environment=PYTHONUNBUFFERED=1
ExecStart="$PY" "$DIR/tracker.py"
# comes back after a crash, a kill or a clean exit; only 'systemctl stop' keeps it down.
# The tracker itself waits for login and for Hyprland, and survives logouts.
Restart=always
RestartSec=3
# SIGTERM makes the tracker save usage.json before it exits
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=default.target
EOF
}

write_file() {  # $1 = path, stdin = content; $2 = "sudo" to write as root
    if [ "$DRY" = 1 ]; then
        printf '  [dry-run] write %s:\n' "$1"; sed 's/^/      /'
    elif [ "${2:-}" = sudo ]; then
        sudo install -Dm644 /dev/stdin "$1"
    else
        mkdir -p "$(dirname "$1")"; cat >"$1"
    fi
}

if [ "$MODE" != none ] && [ "$HAS_SYSTEMD" = 0 ]; then
    warn "no systemd found, so no service was installed. Start the tracker from your Hyprland config instead:"
    if [ -f "$CONFIG_HOME/hypr/hyprland.lua" ]; then  # Hyprland 0.55+ Lua config
        printf '      hl.on("hyprland.start", function() hl.exec_cmd("%s %s") end)\n' "$PY" "$DIR/tracker.py"
    else
        printf '      exec-once = %s %s\n' "$PY" "$DIR/tracker.py"
    fi
    MODE=none
fi

case "$MODE" in
    user)
        info "Installing user service ($USER_UNIT)"
        if [ -f "$SYSTEM_UNIT" ]; then
            warn "a system service already exists ($SYSTEM_UNIT); keeping that one."
            warn "to switch: ./install.sh --uninstall && ./install.sh"
        else
            unit_body user | write_file "$USER_UNIT"
            run systemctl --user daemon-reload
            run systemctl --user enable "$APP_ID.service"
            run systemctl --user restart "$APP_ID.service"
            ok "enabled; logs: journalctl --user -u $APP_ID -f"
        fi
        ;;
    system)
        info "Installing system service ($SYSTEM_UNIT, runs as $USER_NAME)"
        if [ -f "$USER_UNIT" ]; then
            run systemctl --user disable --now "$APP_ID.service" || true
            run rm -f "$USER_UNIT"
            ok "removed the old user service"
        fi
        unit_body system | write_file "$SYSTEM_UNIT" sudo
        run sudo systemctl daemon-reload
        run sudo systemctl enable "$APP_ID.service"
        run sudo systemctl restart "$APP_ID.service"
        ok "enabled; logs: journalctl -u $APP_ID -f"
        ;;
esac

# ------------------------------------------------------------------ launcher entry

if [ "$GUI" = 1 ]; then
    info "Adding launcher entry ($DESKTOP_FILE)"
    write_file "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Time
Comment=Hyprland screen time dashboard
Exec="$PY" "$DIR/gui.py"
Icon=preferences-system-time
Terminal=false
Categories=Utility;
StartupWMClass=$APP_ID
EOF
    ok "open it from your launcher, or run $DIR/gui.py"
fi

info "Done. Data is saved to $DIR/usage.json"
