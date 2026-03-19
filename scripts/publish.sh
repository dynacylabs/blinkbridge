#!/bin/bash
#
# publish.sh - Start an RTSP stream using FFmpeg concat demuxer
#
# This script publishes a video stream to an RTSP server using FFmpeg's concat
# demuxer. It creates a two-level concat structure allowing seamless video
# updates while maintaining continuous streaming. The stream loops indefinitely.
#
# Usage:
#   ./publish.sh stream_name first_clip.mp4
#
# Arguments:
#   stream_name  - Unique identifier for the RTSP stream
#   first_clip   - Initial video file to stream
#
# Output:
#   Creates two concat files:
#     - {stream_name}.concat       - Top-level concat pointing to next.concat
#     - {stream_name}_next.concat  - Contains actual video file list
#
# Dependencies:
#   - ffmpeg: For RTSP streaming
#   - utils.sh: For logging functions
#
# Example:
#   ./publish.sh camera1 /path/to/video.mp4
#   # Starts RTSP stream at rtsp://localhost:8554/camera1
#

stream_name="$1"
first_clip="$2"

# Validate command-line arguments
if [ -z "$stream_name" ] || [ -z "$first_clip" ]; then
  echo "Usage: $0 stream_name first_clip"
  exit 1
fi

# Source logging utilities
. utils.sh
set_log_level "info"

# RTSP server port
port=8554

#######################################
# Start FFmpeg RTSP streaming with concat demuxer.
# Continuously streams video files listed in a concat demuxer file to an
# RTSP endpoint. The stream loops indefinitely and copies video/audio codecs
# without transcoding for efficiency.
#
# Arguments:
#   $1 - Path to concat demuxer file
#   $2 - RTSP URL for output stream
# Returns:
#   0 on success, exits with 1 on failure
#######################################
start_stream() {
    local input_file="$1"
    local output_url="$2"

    info "publishing to $output_url"
    
    # FFmpeg arguments for RTSP streaming
    local ffmpeg_args=(
        -v error                # Show only errors
        -hide_banner            # Hide FFmpeg version info
        -fflags +igndts+genpts  # Ignore DTS, generate PTS for seamless concat
        -re                     # Real-time mode (stream at native frame rate)
        -stream_loop -1         # Loop indefinitely
        -f concat               # Use concat demuxer
        -safe 0                 # Allow absolute file paths in concat file
        -i "$input_file"
        -flush_packets 0        # Optimize packet flushing
        -c:v copy               # Copy video codec (no transcoding)
        -c:a copy               # Copy audio codec (no transcoding)
        -f rtsp                 # RTSP output format
        -fps_mode drop          # Drop frames if necessary to maintain sync
        "$output_url"
    )
    
    # Start ffmpeg streaming
    ffmpeg "${ffmpeg_args[@]}"
    if [ $? -ne 0 ]; then
        error "ffmpeg failed"
        exit 1
    fi
}

#######################################
# Create concat demuxer file structure for video streaming.
# Creates a two-level concat structure:
#   - Main concat file references the "next" concat file twice
#   - The "next" concat file contains the actual video file
# This structure allows updating the video by modifying the "next" file
# while FFmpeg continues reading from the main concat file.
#
# Arguments:
#   $1 - Stream name (used for concat filenames)
#   $2 - Path to first video clip
# Outputs:
#   Creates {stream_name}.concat and {stream_name}_next.concat files
# Returns:
#   None
#######################################
make_concat_files() {
    local stream_name="$1"
    local first_clip="$2"

    local next_concat="${stream_name}_next.concat"
    local concat_file="${stream_name}.concat"

    # Create main concat file pointing to next.concat twice for seamless looping
    echo "ffconcat version 1.0" > $concat_file
    echo "file $next_concat" >> $concat_file
    echo "file $next_concat" >> $concat_file

    # Create next.concat with the actual video file
    echo "ffconcat version 1.0" > $next_concat
    echo "file $first_clip" >> $next_concat
}

# Main script execution
debug "making concat file"
make_concat_files "$stream_name" "$first_clip"

start_stream "${stream_name}.concat" "rtsp://localhost:${port}/${stream_name}"
