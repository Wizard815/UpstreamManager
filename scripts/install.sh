#!/usr/bin/env bash
# Install and start Upstream Manager on this machine.
#
#   scripts/install.sh                 # user service on port 5001 (where Hermes Studio's Upstream tab looks)
#   scripts/install.sh --port 5000     # another port
#   scripts/install.sh --system        # a system-wide service (asks for sudo) instead of a user one
#   scripts/install.sh --no-service    # only set up; run it yourself with: uv run upstream-manager
#   scripts/install.sh --home /data/ManagerFiles   # where Repos/ and References/ live (default ~/ManagerFiles)
#
# What it does, in order: checks git and uv (installs uv for this user if missing, unless
# --no-uv-install), installs the app's dependencies, writes .env from .env.example if there is none
# (filling in what it finds on this machine: herdr, Hermes Studio, the Hermes gateway), installs and
# starts a systemd service, then checks the app answers and shows what it connected to.
# Safe to run again: an existing .env is never changed, and the service is simply restarted.
set -euo pipefail

PORT=5001; MODE=user; SERVICE=1; START=1; UV_INSTALL=1; HOME_DIR="$HOME/ManagerFiles"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --system) MODE=system; shift ;;
    --home) HOME_DIR="${2:?--home needs a folder}"; shift 2 ;;
    --home=*) HOME_DIR="${1#*=}"; shift ;;
    --no-service) SERVICE=0; shift ;;
    --no-start) START=0; shift ;;
    --no-uv-install) UV_INSTALL=0; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done
case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    ! %s\033[0m\n' "$*"; }

# ---- 1. tools ---------------------------------------------------------------
say "Checking tools"
command -v git >/dev/null || { echo "git is not installed: install it first (e.g. sudo apt install git)" >&2; exit 1; }
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null; then
  if [ "$UV_INSTALL" = 1 ] && command -v curl >/dev/null; then
    note "uv (the Python package manager this app uses) is missing: installing it for $USER into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  command -v uv >/dev/null || { echo "uv is not installed: see https://docs.astral.sh/uv/ and run this again" >&2; exit 1; }
fi
UV="$(command -v uv)"
note "git $(git --version | awk '{print $3}'), uv $("$UV" --version | awk '{print $2}')"

# ---- 2. the app -------------------------------------------------------------
say "Installing the app's dependencies"
"$UV" sync --no-dev --quiet
note "ready in $DIR/.venv"

# ---- 3. .env ----------------------------------------------------------------
answers() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null | grep -qv '^000$'; }
if [ -f .env ]; then
  say "Keeping your existing .env"
  PORT_IN_ENV="$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]"'\''' || true)"
  if [ -n "${PORT_IN_ENV:-}" ] && [ "$PORT_IN_ENV" != "$PORT" ]; then
    warn ".env says PORT=$PORT_IN_ENV: using that (edit .env to change it)"; PORT="$PORT_IN_ENV"
  fi
