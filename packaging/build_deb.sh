#!/bin/bash
# Construit le paquet .deb de Sentinelle.
# Usage local (Debian/Ubuntu) :  bash packaging/build_deb.sh
# Usage via Docker (depuis Windows, à la racine du projet) :
#   docker run --rm -v "${PWD}:/src" -w /src debian:13 bash packaging/build_deb.sh
# IMPORTANT : construire sur la MÊME version de Debian que les postes cibles
# (Debian 13/trixie) — un binaire construit sur bookworm embarque des .so
# d'une autre génération qui se mélangent mal à la libmpv du système.
set -euo pipefail

VERSION=$(grep -oP '__version__ = "\K[^"]+' sentinelle/__init__.py)
ARCH=amd64
PKG=sentinelle_${VERSION}_${ARCH}

# --- dépendances de build (no-op si déjà présentes) ---
# libpythonX.Y (celle de l'interpréteur du système : 3.11 sur bookworm, 3.13
# sur trixie…) : requise par PyInstaller ; libgl1/libegl1/… : requises pour
# que les hooks PyInstaller puissent charger PySide6 pendant l'analyse.
if ! command -v python3 >/dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends python3
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! dpkg -s "libpython${PYV}" >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends \
        python3-venv python3-pip "libpython${PYV}" binutils libmpv2 \
        libgl1 libegl1 libglib2.0-0 libxkbcommon0 libdbus-1-3 libfontconfig1
fi

# --- binaire PyInstaller ---
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --quiet -r requirements.txt "pyinstaller==6.*"
/tmp/venv/bin/pyinstaller --noconfirm --windowed --name sentinelle \
    --add-data "sentinelle/ui/sentinelle.png:sentinelle/ui" \
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

# libxcb-* / libxkbcommon-x11-0 : requises par le plugin Qt « xcb » (wheel
# PySide6), absentes d'un Debian GNOME (Wayland) minimal — sans elles,
# l'application se replierait en Wayland natif et les tuiles resteraient noires.
# va-driver-all : pilotes VA-API (décodage vidéo MATÉRIEL). En Depends, pas en
# Recommends : sans pilote, mpv retombe SILENCIEUSEMENT en décodage logiciel,
# ce qui sature le CPU et peut éteindre net les mini-PC sous la pointe de
# charge (un dpkg -i n'installe pas les Recommends). libmpv2 >= 0.34 : sw-fast.
cat > "$ROOT/DEBIAN/control" <<EOF
Package: sentinelle
Version: ${VERSION}
Section: video
Priority: optional
Architecture: ${ARCH}
Depends: libmpv2 (>= 0.34), libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-xkb1, libxkbcommon-x11-0, va-driver-all
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
TryExec=/opt/sentinelle/sentinelle
Exec=/opt/sentinelle/sentinelle
Icon=sentinelle
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=sentinelle
Actions=SafeVideo;

[Desktop Action SafeVideo]
Name=Mode video sur (sans acceleration)
Exec=/opt/sentinelle/sentinelle --safe-video
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

mkdir -p dist    # gitignoré : absent d'un clone frais, dpkg-deb ne le crée pas
dpkg-deb --build --root-owner-group "$ROOT" "dist/${PKG}.deb"
echo "OK -> dist/${PKG}.deb"
