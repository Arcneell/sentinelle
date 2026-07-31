# Construit le paquet Windows PORTABLE de Sentinelle : un ZIP autonome qui
# tourne sur n'importe quel poste, sans installation et SANS EXÉCUTABLE À SIGNER.
#
# Pourquoi pas PyInstaller ici : l'exe produit est un binaire inédit, non signé,
# que les protections de poste (Symantec Endpoint Protection en tête) bloquent
# en « Accès refusé ». Le portable n'introduit aucun binaire nouveau : il embarque
# la distribution « embeddable » officielle de python.org (python.exe et
# pythonw.exe SIGNÉS par la Python Software Foundation) et notre code reste en
# .py. Rien à faire signer, l'antivirus n'a rien d'inconnu à se mettre sous la
# dent.
#
# Usage :
#   pwsh packaging/build_portable.ps1
#   pwsh packaging/build_portable.ps1 -PythonVersion 3.13.12   # version figée
#   pwsh packaging/build_portable.ps1 -NoPrune                 # sans élagage Qt
#
# Résultat : dist/Sentinelle-<version>-windows-portable.zip
#
# libmpv : reprise de lib/libmpv-2.dll si présente, sinon téléchargée depuis les
# builds shinchiro. La variante « v3 » est volontairement ÉCARTÉE : elle exige
# AVX2, absent des mini-PC type Celeron N4020 utilisés sur les murs d'images.

[CmdletBinding()]
param(
    # Branche Python visée. Le correctif est résolu automatiquement (le plus
    # récent publié) sauf si une version complète « 3.13.12 » est donnée.
    [string]$PythonVersion = "3.13",
    # Chemin d'une libmpv-2.dll déjà téléchargée (court-circuite le download).
    [string]$MpvDll = "",
    # Conserve l'intégralité de PySide6 (~250 Mo au lieu de ~90 Mo).
    [switch]$NoPrune
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # sinon Invoke-WebRequest rampe
Set-Location (Split-Path $PSScriptRoot -Parent)     # racine du dépôt

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host $m -ForegroundColor Green }

# --------------------------------------------------------------- version
$verLine = Select-String -Path "sentinelle/__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"'
$version = $verLine.Matches[0].Groups[1].Value
Info "Sentinelle $version — paquet Windows portable"

$name  = "Sentinelle-$version"
$stage = Join-Path (Get-Location) "build/portable/$name"
$zip   = Join-Path (Get-Location) "dist/$name-windows-portable.zip"
$cache = Join-Path (Get-Location) "build/portable/cache"

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage, $cache, "dist" | Out-Null

# ------------------------------------------------- Python « embeddable »
# Le correctif n'est pas figé dans le script : une version codée en dur devient
# fausse au prochain correctif de sécurité Python. On sonde python.org du plus
# récent au plus ancien et on prend le premier qui existe.
function Resolve-EmbeddableUrl([string]$branch) {
    if ($branch -match '^\d+\.\d+\.\d+$') {
        return "https://www.python.org/ftp/python/$branch/python-$branch-embed-amd64.zip"
    }
    foreach ($patch in 30..0) {
        $v = "$branch.$patch"
        $url = "https://www.python.org/ftp/python/$v/python-$v-embed-amd64.zip"
        try {
            Invoke-WebRequest -Method Head -Uri $url -TimeoutSec 20 | Out-Null
            return $url
        } catch { continue }
    }
    throw "Aucune distribution embeddable trouvée pour Python $branch."
}

$embedUrl = Resolve-EmbeddableUrl $PythonVersion
$pyFull   = [regex]::Match($embedUrl, 'python-([\d.]+)-embed').Groups[1].Value
$pyTag    = "python" + ($pyFull -split '\.')[0] + ($pyFull -split '\.')[1]   # python313
$pyShort  = ($pyFull -split '\.')[0] + "." + ($pyFull -split '\.')[1]        # 3.13
Info "Interpréteur : Python $pyFull (embeddable, signé PSF)"

$embedZip = Join-Path $cache "python-$pyFull-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -TimeoutSec 300
}
$pyDir = Join-Path $stage "python"
Expand-Archive -Path $embedZip -DestinationPath $pyDir -Force

