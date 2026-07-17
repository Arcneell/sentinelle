#!/bin/bash
# Construit le paquet .deb de Sentinelle.
# Usage local (Debian/Ubuntu) :  bash packaging/build_deb.sh
# Usage via Docker (depuis Windows, à la racine du projet) :
#   docker run --rm -v "${PWD}:/src" -w /src debian:12 bash packaging/build_deb.sh
set -euo pipefail

VERSION=$(grep -oP '__version__ = "\K[^"]+' rtsp_tool/__init__.py)
ARCH=amd64
PKG=sentinelle_${VERSION}_${ARCH}

# --- dépendances de build (no-op si déjà présentes) ---
# libpython3.11 : requise par PyInstaller ; libgl1/libegl1/… : requises pour
# que les hooks PyInstaller puissent charger PySide6 pendant l'analyse.
if ! command -v python3 >/dev/null || ! dpkg -s libpython3.11 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip libpython3.11 binutils libmpv2 \
        libgl1 libegl1 libglib2.0-0 libxkbcommon0 libdbus-1-3 libfontconfig1
fi

# --- binaire PyInstaller ---
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --quiet -r requirements.txt pyinstaller
/tmp/venv/bin/pyinstaller --noconfirm --windowed --name sentinelle \
    --add-data "rtsp_tool/ui/sentinelle.png:rtsp_tool/ui" \
    --distpath /tmp/dist --workpath /tmp/build run.py

# --- arborescence du paquet ---
ROOT=/tmp/${PKG}
rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" "$ROOT/opt/sentinelle" "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$ROOT/usr/share/icons/hicolor/scalable/apps"
cp -r /tmp/dist/sentinelle/. "$ROOT/opt/sentinelle/"

# icône de l'application (PNG 256 + SVG scalable)
cp packaging/sentinelle.png "$ROOT/usr/share/icons/hicolor/256x256/apps/sentinelle.png"
cp packaging/sentinelle.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/sentinelle.svg"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: sentinelle
Version: ${VERSION}
Section: video
Priority: optional
Architecture: ${ARCH}
Depends: libmpv2 | libmpv1
Recommends: ffmpeg
Maintainer: Sentinelle <sentinelle@example.com>
Description: Visionneuse de videosurveillance multi-sites (RTSP, ONVIF)
 Visualisation en grille/mono de cameras RTSP (Hikvision, Dahua, ONVIF),
 detection de mouvement ONVIF, gestion economique de la bande passante,
 rotation automatique et boucles configurables.
EOF

ln -sf /opt/sentinelle/sentinelle "$ROOT/usr/bin/sentinelle"

cat > "$ROOT/usr/share/applications/sentinelle.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Sentinelle
GenericName=Videosurveillance
Comment=Visionneuse de videosurveillance multi-sites
Exec=/opt/sentinelle/sentinelle
Icon=sentinelle
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=sentinelle
EOF

# rafraîchit le cache des icônes et du menu après (dés)installation
cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi
EOF
cp "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"
chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"

dpkg-deb --build --root-owner-group "$ROOT" "dist/${PKG}.deb"
echo "OK -> dist/${PKG}.deb"
