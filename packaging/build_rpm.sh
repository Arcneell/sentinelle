#!/bin/bash
# Construit le paquet .rpm de Sentinelle (Fedora, et par extension RHEL/Rocky
# récents si les dépendances y existent).
# Usage local (Fedora) :  bash packaging/build_rpm.sh
# Usage via Docker (depuis Windows, à la racine du projet) :
#   docker run --rm -v "${PWD}:/src" -w /src fedora:42 bash packaging/build_rpm.sh
# Construire sur la version de Fedora LA PLUS ANCIENNE encore visée : le binaire
# PyInstaller se lie à la glibc de l'hôte de build, un paquet construit sur
# Fedora 42 tourne sur 43/44, l'inverse non.
set -euo pipefail

VERSION=$(grep -oP '__version__ = "\K[^"]+' sentinelle/__init__.py)
RELEASE=1
ARCH=x86_64
NOM=sentinelle-${VERSION}-${RELEASE}.${ARCH}

# --- dépendances de build (no-op si déjà présentes) ---
# Même règle que build_deb.sh : mpv-libs volontairement ABSENTE du conteneur de
# build. Présente, PyInstaller suit le chargement ctypes de python-mpv et
# embarque libmpv + toute sa pile ffmpeg/libva ; cette libva du bundle masque
# celle du système (LD_LIBRARY_PATH du bootloader) et casse VA-API sur les
# postes : repli silencieux en décodage logiciel, tuiles noires. La pile vidéo
# vient des Requires du paquet, jamais du bundle.
if rpm -q mpv-libs >/dev/null 2>&1; then
    echo "ERREUR : mpv-libs est installée dans l'environnement de build —" >&2
    echo "PyInstaller l'embarquerait et casserait VA-API sur les postes." >&2
    echo "Construire dans un conteneur fedora nu (voir l'en-tête)." >&2
    exit 1
fi
if ! command -v rpmbuild >/dev/null || ! command -v pyinstaller >/dev/null 2>&1; then
    # python3-libs : libpython requise par PyInstaller ; mesa-libGL/EGL, glib2,
    # libxkbcommon, dbus-libs, fontconfig, krb5-libs : requises pour que les
    # hooks PyInstaller chargent PySide6 pendant l'analyse.
    dnf install -y --setopt=install_weak_deps=False \
        python3 python3-pip python3-libs binutils rpm-build \
        mesa-libGL mesa-libEGL glib2 libxkbcommon dbus-libs fontconfig krb5-libs \
        grep findutils
fi

# --- binaire PyInstaller ---
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --quiet -r requirements.txt "pyinstaller==6.*"
/tmp/venv/bin/pyinstaller --noconfirm --windowed --name sentinelle \
    --add-data "sentinelle/ui/sentinelle.png:sentinelle/ui" \
    --distpath /tmp/dist --workpath /tmp/build run.py

# GARDE-FOU : aucune bibliothèque de la pile vidéo ne doit être embarquée —
# elle court-circuiterait mpv-libs/libva installées par les Requires.
for lib in libmpv libva libavcodec; do
    if compgen -G "/tmp/dist/sentinelle/_internal/${lib}*" > /dev/null; then
        echo "ERREUR : ${lib}* trouvée dans le bundle PyInstaller." >&2
        exit 1
    fi
done

# AUCUNE bibliothèque système du conteneur de build ne doit être embarquée.
# Le bootloader PyInstaller pose LD_LIBRARY_PATH=_internal : toute lib système
# copiée là MASQUE celle du poste pour l'ensemble du processus, y compris pour
# libmpv/ffmpeg/pango installées par les Requires, construites contre des
# versions plus récentes. Constaté sur Fedora 44 avec un bundle fedora:42 :
#  - libxkbcommon 1.8 embarquée + libxkbcommon-x11 1.13 système : segfault
#    (symboles privés) à la création de QApplication ;
#  - libfontconfig embarquée + pango système : « undefined symbol:
#    FcConfigSetDefaultSubstitute », libmpv inchargeable, aucune vidéo.
# Règle : chaque lib*.so* de premier niveau appartenant à un paquet du conteneur
# (rpm -qf) est retirée du bundle et son soname devient un Requires du paquet
# (ex. « libfontconfig.so.1()(64bit) »), résolu par rpm quelle que soit la
# version de Fedora. Les libs des wheels (Qt, shiboken, ICU de Qt) n'ont pas de
# propriétaire rpm et restent. libpython reste : la version du conteneur n'est
# pas celle du poste.
SONAMES_SYSTEME=()
for f in /tmp/dist/sentinelle/_internal/lib*.so*; do
    n=$(basename "$f")
    case "$n" in libpython*) continue ;; esac
    if rpm -qf "/usr/lib64/$n" >/dev/null 2>&1; then
        rm -f "$f"
        SONAMES_SYSTEME+=("$n")
    fi
