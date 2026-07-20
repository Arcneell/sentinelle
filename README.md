# Sentinelle

Desktop video-surveillance viewer for RTSP / ONVIF cameras and DVRs, for Windows and
Linux. Grid and single-camera views, ONVIF motion detection, automatic rotation and
configurable sequences, with per-camera bandwidth profiles so large grids stay usable
over slow links.

Works with Hikvision and Dahua natively, several other brands via URL templates, and
any ONVIF device through auto-discovery.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational.svg)

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
└── ui/
    ├── theme.py          Dark theme palette + global flat stylesheet
    ├── main_window.py    Title bar, camera sidebar, grid/single views, rotation, loops, motion
    ├── tile.py           Video tile: state machine, backoff, stop on 401, zoom, PTZ
    ├── photo_tile.py     Photo-mode tile (extreme-eco profile)
    ├── config_dialogs.py Sites, cameras, whole-DVR import, ONVIF scan
    ├── sequence_editor.py Loop editor
    └── icons.py          SVG icons
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

Python 3.11+, [PySide6](https://doc.qt.io/qtforpython/) (Qt 6),
[python-mpv](https://github.com/jaseg/python-mpv), PyYAML, requests.

## License

MIT — see [LICENSE](LICENSE).
