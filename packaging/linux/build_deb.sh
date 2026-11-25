#!/bin/sh
# Build dist/crosstitrate_<version>_all.deb (needs dpkg-deb; fakeroot when not root).
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('crosstitrate/__init__.py').read()).group(1))")
PKG=build/deb/crosstitrate_${VERSION}_all
rm -rf "$PKG"
mkdir -p "$PKG/usr/lib/crosstitrate" "$PKG/usr/bin" "$PKG/DEBIAN"
find crosstitrate -name '*.py' | while read -r f; do install -Dm644 "$f" "$PKG/usr/lib/crosstitrate/$f"; done
install -Dm644 crosstitrate/gui/icon.png "$PKG/usr/lib/crosstitrate/crosstitrate/gui/icon.png"
install -Dm644 crosstitrate/gui/icon.png "$PKG/usr/share/icons/hicolor/256x256/apps/crosstitrate.png"
install -Dm644 packaging/icon.svg "$PKG/usr/share/icons/hicolor/scalable/apps/crosstitrate.svg"
install -Dm644 packaging/linux/crosstitrate.desktop "$PKG/usr/share/applications/crosstitrate.desktop"
install -Dm644 packaging/linux/70-crosstitrate.rules "$PKG/usr/lib/udev/rules.d/70-crosstitrate.rules"
install -Dm644 LICENSE "$PKG/usr/share/doc/crosstitrate/copyright"
install -Dm644 README.md "$PKG/usr/share/doc/crosstitrate/README.md"
# Help > Manual looks for docs/MANUAL.html next to the package
install -Dm644 docs/MANUAL.html "$PKG/usr/lib/crosstitrate/docs/MANUAL.html"
install -Dm644 docs/CrossTitrate-Manual.pdf "$PKG/usr/share/doc/crosstitrate/CrossTitrate-Manual.pdf"
printf '#!/bin/sh\nPYTHONPATH=/usr/lib/crosstitrate exec python3 -m crosstitrate "$@"\n' > "$PKG/usr/bin/crosstitrate"
chmod 755 "$PKG/usr/bin/crosstitrate"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: crosstitrate
Version: $VERSION
Architecture: all
Maintainer: crosstitrate maintainers <noreply@github.com>
Depends: python3 (>= 3.8), python3-tk, python3-serial
Section: science
Priority: optional
Homepage: https://github.com/ohlincha/crosstitrate
Description: Titration control for Metrohm Titrino instruments
 Runs potentiometric titrations with Metrohm Titrinos (718, 751, 785, ...)
 over RS232 or USB-RS232 adapters: live plot, burette changes, data files,
 connection diagnostics and a built-in simulator.
 Includes a udev rule so every user logged in at the computer can use the
 serial ports without being in the dialout group.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload || true
    udevadm trigger --subsystem-match=tty --action=change || true
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p dist
fakeroot dpkg-deb --build --root-owner-group "$PKG" dist/ 2>/dev/null || dpkg-deb --build --root-owner-group "$PKG" dist/
ls -l dist/*.deb
