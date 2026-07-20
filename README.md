<p align="center">
  <img src="packaging/sentinelle.png" alt="Sentinelle" width="128"/>
</p>

<h1 align="center">Sentinelle</h1>

Video-surveillance viewer for RTSP / ONVIF cameras and DVRs, for Windows and
Linux. Grid and single-camera views, ONVIF motion detection, automatic rotation and
configurable sequences, with per-camera bandwidth profiles so large grids stay usable
over slow links.

Works with Hikvision and Dahua natively, several other brands via URL templates, and
any ONVIF device through auto-discovery.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational.svg)

## Two deployment modes

**Standalone** — the desktop app connects directly to the DVRs. No server, nothing else
to install; each workstation keeps its own configuration. This is the default mode.

**With a central server** — a small server (Docker) holds the configuration, the user
accounts and relays the streams; each workstation logs in with a username and password:

- each camera is pulled **once** from its site regardless of how many viewers are
  watching (critical for sites behind 4G), and only while someone is watching;
- DVR credentials **never leave the server** — workstations only get a session token;
- **user accounts with per-user access**: each account sees only the sites/cameras it
  was granted; the restriction is enforced server-side *and* at the relay, so it holds
  even against a tampered client;
- **admin accounts** get an Administration panel in the app (users, cameras/sites,
  loops, settings); regular users only get their own preferences;
- configuration is centralised: add a camera once, every allowed client sees it;
- ONVIF motion is monitored server-side (one subscription per camera) and pushed to
  clients over SSE, filtered to their allowed cameras.

