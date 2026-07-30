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

### Runtime Note

The current supported runtime is the Python service started by Docker Compose.

The shell scripts in the `scripts/` folder are legacy helpers from earlier development and are not part of the default runtime path used by the container entrypoint.

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
- `blink.metadata_pages` - Number of API pages to fetch for video metadata, ~25 clips per page, shared across all cameras (default: `10`). Raise this if you have many cameras and quieter ones are missing their most recent clip on startup/restart.

**RTSP Server:**
- `rtsp_server.address` - MediaMTX server address (default: `mediamtx`)
- `rtsp_server.port` - RTSP port (default: `8554`)

**Frigate Camera Snippet Export (optional):**
- `frigate_export.enabled` - Enable exporting Frigate `cameras:` YAML block
- `frigate_export.output_path` - File path where snippet is written
- `frigate_export.rtsp_host` / `frigate_export.rtsp_port` - Host/port used in exported RTSP URLs
- `frigate_export.roles` - Roles included for each camera input (e.g. `detect`, `record`)
- `frigate_export.detect_defaults` - Default detect width/height/fps for exported cameras

When enabled, BlinkBridge writes a standalone `cameras:` block file after startup completes and cameras are discovered. The file is intended for manual copy/merge into a user-managed Frigate config.

**Web Access (optional):**
- `web.enabled` - Enable BlinkBridge utility web page
- `web.host` / `web.port` - Bind address and port for the web UI
- UI endpoint: `http://<host>:8765/`
- Raw YAML endpoint: `http://<host>:8765/frigate-cameras.yml`

In Codespaces, forward port `8765` to access the page from your browser.

### RTSP Stream URLs

Streams are available at: `rtsp://<host>:8554/<camera_name>`

**Examples:**
```
rtsp://localhost:8554/Front_Door        # Local access
rtsp://192.168.1.100:8554/Front_Door   # Network access
```

**Note:** Camera names are sanitized (spaces and special characters modified). Check console output for exact URLs.

## TODO

- [ ] Support FFmpeg hardware acceleration (e.g. QSV)
- [ ] Process cameras in parallel and reduce latency
- [ ] Add ONVIF server with motion events

## Related Projects

- [arlo-streamer](https://github.com/kaffetorsk/arlo-streamer)