else
  say "Writing .env (connections found on this machine)"
  cp .env.example .env
  chmod 600 .env
  setenv() {  # setenv KEY VALUE: replace the (possibly commented) line for KEY, or add one; any characters are safe
    KEY="$1" VALUE="$2" "$DIR/.venv/bin/python" - <<'PYSET'
import os, re
key, value = os.environ["KEY"], os.environ["VALUE"]
lines = open(".env", encoding="utf-8").read().split("\n")
for i, line in enumerate(lines):
    if re.match(r"^#? ?%s=" % re.escape(key), line):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
open(".env", "w", encoding="utf-8").write("\n".join(lines))
PYSET
  }
  setenv PORT "$PORT"
  setenv UPSTREAM_HOME "$HOME_DIR"
  mkdir -p "$HOME_DIR/Repos" "$HOME_DIR/References"
  note "your files: $HOME_DIR/Repos (repos) and $HOME_DIR/References (the reference library)"
  setenv UPSTREAM_APP_URL "http://127.0.0.1:$PORT"
  if [ -S "${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}" ]; then
    note "herdr: running here, it will be used automatically"
  else
    note "herdr: not running right now (it is picked up by itself once it runs as $USER)"
  fi
  STUDIO=""
  for p in 8648 6060; do answers "http://127.0.0.1:$p/" && { STUDIO="http://127.0.0.1:$p"; break; }; done
  if [ -n "$STUDIO" ]; then setenv HERMES_STUDIO_URL "$STUDIO"; note "Hermes Studio: found at $STUDIO (sign-in still needed: Settings, or HERMES_STUDIO_USER/PASSWORD in .env)"
  else note "Hermes Studio: not found on ports 8648 or 6060 (set HERMES_STUDIO_URL in .env)"; fi
  if answers "http://127.0.0.1:8642/v1/capabilities"; then
    setenv HERMES_API_URL "http://127.0.0.1:8642"
    KEY="$(grep -hE '^API_SERVER_KEY=' "${HERMES_HOME:-$HOME/.hermes}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\''' || true)"
    if [ -n "$KEY" ]; then setenv HERMES_API_KEY "$KEY"; note "Hermes gateway: found on 8642, key copied from your Hermes .env"
    else note "Hermes gateway: found on 8642 (put its API_SERVER_KEY in .env as HERMES_API_KEY)"; fi
  else
    note "Hermes gateway: not answering on 8642 (enable its API server, then set HERMES_API_URL and HERMES_API_KEY)"
  fi
  note "written: $DIR/.env (only you can read it)"
fi

# ---- 4. the service ---------------------------------------------------------
UNIT=upstream-manager.service
unit_text() {
  cat <<UNITEOF
[Unit]
Description=Upstream Manager (repos, upstreams and coding agents)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
# Connections and the port come from $DIR/.env (the app reads it itself).
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$UV run --no-dev --project $DIR upstream-manager
Restart=on-failure
RestartSec=5
$( [ "$MODE" = system ] && printf 'User=%s\nGroup=%s\n' "$USER" "$(id -gn)" )

[Install]
WantedBy=$( [ "$MODE" = system ] && echo multi-user.target || echo default.target )
UNITEOF
}
if [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null; then
  if [ "$MODE" = system ]; then
    say "Installing a system service (sudo)"
    unit_text | sudo tee "/etc/systemd/system/$UNIT" >/dev/null
    sudo systemctl daemon-reload
    [ "$START" = 1 ] && sudo systemctl enable --now "$UNIT" && sudo systemctl restart "$UNIT"
    LOGS="journalctl -u $UNIT -f"
  else
    say "Installing a service for $USER"
    mkdir -p "$HOME/.config/systemd/user"
    unit_text > "$HOME/.config/systemd/user/$UNIT"
    if systemctl --user daemon-reload 2>/dev/null; then
      [ "$START" = 1 ] && systemctl --user enable --now "$UNIT" && systemctl --user restart "$UNIT"
      loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes' || warn "to keep it running after you log out: sudo loginctl enable-linger $USER"
      LOGS="journalctl --user -u $UNIT -f"
    else
      warn "no user systemd session here: start it with  $UV run --no-dev upstream-manager  (or use --system)"
      SERVICE=0
    fi
  fi
elif [ "$SERVICE" = 1 ]; then
  warn "systemd isn't available: start it with  $UV run --no-dev upstream-manager"
  SERVICE=0
fi

# ---- 5. is it up? -----------------------------------------------------------
URL="http://127.0.0.1:$PORT"
if [ "$SERVICE" = 1 ] && [ "$START" = 1 ]; then
  say "Waiting for it to answer on $URL"
  for _ in $(seq 1 40); do curl -s -o /dev/null -m 2 "$URL/api/repos" && UP=1 && break; sleep 0.5; done
  if [ "${UP:-0}" = 1 ]; then
    note "it is running"
    curl -s -m 20 "$URL/api/integrations" | "$DIR/.venv/bin/python" -c '
import json, sys
d = json.load(sys.stdin)
names = {"herdr": "herdr", "studio": "Hermes Studio", "hermes": "Hermes gateway", "orb": "Pondering Orb", "wiki": "LLM wiki"}
for k, n in names.items():
    v = d.get(k) or {}
    print("    %-15s %s" % (n, "connected" if v.get("ok") else "not connected: " + str(v.get("error") or "not set up")))' 2>/dev/null || true
  else
    warn "it didn't answer yet. Look at the log:  ${LOGS:-}"
  fi
fi

say "Done"
note "Open it:            $URL"
note "In Hermes Studio:   the Upstream tab (Studio looks for it at http://127.0.0.1:5001;"
note "                    on another port, start Studio with UPSTREAM_UPKEEP_TARGET=$URL)"
note "Connections:        edit $DIR/.env, then restart the service; or use Settings in the app"
note "Give Hermes its tools:  hermes mcp add upstream --command $DIR/.venv/bin/upstream-mcp --env UPSTREAM_APP_URL=$URL"
[ -n "${LOGS:-}" ] && note "Logs:               $LOGS"
exit 0
