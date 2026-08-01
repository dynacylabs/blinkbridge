#!/bin/bash
#
# stage_video.sh - Queue a video clip for RTSP streaming
#
# This script stages a new video clip for streaming by updating the concat
# demuxer file structure. It first queues the full video, then creates a
# still-frame video from the last frame to loop until the next clip arrives.
# This provides seamless transitions between video clips.
#
# Workflow:
#   1. Backup current concat file
#   2. Update concat file to include new video clip
#   3. Start background process to extract last frame and create still video
#   4. Wait for video clip to finish playing (~0.51s)
#   5. Update concat file to loop the still video
#
# Usage:
#   ./stage_video.sh stream_name video_file.mp4
#
# Arguments:
#   stream_name - Stream identifier (must match publish.sh stream_name)
#   video_file  - Path to video clip to stage
#
# Dependencies:
#   - make_frame.sh: Creates still video from last frame
#   - utils.sh: Logging functions
#
# Files Modified:
#   - {stream_name}_next.concat: Updated with new video then still frame
#   - {stream_name}_still.mp4: Still video created from last frame
#
# Example:
#   ./stage_video.sh camera1 /clips/motion_detected.mp4
#

stream_name="$1"
full_video="$2"

# Validate command-line arguments
if [ -z "$full_video" ] || [ -z "$stream_name" ]; then
  echo "Usage: $0 stream_name video"
  exit 1
fi

# Source logging utilities
. utils.sh
set_log_level "debug"

# File paths based on stream name
last_frame="${stream_name}_still.mp4"
next_concat="${stream_name}_next.concat"

# Backup current concat list and enqueue the new video clip
debug "queueing clip"
cp -f ${next_concat} ${next_concat}.prev

# Update concat file to play the full video
echo "ffconcat version 1.0" > ${next_concat}
echo "file $full_video" >> ${next_concat}

# Start background process to create still video from last frame
# This runs in parallel while the video plays
debug "making last frame clip"
./make_frame.sh "$full_video" "$last_frame" &
pid_make_frame=$!

# Wait for the current video clip to finish playing
# Sleep duration should match or slightly exceed video clip duration (0.5s)
debug "waiting for current clip to finish"
sleep 0.51

# Wait for the still frame video generation to complete
debug "waiting last frame to be generated"
wait $pid_make_frame
if [ $? -ne 0 ]; then
    error "failed to extract last frame"
    # Restore previous concat file on failure
    mv ${next_concat}.prev ${next_concat}
    exit 1
fi

# Update concat file to loop the still frame video
# This provides a seamless transition until the next clip arrives
echo "ffconcat version 1.0" > ${next_concat}
echo "file $last_frame" >> ${next_concat}

info "made and queued input and last frame"

# Clean up backup file
rm ${next_concat}.prev