# Le fichier ._pth fige sys.path et coupe le mécanisme `site` : sans retouche,
# Lib\site-packages est ignoré (« No module named PySide6 ») et le dossier du
# script n'est PAS ajouté au chemin non plus (« No module named sentinelle »).
# On ajoute donc explicitement `..` (la racine du portable, où vit run.py) et
# site-packages, puis on réactive `site` — requis par les .pth des paquets.
$pth = Join-Path $pyDir "$pyTag._pth"
if (-not (Test-Path $pth)) { throw "Fichier ._pth introuvable : $pth" }
@(
    "$pyTag.zip"
    "."
    ".."
    "Lib\site-packages"
    "import site"
) | Set-Content -Path $pth -Encoding ascii

# ------------------------------------------------------------ dépendances
# --platform/--python-version rendent la construction reproductible et
# permettent de fabriquer le paquet depuis n'importe quelle machine ; ils
# exigent --only-binary (aucune compilation locale).
$sitePkgs = Join-Path $pyDir "Lib/site-packages"
Info "Installation des dépendances (roues win_amd64, Python $pyShort)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --no-compile `
    --target $sitePkgs `
    --only-binary=:all: --platform win_amd64 `
    --python-version $pyShort --implementation cp `
    -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Échec de l'installation des dépendances." }

# --------------------------------------------------------------- libmpv
# Cherchée localement d'abord (dev Windows), téléchargée sinon. lib/ est
# gitignoré : un runner de CI part toujours sur le téléchargement.
$libDir = Join-Path $stage "lib"
New-Item -ItemType Directory -Force -Path $libDir | Out-Null

if (-not $MpvDll -and (Test-Path "lib/libmpv-2.dll")) { $MpvDll = "lib/libmpv-2.dll" }

if ($MpvDll) {
    Info "libmpv : reprise de $MpvDll"
    Copy-Item $MpvDll (Join-Path $libDir "libmpv-2.dll")
} else {
    $sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue)?.Source
    if (-not $sevenZip -and (Test-Path "$env:ProgramFiles\7-Zip\7z.exe")) {
        $sevenZip = "$env:ProgramFiles\7-Zip\7z.exe"
    }
    if (-not $sevenZip) {
        throw "7z.exe introuvable et lib/libmpv-2.dll absente : installez 7-Zip " +
              "ou passez -MpvDll <chemin\libmpv-2.dll>."
    }
    Info "libmpv : téléchargement du build shinchiro (x86_64 de base, sans AVX2)"
    # L'API GitHub anonyme est limitée à 60 requêtes/heure et par IP : sur un
    # runner partagé le quota est parfois déjà épuisé. On s'authentifie si un
    # jeton est disponible (GITHUB_TOKEN en CI).
    $headers = @{}
    $token = $env:GITHUB_TOKEN
    if (-not $token) { $token = $env:GH_TOKEN }
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    $rel = Invoke-RestMethod -TimeoutSec 60 -Headers $headers `
        -Uri "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    # « -v3- » exclu : ce build exige AVX2, absent des Celeron/Atom des murs.
    $asset = $rel.assets |
        Where-Object { $_.name -like "mpv-dev-x86_64-*" -and $_.name -notlike "*-v3-*" } |
        Select-Object -First 1
    if (-not $asset) { throw "Aucun asset mpv-dev-x86_64 dans la dernière version." }

    $archive = Join-Path $cache $asset.name
    if (-not (Test-Path $archive)) {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -TimeoutSec 600
    }
    & $sevenZip e $archive "-o$libDir" "libmpv-2.dll" -y | Out-Null
    if (-not (Test-Path (Join-Path $libDir "libmpv-2.dll"))) {
        throw "libmpv-2.dll absente de $($asset.name)."
    }
    Ok "libmpv : $($asset.name)"
}

# --------------------------------------------------- code de l'application
# player.py cherche la DLL dans <parent du paquet>/lib : la disposition
# racine/{sentinelle,lib,run.py} reproduit celle du dépôt, aucun code à adapter.
Copy-Item "run.py" $stage
Copy-Item "LICENSE" $stage
Copy-Item -Recurse "sentinelle" (Join-Path $stage "sentinelle")
Get-ChildItem -Recurse -Directory (Join-Path $stage "sentinelle") -Filter "__pycache__" |
    Remove-Item -Recurse -Force