done
echo "Libs système retirées du bundle : ${#SONAMES_SYSTEME[@]}"
REQUIRES_SONAMES=$(for n in "${SONAMES_SYSTEME[@]}"; do
    printf 'Requires:       %s()(64bit)\n' "$n"
done)

# --- arborescence rpmbuild ---
TOP=/tmp/rpmbuild
rm -rf "$TOP"
mkdir -p "$TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
SRC="$TOP/SOURCES/sentinelle"
mkdir -p "$SRC"
cp -r /tmp/dist/sentinelle "$SRC/opt"
cp packaging/sentinelle.png packaging/sentinelle.svg "$SRC/"

cat > "$SRC/sentinelle.desktop" <<EOF
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

# Requires :
#  - mpv-libs >= 0.34 (sw-fast) : dépôt Fedora de base (ffmpeg-free).
#  - xcb-util-* / libxkbcommon-x11 : plugin Qt « xcb » de la wheel PySide6,
#    absents d'un Fedora Workstation (Wayland) minimal — sans eux Qt se replie
#    en Wayland natif et les tuiles restent noires.
#  - libva : bibliothèque VA-API. Les PILOTES (intel-media-driver,
#    mesa-va-drivers-freeworld) et libavcodec-freeworld (seul libavcodec avec
#    H264/HEVC : ffmpeg-free les désactive à la compilation) sont sur RPM Fusion,
#    hors dépôts de base : impossible de les exiger ici (Recommends, ignoré si le
#    dépôt manque) ; documentés dans le README.
# AutoReqProv désactivé : rpmbuild générerait sinon des Requires sur la version
# exacte de libpython embarquée et des Provides pour chaque .so du bundle.
# __os_install_post vidé : pas de strip ni de recompilation du bundle PyInstaller.
cat > "$TOP/SPECS/sentinelle.spec" <<EOF
%global __os_install_post %{nil}
%global _build_id_links none
%global debug_package %{nil}

Name:           sentinelle
Version:        ${VERSION}
Release:        ${RELEASE}
Summary:        Visionneuse de videosurveillance multi-sites (RTSP, ONVIF)
License:        AGPL-3.0-or-later
URL:            https://github.com/Arcneell/sentinelle
BuildArch:      ${ARCH}
AutoReqProv:    no

Requires:       mpv-libs >= 0.34
Requires:       libva
Requires:       xcb-util-cursor
Requires:       xcb-util-wm
Requires:       xcb-util-image
Requires:       xcb-util-keysyms
Requires:       xcb-util-renderutil
Requires:       libxkbcommon
Requires:       libxkbcommon-x11
Requires:       libxcb
Requires:       fontconfig
Requires:       glib2
Requires:       libglvnd-egl
Requires:       libglvnd-glx
Requires:       dbus-libs
Requires:       krb5-libs
Requires:       hicolor-icon-theme
${REQUIRES_SONAMES}
Recommends:     libva-utils
Recommends:     ffmpeg-free
Recommends:     libavcodec-freeworld

%description
Visualisation en grille/mono de cameras RTSP (Hikvision, Dahua, ONVIF),
detection de mouvement ONVIF, gestion economique de la bande passante,
rotation automatique et boucles configurables.

Depuis RPM Fusion, indispensables : libavcodec-freeworld (le ffmpeg-free de
Fedora n'a aucun decodeur H264/HEVC) et un pilote VA-API complet pour le
decodage materiel (intel-media-driver ou mesa-va-drivers-freeworld).

%install
mkdir -p %{buildroot}/opt %{buildroot}%{_bindir} \\
         %{buildroot}%{_datadir}/applications \\
         %{buildroot}%{_datadir}/icons/hicolor/256x256/apps \\
         %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
cp -a ${SRC}/opt %{buildroot}/opt/sentinelle
ln -s /opt/sentinelle/sentinelle %{buildroot}%{_bindir}/sentinelle
install -m 0644 ${SRC}/sentinelle.desktop %{buildroot}%{_datadir}/applications/
install -m 0644 ${SRC}/sentinelle.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/
install -m 0644 ${SRC}/sentinelle.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/

%files
/opt/sentinelle
%{_bindir}/sentinelle
%{_datadir}/applications/sentinelle.desktop
%{_datadir}/icons/hicolor/256x256/apps/sentinelle.png
%{_datadir}/icons/hicolor/scalable/apps/sentinelle.svg

%post
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor 2>/dev/null || :
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q %{_datadir}/applications 2>/dev/null || :
fi

%postun
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor 2>/dev/null || :
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q %{_datadir}/applications 2>/dev/null || :
fi

%changelog
* $(LC_ALL=C date +'%a %b %d %Y') Sentinelle <sentinelle@example.com> - ${VERSION}-${RELEASE}
- Paquet genere par packaging/build_rpm.sh
EOF

rpmbuild --define "_topdir $TOP" -bb "$TOP/SPECS/sentinelle.spec"

mkdir -p dist    # gitignoré : absent d'un clone frais
cp "$TOP/RPMS/${ARCH}/${NOM}.rpm" dist/
echo "OK -> dist/${NOM}.rpm"
