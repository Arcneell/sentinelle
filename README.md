# RTSP-TOOL

Desktop viewer for RTSP streams from IP DVRs and cameras, for Windows and Linux.
Grid and single-camera views, automatic rotation and configurable sequences, with
per-camera bandwidth profiles so large grids stay usable over slow links.

Works with Hikvision and Dahua natively, several other brands via URL templates, and
any ONVIF device through auto-discovery.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational.svg)

## Features

- Grid (up to 4×4) and single-camera views; double-click a tile to switch.
- Automatic rotation through grid pages or through cameras.
- Sequences ("loops"): ordered steps (grid or single view + cameras + duration) played
  on repeat, with a built-in editor.
- Per-camera bandwidth profiles (see below).
- **Neural image enhancement** (see below): real-time GPU super-resolution to make
  low-quality substreams legible.
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

## Substream optimisation (the real fix)

The biggest quality lever is the DVR encoder, not client-side processing. Many Dahua
DVRs ship their substream as **MJPEG** at a low bitrate — every frame is a standalone,
heavily-compressed JPEG, which is what produces the blocky look. In Configuration,
select a Dahua camera or site → **"Optimiser le flux (DVR)"** switches the substream to
**H.264** at the same bitrate. This transforms the image at the source, with no
post-processing and no artificial look, and affects neither the main stream nor
recording. It is the recommended first step; the enhancement levels below are for when
you cannot change the DVR.

## Image enhancement

The dominant defect of low-bitrate CCTV substreams is **compression blocking** (DCT
macroblocks), not low resolution — most substreams are D1 (704×576), which already has
plenty of pixels. Enhancement is therefore built around **deblocking**, applied in real
time per camera or globally, with three levels:

| Level | What it does | Cost |
|-------|--------------|------|
| Off | Direct rendering (ewa_lanczossharp upscaler) | none |
| Light | Fast deblocking (`pp7`) + contrast-adaptive sharpening | low |
| Max | Strong deblocking (`spp`) + adaptive sharpening, **+ neural upscale only when the source is genuinely low-res (≤380 px)** | higher |

Deblocking (ffmpeg `pp7`/`spp`) removes the compression macroblocks that make the image
look "pixelated"; a contrast-adaptive sharpen shader (bundled) then crisps edges without
halos. For genuinely low-resolution sources (CIF/QVGA) the Max level also runs a neural
upscaler — **Anime4K** (bundled, MIT), or **FSRCNNX** (downloaded on demand, better for
photographic content) in single view. Grid tiles use lighter variants so many cameras
can run at once.

This was tuned by measuring the rendered pixels A/B on a real CCTV substream: on D1
footage, deblocking is what visibly cleans the image, while a neural upscaler alone
changes little and can even amplify blocks.

Honest limit: enhancement reconstructs a *plausible* cleaner image — it cannot recover
information that was never captured (a plate 4 pixels wide stays unreadable).

### Real-time AI reconstruction (grid and single view)

The "Temps réel IA" enhancement level runs a **video super-resolution neural network on
every frame, live** — in the grid as well as in single view. Each stream is decoded by
OpenCV, frames are downscaled (which also averages away compression blocks) and rebuilt
×2 by the network (Real-ESRGAN `animevideov3`) on the GPU via Vulkan (ncnn).

Downscaling before the network is the trick: it removes block noise *and* keeps the frame
rate real-time (network cost scales with input pixels). Tiles share one GPU inference
queue, and each stream keeps only its **latest** decoded frame, so latency stays bounded
instead of accumulating. Measured on an Intel integrated GPU: ~30 fps of total throughput
at grid input size — one tile ≈ 30 fps, two tiles ≈ 18 fps each, nine ≈ 3–4 fps each
(surveillance-grade). Single view uses a larger input (≈15 fps for more detail).

### AI frame reconstruction (single frame)

For one frame, a heavier model goes further: right-click a tile → **"Reconstruire l'image
(IA)"** runs **Real-ESRGAN** ×4 (generative restoration) and shows an original/reconstructed
side-by-side in ~5–20 s, saved to Pictures/RTSP-TOOL.

The engine (`realesrgan-ncnn-vulkan`, BSD-3-Clause, any Vulkan GPU) is downloaded on demand
(~45 MB) into the user profile — not redistributed here. Real-time mode reuses its bundled
video model. Both need `ncnn` + OpenCV (optional dependencies).

**Warning shown in-app**: reconstructed detail is *invented* by the network. It helps
scene legibility, but a reconstructed plate or face has no identification value.

FSRCNNX is GPL-licensed and therefore **not** bundled; it is downloaded on demand into
the user profile and never redistributed by this repository.

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
├── config.py             Data model, brand URL templates, config.yaml read/write
├── probe.py              RTSP failure classification (auth / timeout / network)
├── snapshot.py           JPEG snapshots (ISAPI/CGI) and Hikvision channel discovery
├── onvif.py              ONVIF: WS-Discovery, stream/snapshot URIs, PTZ (no heavy deps)
├── enhance.py            Image enhancement engine (deband/sharpen + neural GLSL SR)
├── player.py             libmpv loading, RTSP settings, upscaling
├── shaders/              Bundled Anime4K neural shaders (MIT) + attribution
└── ui/
    ├── main_window.py    Grid/single views, rotation, loops, enhancement, full screen
    ├── tile.py           Video tile: state machine, backoff, stop on 401, zoom, PTZ
    ├── photo_tile.py     Photo-mode tile (extreme-eco profile)
    ├── config_dialogs.py Sites, cameras, whole-DVR import, ONVIF network scan
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