# ------------------------------------------------------------- élagage Qt
# PySide6 pèse ~250 Mo parce qu'il embarque toute la pile Qt. Sentinelle
# n'importe que QtCore, QtGui et QtWidgets : le reste (3D, WebEngine, Quick/QML,
# multimédia, capteurs…) est retiré. Le test de fumée en fin de script est le
# garde-fou : s'il casse, l'élagage est allé trop loin.
if (-not $NoPrune) {
    $qtRoot = Join-Path $sitePkgs "PySide6"
    if (Test-Path $qtRoot) {
        $before = [math]::Round((Get-ChildItem -Recurse -File $sitePkgs |
                                 Measure-Object Length -Sum).Sum / 1MB)

        # Modules Qt inutilisés (DLL Qt6*, extensions Python Qt*.pyd, stubs).
        $modules = @(
            "Qt3D*", "QtWebEngine*", "QtWebView*", "QtWebChannel*", "QtWebSockets*",
            "QtQuick*", "QtQml*", "QtCharts*", "QtDataVisualization*", "QtGraphs*",
            "QtMultimedia*", "QtSpatialAudio*", "QtPdf*", "QtBluetooth*", "QtNfc*",
            "QtPositioning*", "QtLocation*", "QtSensors*", "QtSerialPort*",
            "QtSerialBus*", "QtTest*", "QtDesigner*", "QtHelp*", "QtUiTools*",
            "QtSql*", "QtTextToSpeech*", "QtRemoteObjects*", "QtScxml*",
            "QtStateMachine*", "QtVirtualKeyboard*", "QtHttpServer*"
        )
        foreach ($m in $modules) {
            Get-ChildItem -Path $qtRoot -Filter "$m" -File -ErrorAction SilentlyContinue |
                Remove-Item -Force
            Get-ChildItem -Path $qtRoot -Filter "Qt6$($m -replace '^Qt','')" -File -ErrorAction SilentlyContinue |
                Remove-Item -Force
        }

        # Dossiers entiers sans objet dans un portable (outils de dev, QML,
        # en-têtes C++, ressources WebEngine).
        foreach ($d in @("qml", "include", "glue", "typesystems", "examples",
                         "scripts", "support", "resources", "lupdate", "Qt/qml")) {
            $p = Join-Path $qtRoot $d
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }

        # Greffons Qt hors périmètre. On CONSERVE platforms (qwindows),
        # imageformats (qsvg : toutes les icônes de l'interface sont des SVG
        # rendus à la volée), iconengines, styles, tls et networkinformation.
        $plugins = Join-Path $qtRoot "plugins"
        foreach ($d in @("assetimporters", "geometryloaders", "renderers",
                         "renderplugins", "sceneparsers", "multimedia", "position",
                         "sensors", "webview", "qmltooling", "designer",
                         "sqldrivers", "canbus", "texttospeech", "virtualkeyboard",
                         "playlistformats", "scxmldatamodel", "help")) {
            $p = Join-Path $plugins $d
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }

        # Traductions : seul le français est chargé (__main__.py, QTranslator).
        $tr = Join-Path $qtRoot "translations"
        if (Test-Path $tr) {
            Get-ChildItem -Recurse -File $tr |
                Where-Object { $_.Name -notlike "*_fr.qm" } | Remove-Item -Force
        }

        # Stubs de typage : inutiles à l'exécution, ~20 Mo.
        Get-ChildItem -Recurse -File $sitePkgs -Include "*.pyi" | Remove-Item -Force
        Get-ChildItem -Recurse -Directory $sitePkgs -Filter "__pycache__" |
            Remove-Item -Recurse -Force

        # Tous les .exe des dépendances (designer, linguist, uic, rcc…) : inutiles
        # ici, et ce sont exactement les binaires inconnus que l'on refuse de
        # livrer — leur présence rallumerait l'analyse heuristique des antivirus.
        Get-ChildItem -Recurse -File $sitePkgs -Include "*.exe" | Remove-Item -Force

        $after = [math]::Round((Get-ChildItem -Recurse -File $sitePkgs |
                                Measure-Object Length -Sum).Sum / 1MB)
        Info "Élagage Qt : $before Mo -> $after Mo"
    }
}

