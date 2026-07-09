# RTSP-TOOL

Desktop viewer for RTSP streams from Hikvision and Dahua DVRs, for Windows and Linux.
Grid and single-camera views, automatic rotation and configurable sequences, with
per-camera bandwidth profiles so large grids stay usable over slow links.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational.svg)

## Features

- Grid (up to 4×4) and single-camera views; double-click a tile to switch.
- Automatic rotation through grid pages or through cameras.
- Sequences ("loops"): ordered steps (grid or single view + cameras + duration) played
  on repeat, with a built-in editor.
- Per-camera bandwidth profiles (see below).
- Whole-DVR import: channels and their names are discovered over the Hikvision ISAPI, or
  listed manually for Dahua.
- Reconnection with exponential backoff; retries stop on authentication failure to avoid
  locking the DVR account.
- Snapshot capture, per-tile and total bitrate, multi-monitor full screen, dark theme.
- Configured entirely in the UI. Passwords are not shown again once set and are
  obfuscated on disk.

## Bandwidth profiles

Only the stream requested from the DVR determines the bitrate — there is no transcoding,
and an off-screen camera holds no connection.

| Profile | Grid | Single |
|---------|------|--------|
| Normal | substream | mainstream (HD) |
| Eco | substream | substream |
| Extreme eco | JPEG snapshot every N seconds | substream |

Rotation and sequences close the current streams before opening the next ones. RTSP runs
over TCP. Substreams are upscaled with mpv's `ewa_lanczossharp` scaler so they stay
readable when enlarged.

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

Stored at `%APPDATA%\RTSP-TOOL\config.yaml` (Windows) or `~/.config/rtsp-tool/config.yaml`
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
rtsp_tool/
├── config.py             Data model and config.yaml read/write
├── probe.py              RTSP failure classification (auth / timeout / network)
├── snapshot.py           JPEG snapshots (ISAPI/CGI) and Hikvision channel discovery
├── player.py             libmpv loading, RTSP settings, upscaling
└── ui/
    ├── main_window.py    Grid/single views, rotation, loops, full screen
    ├── tile.py           Video tile: state machine, backoff, stop on 401, bitrate
    ├── photo_tile.py     Photo-mode tile (extreme-eco profile)
    ├── config_dialogs.py Sites, cameras, whole-DVR import
    ├── sequence_editor.py Loop editor
    └── icons.py          SVG icons
packaging/                .deb build, icon generation, deployment guide
```

Each tile runs its own libmpv instance on a separate thread, so a failing stream does not
affect the others. On an authentication failure the tile stops retrying: rotation and
loops re-open streams constantly, and a wrong password retried in a loop would lock the
DVR account.

## Tech stack

Python 3.11+, [PySide6](https://doc.qt.io/qtforpython/) (Qt 6),
[python-mpv](https://github.com/jaseg/python-mpv), PyYAML, requests.

## License

MIT — see [LICENSE](LICENSE).
