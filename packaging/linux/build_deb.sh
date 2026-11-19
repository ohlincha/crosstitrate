#!/bin/sh
# Build dist/titrate_<version>_all.deb (needs dpkg-deb; fakeroot when not root).
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('titrate/__init__.py').read()).group(1))")
PKG=build/deb/titrate_${VERSION}_all
rm -rf "$PKG"
mkdir -p "$PKG/usr/lib/titrate" "$PKG/usr/bin" "$PKG/DEBIAN"
find titrate -name '*.py' | while read -r f; do install -Dm644 "$f" "$PKG/usr/lib/titrate/$f"; done
install -Dm644 packaging/linux/titrate.desktop "$PKG/usr/share/applications/titrate.desktop"
install -Dm644 LICENSE "$PKG/usr/share/doc/titrate/copyright"
install -Dm644 README.md "$PKG/usr/share/doc/titrate/README.md"
printf '#!/bin/sh\nPYTHONPATH=/usr/lib/titrate exec python3 -m titrate "$@"\n' > "$PKG/usr/bin/titrate"
chmod 755 "$PKG/usr/bin/titrate"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: titrate
Version: $VERSION
Architecture: all
Maintainer: titrate maintainers <noreply@github.com>
Depends: python3 (>= 3.8), python3-tk, python3-serial
Section: science
Priority: optional
Homepage: https://github.com/ohlincha/titrate
Description: Titration control for Metrohm Titrino instruments
 Runs potentiometric titrations with Metrohm Titrinos (718, 751, 785, ...)
 over RS232 or USB-RS232 adapters: live plot, burette changes, data files,
 connection diagnostics and a built-in simulator.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    echo "titrate: to use serial ports, add your user to the dialout group:"
    echo "    sudo usermod -aG dialout \$USER   (then log out and in again)"
fi
POST
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p dist
fakeroot dpkg-deb --build --root-owner-group "$PKG" dist/ 2>/dev/null || dpkg-deb --build --root-owner-group "$PKG" dist/
ls -l dist/*.deb