# ------------------------------------------------------------- lanceurs
# .bat et non .exe : un script n'est pas un binaire, rien à signer. pythonw.exe
# (signé PSF) n'ouvre pas de console.
# Le mode portable de l'exe PyInstaller repose sur sys.frozen, faux ici : on
# transmet donc explicitement le config.yaml voisin s'il existe, pour que le
# portable garde le même comportement (configuration à côté du programme).
@'
@echo off
rem Lance Sentinelle. Un config.yaml posé à côté de ce fichier est prioritaire.
if exist "%~dp0config.yaml" (
    start "" "%~dp0python\pythonw.exe" "%~dp0run.py" --config "%~dp0config.yaml" %*
) else (
    start "" "%~dp0python\pythonw.exe" "%~dp0run.py" %*
)
'@ | Set-Content -Path (Join-Path $stage "Sentinelle.bat") -Encoding ascii

# Variante de diagnostic : console visible, journalisation DEBUG, fenêtre qui
# reste ouverte après un échec. Sans elle, un démarrage raté depuis l'explorateur
# ne laisse qu'une fenêtre qui disparaît.
@'
@echo off
rem Diagnostic : console visible et journalisation detaillee.
"%~dp0python\python.exe" "%~dp0run.py" --verbose %*
echo.
echo Sentinelle s'est arrete (code %ERRORLEVEL%).
pause
'@ | Set-Content -Path (Join-Path $stage "Sentinelle (diagnostic).bat") -Encoding ascii

@"
Sentinelle $version — version portable Windows
==============================================

Aucune installation, aucun droit administrateur.

  1. Décompresser ce dossier où vous voulez (clé USB, C:\Sentinelle, réseau…).
  2. Double-cliquer sur Sentinelle.bat.

Le dossier contient sa propre copie de Python $pyFull (distribution officielle
python.org, signée par la Python Software Foundation) : rien n'est installé sur
le poste et aucun exécutable inconnu n'est déposé.

Configuration
-------------
Par défaut : %APPDATA%\Sentinelle\config.yaml
Mode portable : poser un fichier config.yaml à côté de Sentinelle.bat, il est
alors prioritaire (la configuration voyage avec le dossier).

En cas de problème
------------------
Lancer « Sentinelle (diagnostic).bat » : la console reste ouverte et affiche
l'erreur. Le journal est dans %LOCALAPPDATA%\Sentinelle\sentinelle.log.

Licence : AGPL-3.0-or-later, voir LICENSE.
Sources : https://github.com/Arcneell/sentinelle
libmpv est distribuée selon ses propres termes (GPL/LGPL) — sources amont :
https://github.com/mpv-player/mpv
"@ | Set-Content -Path (Join-Path $stage "LISEZ-MOI.txt") -Encoding utf8

# ---------------------------------------------------------- test de fumée
# Vérifie le paquet FABRIQUÉ, pas l'environnement de développement : chemin
# ._pth, chargement de libmpv depuis lib/, greffon SVG survivant à l'élagage,
# et import de la fenêtre principale (qui tire tout le reste de l'interface).
Info "Test de fumée du paquet"
$smoke = Join-Path $cache "smoke.py"
@'
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PySide6, yaml, requests                      # noqa: F401
from PySide6.QtWidgets import QApplication
app = QApplication([])

from sentinelle import __version__
from sentinelle.player import mpv_disponible, MPV_IMPORT_ERROR
assert mpv_disponible(), f"libmpv non chargee : {MPV_IMPORT_ERROR}"

from sentinelle.ui.theme import apply_theme
apply_theme(app)

from sentinelle.ui.icons import icon, app_icon
assert not icon("play").isNull(), "greffon SVG absent (elagage trop agressif)"
assert not app_icon().isNull(), "icone d application introuvable"

import sentinelle.ui.main_window                    # noqa: F401
import sentinelle.onvif, sentinelle.remote, sentinelle.snapshot   # noqa: F401

print(f"OK Sentinelle {__version__} / Python {sys.version.split()[0]} / Qt {PySide6.QtCore.qVersion()}")
'@ | Set-Content -Path $smoke -Encoding utf8

Push-Location $stage
try {
    & "./python/python.exe" $smoke
    if ($LASTEXITCODE -ne 0) { throw "Test de fumée en échec (code $LASTEXITCODE)." }
} finally {
    Pop-Location
}

# ------------------------------------------------------------------- ZIP
# CreateFromDirectory plutôt que Compress-Archive : ce dernier met plusieurs
# minutes sur ~10 000 fichiers.
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Ok "OK -> $zip ($mb Mo)"
Write-Host "Aucune signature requise : le seul exécutable livré est le Python officiel."
