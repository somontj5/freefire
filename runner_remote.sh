#!/usr/bin/env bash
set -euo pipefail
# Never use shell tracing: credentials must not be printed.
export DISPLAY=:99
export GENSPARK_PRIVATE_DIR="${GENSPARK_PRIVATE_DIR:-$PWD/private-runtime}"
mkdir -p "$GENSPARK_PRIVATE_DIR"
chmod 700 "$GENSPARK_PRIVATE_DIR"
if [[ -z "${REMOTE_DESKTOP_PASSWORD:-}" || ${#REMOTE_DESKTOP_PASSWORD} -lt 8 ]]; then
  echo 'Set REMOTE_DESKTOP_PASSWORD secret (at least 8 characters).'; exit 1
fi
# Classic VNC uses the first 8 characters; tailnet access control is the primary boundary.
python runner_state.py vnc-password
unset REMOTE_DESKTOP_PASSWORD
chmod 600 "$GENSPARK_PRIVATE_DIR/vnc.pass"
nohup Xvfb :99 -screen 0 1280x800x24 -nolisten tcp > /tmp/ff-xvfb.log 2>&1 &
for i in $(seq 1 30); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 1; done
xdpyinfo -display :99 >/dev/null
nohup fluxbox > /tmp/ff-window.log 2>&1 &
nohup x11vnc -display :99 -localhost -rfbauth "$GENSPARK_PRIVATE_DIR/vnc.pass" -rfbport 5900 -forever -shared > /tmp/ff-vnc.log 2>&1 &
nohup websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 > /tmp/ff-websockify.log 2>&1 &
nohup python remote_control.py > /tmp/ff-control.log 2>&1 &
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null && curl -fsS http://127.0.0.1:6081/ >/dev/null && break; sleep 1; done
curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null
curl -fsS http://127.0.0.1:6081/ >/dev/null
# Serve is private to the tailnet, NEVER Funnel. HTTPS must be enabled in tailnet.
sudo tailscale serve --bg --https=443 http://127.0.0.1:6080
sudo tailscale serve --bg --https=8443 http://127.0.0.1:6081
python runner_state.py remote-links
printf 'DISPLAY=:99\nGENSPARK_REMOTE=1\nGENSPARK_HEADLESS=0\n' >> "$GITHUB_ENV"
