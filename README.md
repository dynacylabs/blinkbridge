# BlinkBridge

**Convert Blink camera motion clips to continuous RTSP streams**

BlinkBridge bridges the gap between Blink's battery-powered security cameras and standard video surveillance systems by creating persistent RTSP streams from motion-triggered video clips.

[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://hub.docker.com/r/rogerdammit/blinkbridge)

---

## 🚨 Important Notes

- **Storage Issue**: There is an issue related to local storage systems. See [issue #1](https://github.com/roger-/blinkbridge/issues/1) for a temporary fix.
- **Photo Capture**: Disable the "Photo Capture" feature in the Blink app for each camera to work around an issue where photos prevent videos from being recognized by BlinkPy.

---

## Overview

[Blink cameras](https://blinkforhome.com/) are battery-operated security cameras without native RTSP support. BlinkBridge solves this limitation by:

- Monitoring your Blink cameras for motion events using the [BlinkPy](https://github.com/fronzot/blinkpy) library
- Downloading video clips when motion is detected
- Creating continuous RTSP streams using [FFmpeg](https://ffmpeg.org/) and [MediaMTX](https://github.com/bluenviron/mediamtx)
- Maintaining a static frame from the last clip when no motion is detected

### Use Cases

Once RTSP streams are available, integrate with:
- **[Frigate NVR](https://github.com/blakeblackshear/frigate)** - Advanced person/object detection and recording
- **[Scrypted](https://github.com/koush/scrypted)** - HomeKit Secure Video support
- **Any RTSP-compatible NVR or monitoring system**

### Limitations

⚠️ **Latency**: Due to Blink's polling mechanism, expect a delay of **up to ~30 seconds** between motion detection and stream updates. The polling interval can be adjusted in the configuration, but aggressive polling may result in rate limiting by Blink's servers.

---

## How It Works

```
┌─────────────────┐
│  Blink Camera   │
│ (Motion Event)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   BlinkBridge   │
│  • Poll for     │
│    motion       │
│  • Download     │
│    clip         │
│  • Extract      │
│    last frame   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│     FFmpeg      │
│  • Create still │
│    video loop   │
│  • Concat clips │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    MediaMTX     │
│  (RTSP Server)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Your NVR/     │
│   Application   │
└─────────────────┘
```

### Detailed Process

1. **Initial Setup**: BlinkBridge downloads the latest clip for each enabled camera
2. **Still Video Creation**: FFmpeg extracts the last frame and creates a short (~0.5s) still video
3. **Stream Publishing**: The still video loops continuously on MediaMTX using FFmpeg's concat demuxer
4. **Motion Detection**: When motion is detected, the new clip is downloaded and published to the stream
5. **Frame Persistence**: After the clip finishes, a new still video from the last frame loops until the next motion event

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Blink account credentials
- Network access to Blink's servers

### Installation

1. **Download the Docker Compose file**
   ```bash
   curl -O https://raw.githubusercontent.com/roger-/blinkbridge/main/compose.yaml
   ```

2. **Create a config directory**
   ```bash
   mkdir -p config
   ```

3. **Download and configure `config.json`**
   ```bash
   curl -o config/config.json https://raw.githubusercontent.com/roger-/blinkbridge/main/config/config.json
   ```

4. **Edit `config/config.json`** with your Blink credentials:
   ```json
   {
     "blink": {
       "login": {
         "username": "your-email@example.com",
         "password": "your-password"
       }
     }
   }
   ```

5. **Perform initial authentication** (one-time setup):
   ```bash
   docker compose run blinkbridge
   ```
   Enter the verification code sent to your email/phone. Credentials are saved to `config/.cred.json`. Exit with `Ctrl+C`.

6. **Start the service**:
   ```bash
   docker compose up -d
   ```

7. **View RTSP URLs** in the logs:
   ```bash
   docker compose logs -f blinkbridge
   ```

Your streams will be available at: `rtsp://localhost:8554/<camera_name>`

---

## Configuration

### Configuration File (`config/config.json`)

```json
{
  "still_video_duration": 0.5,
  "paths": {
    "videos": "/working",
    "concat": "/working",
    "config": "/config"
  },
  "cameras": {
    "enabled": [],
    "disabled": [],
    "max_failures": 3,
    "restart_delay_seconds": 60
  },
  "blink": {
    "login": {
      "username": "your-email@example.com",
      "password": "your-password"
    },
    "history_days": 90,
    "poll_interval": 5
  },
  "rtsp_server": {
    "address": "localhost",
    "port": 8554
  },
  "log_level": "INFO"
}
```

### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `still_video_duration` | Duration of the still frame video in seconds | `0.5` |
| `cameras.enabled` | List of camera names to enable (empty = all) | `[]` |
| `cameras.disabled` | List of camera names to disable | `[]` |
| `cameras.max_failures` | Max stream failures before disabling camera | `3` |
| `cameras.restart_delay_seconds` | Seconds to wait before restarting failed stream | `60` |
| `blink.poll_interval` | Seconds between motion checks (⚠️ don't set too low) | `5` |
| `blink.history_days` | Days of history to query from Blink | `90` |
| `rtsp_server.address` | MediaMTX server address | `localhost` |
| `rtsp_server.port` | MediaMTX RTSP port | `8554` |
| `log_level` | Logging level (DEBUG, INFO, WARNING, ERROR) | `DEBUG` |

### Docker Compose Configuration

```yaml
services:
  blinkbridge:
    image: rogerdammit/blinkbridge
    volumes:
      - ./config:/config
      # Path for temporary files (video clips, concat files)
      - /tmp/blinkbridge:/working
      # Alternative: Use RAM for better performance
      # - type: tmpfs
      #   target: /working
      #   tmpfs:
      #     size: 52428800  # 50MB
    tty: true  # Enable color logs
    environment:
      - BLINKBRIDGE_CONFIG=/config/config.json
  
  mediamtx:
    image: bluenviron/mediamtx
    container_name: mediamtx
    ports:
      - 8554:8554  # RTSP port
```

### Using tmpfs for Working Directory

For better performance and reduced disk wear, use tmpfs (RAM) for temporary files:

```yaml
volumes:
  - type: tmpfs
    target: /working
    tmpfs:
      size: 52428800  # 50MB (adjust based on your needs)
```

---

## Architecture

### Components

```
blinkbridge/
├── main.py            # Application orchestrator
├── blink.py           # Blink API client wrapper
├── stream_server.py   # RTSP stream management
├── ffmpeg.py          # Video processing utilities
├── config.py          # Configuration management
└── utils.py           # Helper functions
```

#### `main.py` - Application
- Coordinates all components
- Monitors cameras for motion events
- Manages stream lifecycle
- Handles failures and restarts

#### `blink.py` - CameraManager
- Authenticates with Blink servers
- Downloads video clips
- Polls for motion detection
- Manages metadata and clip history

#### `stream_server.py` - StreamServer
- Creates RTSP streams using FFmpeg
- Manages video concatenation
- Handles still frame looping
- Publishes to MediaMTX

#### `ffmpeg.py` - Video Processing
- Extracts last frame from clips
- Creates still videos with matching parameters
- Probes video stream parameters

---

## Troubleshooting

### Stream Not Updating

1. Check BlinkBridge logs: `docker compose logs -f blinkbridge`
2. Verify motion detection is working in the Blink app
3. Check the poll interval isn't too high
4. Ensure "Photo Capture" is disabled in Blink app

### Authentication Issues

1. Delete `config/.cred.json`
2. Run `docker compose run blinkbridge` to re-authenticate
3. Check your credentials in `config/config.json`

### High Latency

- Motion clips take time to upload to Blink's servers
- Polling interval adds additional delay
- Network latency between you and Blink's servers
- Consider this is a limitation of Blink's architecture

### Stream Failures

- Check `max_failures` and `restart_delay_seconds` settings
- Review FFmpeg logs for errors
- Ensure MediaMTX is running and accessible
- Verify sufficient disk space/RAM for working directory

### Container Issues

```bash
# View logs
docker compose logs -f

# Restart services
docker compose restart

# Rebuild image
docker compose build --no-cache

# Check resource usage
docker stats
```

---

## Development

### Building Locally

```bash
docker compose build
```

### Running Without Docker

1. **Install dependencies**:
   ```bash
   pip install rich blinkpy==0.23.0 aiohttp
   ```

2. **Install FFmpeg**:
   ```bash
   # Ubuntu/Debian
   sudo apt install ffmpeg
   
   # macOS
   brew install ffmpeg
   ```

3. **Configure paths** in `config.json` to local directories

4. **Run**:
   ```bash
   export BLINKBRIDGE_CONFIG=config/config.json
   python -m blinkbridge.main
   ```

---

## Roadmap

- [ ] Better error handling and recovery
- [ ] Code cleanup and refactoring
- [ ] Hardware acceleration support (QSV, NVENC, VA-API)
- [ ] Parallel camera processing for reduced latency
- [ ] ONVIF server with motion events
- [ ] Web UI for configuration and monitoring
- [ ] Support for other video sources
- [ ] Configurable video quality settings

---

## Related Projects

- [arlo-streamer](https://github.com/kaffetorsk/arlo-streamer) - Similar solution for Arlo cameras
- [BlinkPy](https://github.com/fronzot/blinkpy) - Python library for Blink cameras
- [MediaMTX](https://github.com/bluenviron/mediamtx) - RTSP server
- [Frigate](https://github.com/blakeblackshear/frigate) - NVR with AI object detection
- [Scrypted](https://github.com/koush/scrypted) - Home video integration platform

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

---

## License

This project is provided as-is. Check the repository for license information.

---

## Disclaimer

This project is not affiliated with or endorsed by Blink or Amazon. Use at your own risk. Excessive polling may result in rate limiting or account restrictions.

