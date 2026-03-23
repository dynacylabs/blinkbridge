# blinkbridge

A tool for creating RTSP streams from [Blink cameras](https://blinkforhome.com/) using [FFmpeg](https://ffmpeg.org/) and [MediaMTX](https://github.com/bluenviron/mediamtx).

Blink cameras are battery operated and don't have native RTSP support. This tool uses the [BlinkPy](https://github.com/fronzbot/blinkpy) Python library to download clips every time motion is detected and creates RTSP streams from them.

**Key Points:**
- Delay of up to ~30 seconds between motion detection and stream update
- Streams persist the last recorded frame until new motion is detected
- Compatible with [Frigate NVR](https://github.com/blakeblackshear/frigate), [Scrypted](https://github.com/koush/scrypted), and other RTSP consumers

## Limitations

- **Photo Capture** - Must disable "Photo Capture" in Blink app for each camera (photos prevent video recognition)
- **Local Storage** - Known issue with local storage systems (see [#1](https://github.com/roger-/blinkbridge/issues/1) for workaround)

## How It Works

1. **Download** - Retrieves the latest clip for each enabled camera from the Blink server
2. **Extract** - FFmpeg extracts the last frame and creates a short still video (~0.5s)
3. **Publish** - The still video is published on a loop to MediaMTX using [FFmpeg's concat demuxer](https://trac.ffmpeg.org/wiki/Concatenate#demuxer)
4. **Update** - When motion is detected, the new clip is downloaded and published
5. **Loop** - A still video from the last frame of the new clip is then published on a loop

## Usage

**Step 1:** Download `compose.yaml` and `config/config.json` from this repository

**Step 2:** Edit `config.json` in `./config/` directory:
   - Add your Blink login credentials
   - Configure camera and server settings (see Configuration section below)

**Step 3:** Initial setup (one-time only):
   ```bash
   docker compose run blinkbridge
   ```
   Enter your Blink verification code when prompted. Credentials will be saved to `config/.cred.json`. Exit with `CTRL+C`.

**Step 4:** Start the service:
   ```bash
   docker compose up
   ```
   RTSP URLs will be printed to the console.

### Configuration

Edit `config.json` with the following settings:

**General Settings:**
- `still_video_duration` - Duration in seconds for the still frame video (default: `0.5`)
- `log_level` - Logging level: `INFO`, `DEBUG`, `WARNING`, or `ERROR`
- `paths` - Directory paths for videos, concat files, and config

**Camera Settings:**
- `cameras.enabled` - List of specific camera names to enable (empty = all cameras)
- `cameras.disabled` - List of camera names to disable
- `cameras.max_failures` - Max consecutive failures before stopping a stream (default: `3`)
- `cameras.restart_delay_seconds` - Delay before restarting after failure (default: `60`)

**Blink Account:**
- `blink.login.username` - Your Blink account email
- `blink.login.password` - Your Blink account password
- `blink.history_days` - Days to look back in history (default: `90`)
- `blink.poll_interval` - Polling interval in minutes (default: `1`, minimum recommended)
- `blink.metadata_pages` - Number of API pages to fetch for video metadata, ~25 clips per page (default: `10`)

**RTSP Server:**
- `rtsp_server.address` - MediaMTX server address (default: `mediamtx`)
- `rtsp_server.port` - RTSP port (default: `8554`)

**FFmpeg Hardware Acceleration** (experimental -- untested, community testing welcome):
- `ffmpeg.encoder` - Encoder selection (default: `auto`). Options:
  - `auto` - Automatically detect and use the best available hardware encoder
  - `h264_nvenc` - NVIDIA GPU (requires nvidia-container-toolkit)
  - `h264_qsv` - Intel Quick Sync Video
  - `h264_vaapi` - VA-API (Intel/AMD on Linux)
  - `h264_videotoolbox` - Apple VideoToolbox (macOS, not available inside Docker)
  - `h264_v4l2m2m` - V4L2 Memory-to-Memory (Raspberry Pi, ARM SoCs)
  - `libx264` - Software encoding (always works, default fallback)
- `ffmpeg.vaapi_device` - VA-API render device path (default: `/dev/dri/renderD128`)

> **Note:** On Mac Docker, no GPU acceleration is available (Docker runs a Linux VM without GPU passthrough). The `auto` setting will detect this and fall back to software encoding automatically.

To enable hardware acceleration in Docker, uncomment the appropriate lines in `compose.yaml`:
- **Intel/AMD (VAAPI or QSV):** Uncomment `devices: ["/dev/dri:/dev/dri"]` and install the matching driver package in the Dockerfile
- **NVIDIA:** Uncomment the `runtime: nvidia` section (requires [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on the host)

> **Warning: Hardware acceleration is experimental and untested.** The `auto` detection runs a real test encode at startup to verify the encoder actually works on your hardware. If the test fails, it falls back to software encoding (`libx264`) automatically. If you encounter issues with a specific GPU or driver, set `"encoder": "libx264"` as a workaround and [open an issue](https://github.com/dynacylabs/blinkbridge/issues) with your hardware details.

### RTSP Stream URLs

Streams are available at: `rtsp://<host>:8554/<camera_name>`

**Examples:**
```
rtsp://localhost:8554/Front_Door        # Local access
rtsp://192.168.1.100:8554/Front_Door   # Network access
```

**Note:** Camera names are sanitized (spaces and special characters modified). Check console output for exact URLs.

## TODO

- [ ] Process cameras in parallel and reduce latency
- [ ] Add ONVIF server with motion events

## Related Projects

- [arlo-streamer](https://github.com/kaffetorsk/arlo-streamer)

