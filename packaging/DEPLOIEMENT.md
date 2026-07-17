# Packaging & déploiement Sentinelle

## Windows — build + signature

### 1. Construire l'exe
```powershell
pip install pyinstaller
python packaging/make_icon.py     # génère l'icône (une fois)
pyinstaller --noconfirm --windowed --name Sentinelle --icon packaging/sentinelle.ico `
    --add-binary "lib\libmpv-2.dll;." `
    --add-data "rtsp_tool\ui\sentinelle.ico;rtsp_tool/ui" `
    --add-data "rtsp_tool\ui\sentinelle.png;rtsp_tool/ui" run.py
# -> dist\Sentinelle\  (déployer le dossier entier, pas seulement l'exe)
```

### 2. Signer (optionnel mais recommandé en parc géré)
Beaucoup d'antivirus/EDR bloquent les exécutables non signés. Avec un certificat
de signature de code interne (créé une fois) :
```powershell
# créer un certificat auto-signé (une fois)
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
    -Subject "CN=<Votre organisation> - Sentinelle" `
    -CertStoreLocation Cert:\CurrentUser\My `
    -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
    -NotAfter (Get-Date).AddYears(5)
Export-Certificate -Cert $cert -FilePath sentinelle.cer   # partie publique à déployer

# signer (à chaque build)
Set-AuthenticodeSignature -FilePath dist\Sentinelle\Sentinelle.exe `
    -Certificate $cert -HashAlgorithm SHA256 `
    -TimestampServer http://timestamp.digicert.com
```
Si le serveur d'horodatage est injoignable (proxy), omettre `-TimestampServer` :
la signature reste valable jusqu'à l'expiration du certificat.

### 3. Approuver le certificat sur les postes (parc géré)
Via GPO — *Configuration ordinateur → Stratégies → Paramètres Windows →
Paramètres de sécurité → Stratégies de clé publique* : importer le `sentinelle.cer` dans
**Autorités de certification racines de confiance** et **Éditeurs approuvés**.
Sans annuaire : `certutil -addstore Root sentinelle.cer` et
`certutil -addstore TrustedPublisher sentinelle.cer` (admin, sur chaque poste).

Pour un antivirus/EDR, ajouter une exception **par signataire** plutôt que par
hash, afin que chaque nouvelle version signée passe sans re-whitelister.

> Le statut de signature affiche « UnknownError » tant que le certificat
> auto-signé n'est pas dans les racines approuvées du poste — c'est attendu ;
> il devient « Valid » une fois le `.cer` importé.

Pour une distribution hors parc (SmartScreen, postes non gérés), utiliser un
certificat de signature de code commercial (OV/EV).

## Linux — .deb

### Construire
Depuis la racine du projet, avec Docker (fonctionne aussi depuis Windows) :
```bash
docker run --rm -v "${PWD}:/src" -w /src debian:12 bash packaging/build_deb.sh
```
ou sur une machine Debian/Ubuntu : `bash packaging/build_deb.sh`.
Résultat : `dist/sentinelle_<version>_amd64.deb` (avec icône + entrée de menu).

### Installer
```bash
sudo apt install ./sentinelle_1.0.0_amd64.deb   # tire libmpv2 automatiquement
```
Puis lancer **Sentinelle** depuis le menu des applications (icône dédiée), ou la
commande `sentinelle`. Compatible Debian 12+/Ubuntu 24.04+.
Sous Wayland, si les tuiles restent noires : `QT_QPA_PLATFORM=xcb sentinelle`.

La configuration est propre à chaque poste
(`~/.config/sentinelle/config.yaml`), gérée par l'interface ; pour livrer une
config commune, la copier ou utiliser `sentinelle --config /chemin/config.yaml`.
