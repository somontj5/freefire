#!/usr/bin/env bash
set -euo pipefail
# No shell tracing and no certificate/key contents in logs.
umask 077
export DISPLAY=:99
export GENSPARK_PRIVATE_DIR="${GENSPARK_PRIVATE_DIR:-$PWD/private-runtime}"
mkdir -p "$GENSPARK_PRIVATE_DIR"
chmod 700 "$GENSPARK_PRIVATE_DIR"

if [[ -z "${REMOTE_DESKTOP_PASSWORD:-}" || ${#REMOTE_DESKTOP_PASSWORD} -lt 8 ]]; then
  echo '::error::Set REMOTE_DESKTOP_PASSWORD secret (at least 8 ASCII characters).'
  exit 1
fi

host=$(sudo tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); h=d["Self"]["DNSName"].rstrip("."); assert d.get("BackendState")=="Running", "Tailscale is not connected"; assert h.endswith(".ts.net") and all(c.isalnum() or c in ".-" for c in h), "Invalid tailnet DNS name"; print(h)')

cert_file="$GENSPARK_PRIVATE_DIR/tls-check.crt"
key_file="$GENSPARK_PRIVATE_DIR/tls-check.key"
cert_log="$GENSPARK_PRIVATE_DIR/tls-check.log"
cleanup_tls() {
  sudo rm -f -- "$cert_file" "$key_file" "$cert_log" || true
}
trap cleanup_tls EXIT

echo 'Requesting the Tailscale HTTPS certificate once, before publishing browser links.'
set +e
sudo timeout --kill-after=5s 90s tailscale cert \
  --cert-file="$cert_file" --key-file="$key_file" "$host" >"$cert_log" 2>&1
cert_rc=$?
set -e
if [[ $cert_rc -ne 0 ]]; then
  echo '::error::Tailscale certificate provisioning failed; this is before the Genspark login.'
  # tailscale cert normally outputs status/errors, never PEM when output paths are given.
  # Filter nevertheless so diagnostics cannot print keys or tokens.
  sudo python3 - "$cert_log" <<'PY'
from pathlib import Path
import re,sys
text=Path(sys.argv[1]).read_text(encoding='utf8',errors='replace')
text=re.sub(r'-----BEGIN [^-]+-----.*?-----END [^-]+-----','[PEM REDACTED]',text,flags=re.S)
text=re.sub(r'tskey-[A-Za-z0-9_-]+','[TOKEN REDACTED]',text)
print(text[-6000:])
PY
  if [[ $cert_rc -eq 124 || $cert_rc -eq 137 ]]; then
    echo 'Certificate request timed out. Check HTTPS Certificates in Tailscale DNS and any ACME/rate-limit message above.'
  fi
  echo 'Do not keep creating new runs if the error says rateLimited / 429 / retry after.'
  exit 1
fi
sudo openssl x509 -in "$cert_file" -noout -checkend 60
cleanup_tls
trap - EXIT

python runner_state.py vnc-password
unset REMOTE_DESKTOP_PASSWORD
chmod 600 "$GENSPARK_PRIVATE_DIR/vnc.pass"
nohup Xvfb :99 -screen 0 1280x800x24 -nolisten tcp > /tmp/ff-xvfb.log 2>&1 &
for i in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 1
done
xdpyinfo -display :99 >/dev/null
nohup fluxbox > /tmp/ff-window.log 2>&1 &
nohup x11vnc -display :99 -localhost -rfbauth "$GENSPARK_PRIVATE_DIR/vnc.pass" -rfbport 5900 -forever -shared > /tmp/ff-vnc.log 2>&1 &
nohup websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 > /tmp/ff-websockify.log 2>&1 &
nohup python remote_control.py > /tmp/ff-control.log 2>&1 &

ready=0
for i in $(seq 1 30); do
  if curl --noproxy '*' -fsS http://127.0.0.1:6080/vnc.html >/dev/null 2>&1 && \
     curl --noproxy '*' -fsS http://127.0.0.1:6081/ >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo '::error::Local browser/control services did not start within 30 seconds. Check x11vnc/websockify logs.'
  exit 1
fi

# Private Serve only. Do not use Funnel, curl -k, or public forwarding.
sudo tailscale serve --yes --bg --https=443 http://127.0.0.1:6080
sudo tailscale serve --yes --bg --https=8443 http://127.0.0.1:6081
sudo tailscale serve status
curl --noproxy '*' --fail --show-error --silent --connect-timeout 5 --max-time 20 \
  "https://${host}/vnc.html" --output /dev/null
curl --noproxy '*' --fail --show-error --silent --connect-timeout 5 --max-time 20 \
  "https://${host}:8443/" --output /dev/null

# Links are published only AFTER certificate and actual HTTPS checks succeeded.
python runner_state.py remote-links
printf 'DISPLAY=:99\nGENSPARK_REMOTE=1\nGENSPARK_HEADLESS=0\n' >> "$GITHUB_ENV"
echo 'Private HTTPS screen and control page passed checks on this runner.'
