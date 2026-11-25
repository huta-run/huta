#!/usr/bin/env bash
# Install huta on the host from this checkout. Run as root, only after reviewing the diff:
# huta is the trusted base and is never deployed by huta (CLAUDE.md).
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

DEST=/opt/huta

# Never replace the code under a release or recovery in progress: the new code would inherit a
# half-done phase it may read differently (DESIGN-v2 §0). HUTA_INSTALL_BUSY_OK=1 overrides.
if [[ -d /var/lib/huta && "${HUTA_INSTALL_BUSY_OK:-}" != 1 ]]; then
  busy=$(python3 -I -B -c 'import sys; sys.path.insert(0, "src"); from huta.store import busy
print("\n".join(busy("/var/lib/huta")))') || { echo "could not check whether huta is busy" >&2; exit 1; }
  if [[ -n "$busy" ]]; then
    printf 'huta is busy, not installing:\n%s\n' "$busy" >&2
    exit 1
  fi
fi
rev=$(git rev-parse HEAD)
dirty=$(git status --porcelain -- src/huta | wc -l)

install -d -m 0755 -o root -g root "$DEST" "$DEST/lib"
rm -rf "$DEST/lib/huta.new"
cp -r src/huta "$DEST/lib/huta.new"
find "$DEST/lib/huta.new" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$DEST/lib/huta.new"
chmod -R go-w "$DEST/lib/huta.new"
rm -rf "$DEST/lib/huta.old"
[[ -d "$DEST/lib/huta" ]] && mv "$DEST/lib/huta" "$DEST/lib/huta.old"
mv "$DEST/lib/huta.new" "$DEST/lib/huta"

cat > /usr/local/sbin/huta <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -I -B -c 'import sys; sys.path.insert(0, "/opt/huta/lib"); from huta.cli import main; sys.exit(main())' "$@"
EOF
chmod 0755 /usr/local/sbin/huta
install -m 0755 -o root -g root install-client.sh "$DEST/install-client.sh"   # served by `huta client-bundle`
printf '%s%s\n' "$rev" "$([[ $dirty -gt 0 ]] && echo ' (dirty)')" > "$DEST/REVISION"
install -d -m 0700 /var/lib/huta
# The egress daemon's unit: installed and kept current, but enabled by the operator.
install -m 0644 -o root -g root systemd/huta-egress.service /etc/systemd/system/huta-egress.service
# The log collector (huta logs): installed, kept current and enabled.
install -m 0644 -o root -g root systemd/huta-logs.service /etc/systemd/system/huta-logs.service
install -m 0644 -o root -g root systemd/huta-logs.timer /etc/systemd/system/huta-logs.timer
# The monitoring tick: installed, kept current and enabled; it does nothing without [monitoring].
install -m 0644 -o root -g root systemd/huta-monitor.service /etc/systemd/system/huta-monitor.service
install -m 0644 -o root -g root systemd/huta-monitor.timer /etc/systemd/system/huta-monitor.timer
# The pull transport's units, kept current where the operator installed them.
for u in huta-intake.service huta-intake.path; do
  if [[ -f /etc/systemd/system/$u ]]; then
    install -m 0644 -o root -g root "systemd/$u" "/etc/systemd/system/$u"
  fi
done
# The push forced command, kept current where the operator installed it.
if [[ -f /usr/local/sbin/huta-ssh ]]; then
  install -m 0755 -o root -g root systemd/huta-ssh /usr/local/sbin/huta-ssh
fi
systemctl daemon-reload
systemctl enable --now huta-logs.timer
systemctl enable --now huta-monitor.timer
if systemctl is-enabled --quiet huta-intake.path 2>/dev/null; then
  systemctl reset-failed huta-intake.service huta-intake.path 2>/dev/null || true
  systemctl restart huta-intake.path
fi
if systemctl is-active --quiet huta-egress; then systemctl restart huta-egress; fi
echo "installed huta $(cat "$DEST/REVISION")"
