#!/bin/sh
# Sestaví mtcontroller_<verze>_all.deb (jen dpkg-deb, bez debhelperu).
#   packaging/build-deb.sh [výstupní adresář]
set -eu
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$SRC/dist}"
BASE=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/src/mtcontroller/__init__.py")
REV=$(git -C "$SRC" rev-list --count HEAD 2>/dev/null || echo 0)
HASH=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo local)
VERSION="$BASE+$REV.g$HASH"
ROOT=$(mktemp -d)
chmod 0755 "$ROOT"
trap 'rm -rf "$ROOT"' EXIT

APP="$ROOT/opt/mtcontroller/app"
install -d "$APP" "$ROOT/DEBIAN" "$ROOT/usr/sbin" "$ROOT/lib/systemd/system" \
  "$ROOT/usr/share/mtcontroller/nginx" "$ROOT/usr/share/doc/mtcontroller"
cp -a "$SRC/src/mtcontroller" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
echo "$VERSION" > "$APP/VERSION"

cat > "$ROOT/usr/sbin/mtc-admin" <<'EOS'
#!/bin/sh
exec env PYTHONPATH=/opt/mtcontroller/app MTC_CONFIG=/etc/mtcontroller/config.toml python3 -m mtcontroller.cli "$@"
EOS
chmod 0755 "$ROOT/usr/sbin/mtc-admin"

for u in mtc-web.service mtc-worker.service mtc-syslog.service mtc-setup.service mtc-setup-apply.service mtc-setup-apply.path \
         mtc-backup.service mtc-backup.timer mtc-backup.path; do
  install -m 0644 "$SRC/deploy/systemd/$u" "$ROOT/lib/systemd/system/$u"
done
install -m 0644 "$SRC/deploy/common.sh" "$ROOT/usr/share/mtcontroller/common.sh"
install -m 0644 "$SRC/deploy/nginx/mtcontroller.conf" "$SRC/deploy/nginx/mtcontroller-local.conf" \
  "$SRC/deploy/nginx/mtcontroller-setup.conf" "$ROOT/usr/share/mtcontroller/nginx/"
install -m 0644 "$SRC/deploy/systemd/mtc-worker-routes.conf" "$ROOT/usr/share/mtcontroller/"
install -m 0644 "$SRC/config.toml.example" "$ROOT/usr/share/mtcontroller/config.toml.example"
install -m 0644 "$SRC/README.md" "$ROOT/usr/share/doc/mtcontroller/README.md"
[ -d "$SRC/docs" ] && cp -a "$SRC/docs/." "$ROOT/usr/share/doc/mtcontroller/"

sed "s/@VERSION@/$VERSION/" "$SRC/packaging/debian/control.in" > "$ROOT/DEBIAN/control"
echo "Installed-Size: $(du -sk "$ROOT" | cut -f1)" >> "$ROOT/DEBIAN/control"
for f in postinst prerm postrm; do
  install -m 0755 "$SRC/packaging/debian/$f" "$ROOT/DEBIAN/$f"
done
chmod -R u=rwX,go=rX "$ROOT/opt" "$ROOT/usr" "$ROOT/lib"
mkdir -p "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT/mtcontroller_${VERSION}_all.deb"
