# Construit l'exécutable Windows de Sentinelle (PyInstaller) et le SIGNE si un
# certificat est fourni.
#
# Les postes protégés par Symantec Endpoint Protection (et autres) bloquent les
# binaires non signés (« Accès refusé ») : signer l'exe évite ce blocage et les
# faux positifs. Fournissez un certificat de signature de code (.pfx) :
#
#   $env:SENTINELLE_PFX    = "C:\chemin\vers\certificat.pfx"
#   $env:SENTINELLE_PFX_PW = "motDePasseDuPfx"
#   pwsh packaging/build_windows.ps1
#
# Sans ces variables, l'exe est construit mais NON signé (un avertissement le
# rappelle). Prérequis : Python + pip, et signtool.exe (SDK Windows) dans le
# PATH pour la signature.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # racine du dépôt

# --- version (depuis sentinelle/__init__.py) ---
$verLine = Select-String -Path "sentinelle/__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"'
$version = $verLine.Matches[0].Groups[1].Value
Write-Host "Construction de Sentinelle $version" -ForegroundColor Cyan

# --- libmpv requise à côté du build ---
if (-not (Test-Path "lib/libmpv-2.dll")) {
    throw "lib/libmpv-2.dll introuvable : placez la DLL libmpv dans lib/ avant de construire."
}

# --- dépendances de build ---
python -m pip install --quiet --upgrade pip
# PyInstaller épinglé sur la même majeure que le build .deb (build_deb.sh) :
# une majeure différente entre .exe et .deb livrés donnerait des comportements
# de bootloader divergents entre les deux plateformes.
python -m pip install --quiet -r requirements.txt "pyinstaller==6.*"

# --- build ---
pyinstaller --noconfirm --windowed --name Sentinelle --icon packaging/sentinelle.ico `
    --add-binary "lib/libmpv-2.dll;." `
    --add-data "sentinelle/ui/sentinelle.ico;sentinelle/ui" `
    --add-data "sentinelle/ui/sentinelle.png;sentinelle/ui" run.py

$exe = "dist/Sentinelle/Sentinelle.exe"
if (-not (Test-Path $exe)) { throw "Build échoué : $exe absent." }

# --- signature (optionnelle) ---
if ($env:SENTINELLE_PFX) {
    if (-not (Test-Path $env:SENTINELLE_PFX)) {
        throw "Certificat introuvable : $env:SENTINELLE_PFX"
    }
    Write-Host "Signature de l'exécutable…" -ForegroundColor Cyan
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue)?.Source
    if (-not $signtool) {
        throw "signtool.exe introuvable dans le PATH (installez le SDK Windows)."
    }
    & $signtool sign /f $env:SENTINELLE_PFX /p $env:SENTINELLE_PFX_PW `
        /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) { throw "Échec de la signature (code $LASTEXITCODE)." }
    & $signtool verify /pa $exe
    Write-Host "OK -> $exe (signé)" -ForegroundColor Green
} else {
    Write-Warning "SENTINELLE_PFX non défini : exécutable NON signé."
    Write-Warning "Il sera probablement bloqué par les antivirus/EDR (ex. Symantec Endpoint Protection)."
    Write-Host "OK -> $exe (non signé)" -ForegroundColor Yellow
}
