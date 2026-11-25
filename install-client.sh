#!/usr/bin/env bash
# Install the huta client on a dev box (as your normal user, not root), for the push transport.
#
#   ./install-client.sh --box NAME --keygen-only            # 1. make the key, print the public line
#   (on the server: sudo huta enroll NAME --from <this box's IP> --pubkey '<that line>' > s.json)
#   ./install-client.sh --box NAME --settings s.json        # 2. install with the server's settings
# or, by hand:
#   ./install-client.sh --box NAME --ssh huta-NAME@HOST --port PORT --registry HOST:PORT \
#                       --host-key "ssh-ed25519 AAAA…"
# The full agent-guided flow: https://huta.run/install.md
#
# What it does, all under your home directory:
#   ~/.local/lib/huta-client/huta    the client (copied from this checkout)
#   ~/.local/bin/huta                launcher
#   ~/.ssh/id_huta_push(.pub)        a NEW key just for huta (made once, never reused)
#   ~/.config/huta/client.toml       transport = "push"
#   ~/.config/huta/known_hosts       the host's key, pinned (no trust-on-first-use)
# Then it prints the public key. Give it to the operator; after they add it, run `huta login`.
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -ne 0 ]] || { echo "run as your normal user, not root" >&2; exit 1; }

BOX="" SSH="" PORT="22" REGISTRY="" HOSTKEY="" SETTINGS="" KEYGEN_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --box) BOX="$2"; shift 2 ;;
    --ssh) SSH="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --host-key) HOSTKEY="$2"; shift 2 ;;
    --settings) SETTINGS="$2"; shift 2 ;;   # the JSON `huta enroll` printed on the server
    --keygen-only) KEYGEN_ONLY=1; shift ;; # step 1: make the key, print the public line, stop
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ "$BOX" =~ ^[a-z][a-z0-9-]{0,26}$ ]] || { echo "--box: lowercase letters, digits, dashes; at most 27" >&2; exit 2; }

KEY="$HOME/.ssh/id_huta_push"
if [[ -n "$KEYGEN_ONLY" ]]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  [[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N "" -C "huta-push@$BOX" -f "$KEY"
  cat "$KEY.pub"
  exit 0
fi
if [[ -n "$SETTINGS" ]]; then
  # Read the enroll output as data (never sourced); the checks below validate every value.
  eval "$(python3 -I - "$SETTINGS" "$BOX" <<'PY'
import json, shlex, sys
d = json.load(open(sys.argv[1]))
if d.get("box") != sys.argv[2]:
    sys.exit(f"settings are for box {d.get('box')!r}, not {sys.argv[2]!r}")
for k, v in (("SSH", d["ssh"]), ("PORT", str(d["port"])), ("REGISTRY", d["registry"]),
             ("HOSTKEY", d["host_key"])):
    print(f"{k}={shlex.quote(v)}")
PY
)"
fi
[[ -n "$SSH" && -n "$REGISTRY" && -n "$HOSTKEY" ]] || { echo "--ssh, --registry and --host-key are required" >&2; exit 2; }
# Everything below is written into TOML and known_hosts: accept only plain values.
[[ "$SSH" =~ ^huta-[a-z][a-z0-9-]{0,26}@[A-Za-z0-9.:-]+$ ]] || { echo "--ssh must be huta-<box>@<host>" >&2; exit 2; }
[[ "${SSH%%@*}" == "huta-$BOX" ]] || { echo "--ssh user must be huta-$BOX" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && (( PORT >= 1 && PORT <= 65535 )) || { echo "--port: 1..65535" >&2; exit 2; }
[[ "$REGISTRY" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || { echo "--registry: host[:port]" >&2; exit 2; }
[[ "$HOSTKEY" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+$ ]] || { echo "--host-key: 'ssh-ed25519 AAAA…'" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 (3.11+) is required" >&2; exit 1; }
python3 -c 'import sys, tomllib; sys.exit(sys.version_info < (3, 11))' || { echo "python 3.11+ required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

LIB="$HOME/.local/lib/huta-client"
rm -rf "$LIB.new"; mkdir -p "$LIB.new" "$HOME/.local/bin" "$HOME/.config/huta" "$HOME/.ssh"
cp -R src/huta "$LIB.new/huta"
find "$LIB.new" -name __pycache__ -prune -exec rm -rf {} +
# The installed revision: the client compares it with the host's and updates itself.
if [[ -f REVISION ]]; then cp REVISION "$LIB.new/REVISION"            # from a client bundle
elif rev=$(git rev-parse HEAD 2>/dev/null); then
  git diff --quiet HEAD -- src 2>/dev/null || rev="$rev (dirty)"
  printf '%s\n' "$rev" > "$LIB.new/REVISION"
else echo unknown > "$LIB.new/REVISION"; fi
cp install-client.sh "$LIB.new/install-client.sh"
if [[ -d "$LIB" ]]; then mv "$LIB" "$LIB.old"; fi
mv "$LIB.new" "$LIB" && rm -rf "$LIB.old"

cat > "$HOME/.local/bin/huta" <<'EOF'
#!/bin/sh
exec python3 -I -B -c 'import sys, os; sys.path.insert(0, os.path.expanduser("~/.local/lib/huta-client")); from huta.client import main; sys.exit(main())' "$@"
EOF
chmod 0755 "$HOME/.local/bin/huta"

[[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N "" -C "huta-push@$BOX" -f "$KEY"

HOST="${SSH#*@}"
if [[ "$PORT" == "22" ]]; then KH_HOST="$HOST"; else KH_HOST="[$HOST]:$PORT"; fi
printf '%s %s\n' "$KH_HOST" "$HOSTKEY" > "$HOME/.config/huta/known_hosts"

cat > "$HOME/.config/huta/client.toml" <<EOF
# huta client on $BOX: push transport. Written by install-client.sh.
transport = "push"

[push]
registry = "$REGISTRY"
ssh = "$SSH"
port = $PORT
key = "~/.ssh/id_huta_push"
known_hosts = "~/.config/huta/known_hosts"
EOF

echo
echo "huta client installed for box '$BOX'. Make sure ~/.local/bin is on your PATH."
if [[ -n "$SETTINGS" ]]; then
  echo "The box is enrolled. Next: huta login   (then: huta pack && huta submit <target>)"
else
  echo "If the operator hasn't enrolled this key yet, give them this PUBLIC key (safe to share):"
  echo
  cat "$KEY.pub"
  echo
  echo "After they add it: huta login   (then: huta pack && huta submit <target>)"
fi
