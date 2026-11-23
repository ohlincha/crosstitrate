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
install -Dm644 LICENSE "$PKG/usr/share/doc/crosstitrate/copyright"
install -Dm644 README.md "$PKG/usr/share/doc/crosstitrate/README.md"
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
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    echo "crosstitrate: to use serial ports, add your user to the dialout group:"
    echo "    sudo usermod -aG dialout \$USER   (then log out and in again)"
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p dist
fakeroot dpkg-deb --build --root-owner-group "$PKG" dist/ 2>/dev/null || dpkg-deb --build --root-owner-group "$PKG" dist/
ls -l dist/*.deb