The mode is chosen per workstation in *Configuration → Connexion* and can be changed at
any time. See [Server](#server-optional) below.

## Features

- Grid (up to 4×4) and single-camera views; double-click a tile to switch.
- **ONVIF motion detection**: tiles with motion are outlined in red, and a
  **"motion view"** automatically fills the grid with the cameras that are moving.
- Automatic rotation through grid pages or through cameras.
- Sequences ("loops"): ordered steps (grid or single view + cameras + duration) played
  on repeat, with a built-in editor.
- Per-camera bandwidth profiles (see below).
- **Wide device support**: Hikvision, Dahua, Amcrest, Reolink, Uniview, Axis, Vivotek,
  Foscam, TP-Link/Tapo via built-in URL templates, plus **ONVIF** for anything else.
- **ONVIF network discovery**: scan the LAN, pick the cameras, and their stream URLs
  (main + sub), snapshot URL and PTZ capability are resolved automatically.
- **PTZ control** for motorised ONVIF cameras (pan/tilt/zoom pad in single view).
- **Digital zoom** and per-tile aspect mode (fit / crop / stretch); a **"test
  connection"** button when adding a camera.
- Whole-DVR import: channels and their names are discovered over the Hikvision ISAPI, or
  listed manually for other brands.
- Reconnection with exponential backoff; retries stop on authentication failure to avoid
  locking the DVR account.
- Snapshot capture, per-tile and total bitrate, multi-monitor full screen, and a
  dark interface tuned for a video wall.
- Configured entirely in the UI. Passwords are not shown again once set and are
  obfuscated on disk.

## Motion detection (ONVIF)

Toggle **Mouvement** to subscribe to each camera's ONVIF event stream (PullPoint). When
a camera reports motion, its tile is outlined in red. Toggle **Vue mouvement** and the
grid stops showing your manual selection and instead shows, live, only the cameras that
are currently moving — a hands-off wall that surfaces activity across every site.

Requires ONVIF (and its motion rule) to be enabled on the device; a camera without an
event service is simply skipped. Motion clears on the camera's "off" event or after a
few seconds without a new one.

## Bandwidth profiles

Only the stream requested from the DVR determines the bitrate — there is no transcoding,
and an off-screen camera holds no connection.

| Profile | Grid | Single |
|---------|------|--------|
| Normal | substream | mainstream (HD) |
| Eco | substream | substream |
| Extreme eco | JPEG snapshot every N seconds | substream |

Rotation and sequences close the current streams before opening the next ones. RTSP runs
over TCP. Substreams are rendered with mpv's `ewa_lanczossharp` scaler so they stay
readable when enlarged — no extra processing, no external dependencies.

## Install

Requires Python 3.11+ and libmpv.

- Windows: put `libmpv-2.dll` in a `lib/` folder at the project root.
- Debian/Ubuntu: `sudo apt install libmpv2`. Fedora: `sudo dnf install mpv-libs`.
- Optional: `ffprobe` (from `ffmpeg`) improves failure diagnostics.

```bash
pip install -r requirements.txt
python run.py
```

The Configuration window opens on first run.

## Configuration

Managed in the UI: add a site (fiber or 4G), add a DVR (address and credentials, then
channel discovery or a manual list), then tick the cameras to display.

Stored at `%APPDATA%\Sentinelle\config.yaml` (Windows) or `~/.config/sentinelle/config.yaml`
(Linux). A `config.yaml` next to the executable takes priority.

Passwords are obfuscated in the file, not encrypted — the key ships with the app, so this
only prevents casual reading. Use a read-only DVR account.

## Server (optional)

The server is two containers: a FastAPI control plane and a
[MediaMTX](https://github.com/bluenviron/mediamtx) stream relay. Streams are proxied
**on demand** with no re-encoding (H.264 passthrough), so CPU usage stays negligible.

```bash
cd deploy
docker compose up -d --build
```

- On first start an **admin** account is created; its initial password is printed in
  the API logs (`docker compose logs api`) and written to `deploy/data/admin-initial.txt`.
  Log in with it, then change it (*Configuration → Mon compte*) and delete that file.
- To bootstrap from an existing standalone installation, copy its `config.yaml` into
  `deploy/data/` before the first start — same file format.
- Manage everything from the app while logged in as admin → **Administration**: create
  user accounts, grant each one whole sites or individual cameras, edit cameras/sites,
  loops and settings.
- Exposed ports: `8080/tcp` (API: login, config, snapshots, PTZ, motion events over
  SSE, relay authorization) and `8554/tcp` (RTSP relay). The MediaMTX control port stays
  inside the Docker network.
- On each workstation: *Configuration → Connexion* → mode **Serveur central** and the
  server URL (`http://server:8080`), then log in. "Rester connecté" stores the
  credentials for unattended restart (use a dedicated viewer account on wall displays).

Security model: passwords are hashed with PBKDF2 (never stored or sent in clear);
sessions are stateless signed tokens that a password change immediately invalidates;
per-user camera access is enforced both in the API and at the relay (MediaMTX external
HTTP authorization calls back into the API for every read); DVR credentials live only on
the server. The API speaks plain HTTP — deploy it on a trusted network (VPN) or behind a
TLS reverse proxy (Caddy/nginx). `deploy/data/` holds all secrets and is gitignored.

> Note: after editing `deploy/mediamtx.yml`, recreate the relay so it reloads its
> config: `docker compose up -d --force-recreate mediamtx`.

## Packaging

See [packaging/DEPLOIEMENT.md](packaging/DEPLOIEMENT.md) for building and signing the
Windows exe and building the `.deb` (with icon and menu entry).

```bash
docker run --rm -v "${PWD}:/src" -w /src debian:12 bash packaging/build_deb.sh
```

## Architecture

```
sentinelle/
├── config.py             Data model, brand URL templates, config.yaml read/write
├── probe.py              RTSP failure classification (auth / timeout / network)
├── snapshot.py           JPEG snapshots (ISAPI/CGI) and Hikvision channel discovery
├── onvif.py              ONVIF: WS-Discovery, stream/snapshot URIs, PTZ, motion events
├── motion.py             ONVIF motion monitor (per-camera event subscription threads)
├── player.py             libmpv loading, RTSP settings, upscaling
├── remote.py             Server mode: API client, session login, SSE motion listener
└── ui/
    ├── theme.py          Dark theme palette + global flat stylesheet
    ├── main_window.py    Title bar, camera sidebar, grid/single views, rotation, loops, motion
    ├── tile.py           Video tile: state machine, backoff, stop on 401, zoom, PTZ
    ├── photo_tile.py     Photo-mode tile (extreme-eco profile)
    ├── config_dialogs.py Camera manager, standalone config, server preferences
    ├── login_dialog.py   Server login
    ├── admin_dialog.py   Admin panel: users + permissions, cameras, loops, settings
    ├── sequence_editor.py Loop editor
    └── icons.py          SVG icons
sentinelle_server/
├── app.py                FastAPI API: login, config, snapshots, PTZ, SSE, relay-auth
├── auth.py               User accounts, PBKDF2 hashing, signed tokens, permissions
├── store.py              Central config (same YAML format) + secret/bootstrap admin
├── relay.py              MediaMTX orchestration (one on-demand path per stream)
└── motion.py             Server-side ONVIF motion monitor + event hub
deploy/                   docker-compose.yml, Dockerfile.server, mediamtx.yml
packaging/                .deb build, icon generation, deployment guide
```

ONVIF is implemented directly over SOAP/HTTP (WS-UsernameToken digest auth) — no
`zeep`/`onvif-zeep` dependency. Network discovery uses WS-Discovery multicast, which
does not cross VLAN/VPN boundaries; cameras on routed subnets are added by direct IP
instead (the ONVIF client resolves their stream URLs the same way).

Each tile runs its own libmpv instance on a separate thread, so a failing stream does not
affect the others. On an authentication failure the tile stops retrying: rotation and
loops re-open streams constantly, and a wrong password retried in a loop would lock the
DVR account.

## Tech stack

Client: Python 3.11+, [PySide6](https://doc.qt.io/qtforpython/) (Qt 6),
[python-mpv](https://github.com/jaseg/python-mpv), PyYAML, requests.
Server: FastAPI + uvicorn, [MediaMTX](https://github.com/bluenviron/mediamtx) (Docker).

## License

MIT — see [LICENSE](LICENSE).
