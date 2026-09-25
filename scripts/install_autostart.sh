#!/usr/bin/env bash
# Start Spoke at login. macOS: LaunchAgent. Linux: XDG autostart entry.
#   scripts/install_autostart.sh              install
#   scripts/install_autostart.sh --uninstall  remove
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
LABEL="com.spoke.dictation"
ACTION="${1:-install}"

if [[ "$ACTION" != "install" && "$ACTION" != "--uninstall" ]]; then
  echo "usage: $0 [--uninstall]" >&2; exit 2
fi
if [[ "$ACTION" == "install" && ! -x "$PY" ]]; then
  echo "No venv at $REPO/.venv -- run the install steps in README.md first." >&2; exit 1
fi

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [[ "$ACTION" == "--uninstall" ]]; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$PLIST"
      echo "Removed $PLIST"; exit 0
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.spoke"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>spoke</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/.spoke/launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/.spoke/launchd.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed $PLIST and started Spoke."
    echo
    echo "IMPORTANT: under launchd, macOS attributes permissions to the Python binary, not your terminal."
    echo "Grant Accessibility, Input Monitoring and Microphone to:"
    echo "    $("$PY" -c 'import os,sys;print(os.path.realpath(sys.executable))')"
    echo "(System Settings > Privacy & Security > <each one> > + , then Cmd+Shift+G and paste the path.)"
    echo "Then restart it:  launchctl kickstart -k gui/$(id -u)/$LABEL"
    echo "Uninstall:        $0 --uninstall"
    ;;
  Linux)
    DESKTOP="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/spoke.desktop"
    if [[ "$ACTION" == "--uninstall" ]]; then
      rm -f "$DESKTOP"
      pkill -f "$PY -m spoke" 2>/dev/null || true
      echo "Removed $DESKTOP"; exit 0
    fi
    # XDG autostart (not a systemd user unit): it runs inside your graphical session, so
    # DISPLAY / WAYLAND_DISPLAY / XDG_SESSION_TYPE and the keyring are always available.
    mkdir -p "$(dirname "$DESKTOP")" "$HOME/.spoke"
    cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=Spoke
Comment=Push-to-talk dictation
Exec=sh -c 'cd "$REPO" && exec "$PY" -m spoke >> "\$HOME/.spoke/autostart.log" 2>&1'
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
DESK
    echo "Installed $DESKTOP (Spoke starts at next login)."
    echo "Start it now:  (cd \"$REPO\" && nohup \"$PY\" -m spoke >/dev/null 2>&1 &)"
    echo "Uninstall:     $0 --uninstall"
    ;;
  *)
    echo "Unsupported OS $(uname -s). On Windows use scripts/install_autostart.ps1." >&2; exit 1 ;;
esac
