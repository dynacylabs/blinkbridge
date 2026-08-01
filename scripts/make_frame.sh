#!/bin/bash
#
# make_frame.sh - Create a still video from the last frame of an input video
#
# This script extracts the last frame from an input video and creates a new video
# by looping that frame for a specified duration. The output video matches all
# codec parameters (resolution, frame rate, color space, etc.) of the input.
#
# Usage:
#   ./make_frame.sh input_video.mp4 output_video.mp4
#
# Arguments:
#   input_video  - Path to the source video file
#   output_video - Path where the still video should be saved
#
# Dependencies:
#   - ffmpeg: For video processing
#   - ffprobe: For extracting video parameters
#   - utils.sh: For logging functions
#
# Example:
#   ./make_frame.sh camera_clip.mp4 still_frame.mp4
#

input_video="$1"
output_video="$2"

# Source logging utilities
. utils.sh
set_log_level "debug"

# Duration of the output still video in seconds
output_duration=0.5

# Validate command-line arguments
if [ -z "$input_video" ] || [ -z "$output_video" ]; then
  echo "Usage: $0 input_video.mp4 output_video.mp4"
  exit 1
fi

#######################################
# Extract codec parameters from a video file using ffprobe.
# Sets the global 'params' variable with extracted parameters.
#
# Arguments:
#   $1 - Path to the video file
# Outputs:
#   Writes parameters to global 'params' variable
# Returns:
#   0 on success, 1 on failure
#######################################
extract_parameters() {
  local video_file=$1
  local ffprobe_params=(
    -hide_banner
    -v error
    -show_entries
    stream=codec_name,pix_fmt,width,height,bit_rate,r_frame_rate,color_space,color_transfer,color_primaries,time_base,profile,level,color_range,channels,channel_layout,sample_rate
    -of default=noprint_wrappers=1
    "$video_file"
  )
  
  params=$(ffprobe "${ffprobe_params[@]}" 2>&1)
  if [ $? -ne 0 ]; then
    error "ffprobe failed to extract parameters."
    exit 1
  fi
}

# Global associative array storing codec-specific parameters
# Format: codec_params["codecname_parameter"]="value"
declare -A codec_params

#######################################
# Parse ffprobe output into codec-specific parameter array.
# Parses ffprobe output and organizes parameters by codec type (video/audio).
# Parameters are stored with keys like "h264_width" or "aac_channels".
#
# Globals:
#   params - Raw ffprobe output (read)
#   codec_params - Associative array of parsed parameters (write)
# Returns:
#   None
#######################################
parse_parameters() {
  local current_codec_name=""
  while read -r line; do
    key=$(echo "$line" | cut -d'=' -f1)
    value=$(echo "$line" | cut -d'=' -f2)
    # Track which codec the parameters belong to
    if [[ $key == "codec_name" ]]; then
      current_codec_name="$value"
    fi
    # Store parameter with codec prefix for easy lookup
    codec_params["${current_codec_name}_$key"]="$value"
  done <<< "$params"
}

#######################################
# Print all extracted codec parameters for debugging.
# Iterates through the codec_params array and logs each parameter.
#
# Globals:
#   codec_params - Associative array to print (read)
# Outputs:
#   Writes parameter list to debug log
# Returns:
#   None
#######################################
print_parameters() {
  debug "parameters:"
  for key in "${!codec_params[@]}"; do
    debug "   $key: ${codec_params[$key]}"
  done
}

#######################################
# Extract the last frame from the input video.
# Uses ffmpeg to save the final frame of the video as a JPEG image.
# The frame is saved to a temporary file with a random name.
#
# Arguments:
#   None (uses global input_video)
# Globals:
#   input_video - Path to source video (read)
# Outputs:
#   Writes last frame to temporary JPEG file
#   Writes filename to stdout
# Returns:
#   0 on success, exits with 1 on failure
#######################################
extract_last_frame() {
  local random_filename=$(mktemp last_frame_XXXXXX.jpg)
  local ffmpeg_params=(
    -hide_banner
    -y                      # Overwrite output file
    -v error                # Show only errors
    -sseof -1               # Seek to 1 second before end of file
    -i "$input_video"
    -update 1               # Update a single image file
    -pix_fmt yuv420p        # Standard pixel format for compatibility
    -q:v 1                  # Highest quality JPEG (1-31 scale)
    "$random_filename"
  )

  ffmpeg "${ffmpeg_params[@]}" 2>&1
  if [ $? -ne 0 ]; then
    error "ffmpeg failed to extract the last frame."
    exit 1
  fi
  
  echo "$random_filename"
}

#######################################
# Create a still video by looping the last frame.
# Extracts the last frame from the input video and creates a new video
# that loops this frame for the specified duration. The output matches
# all video and audio codec parameters from the input.
#
# Globals:
#   codec_params - Associative array of video/audio parameters (read)
#   output_duration - Duration of output video in seconds (read)
#   output_video - Path for output file (read)
# Outputs:
#   Creates output video file
# Returns:
#   0 on success, exits with 1 on failure
#######################################
create_video() {
  local last_frame_filename=$(extract_last_frame)
  
  # Extract time base denominator for video track timescale
  local time_base_denominator=$(echo "${codec_params[h264_time_base]}" | cut -d'/' -f2)
  
  # Calculate FPS from frame rate fraction
  local fps_value=$(echo "${codec_params[h264_r_frame_rate]}" | awk -F '/' '{ print $1 / $2 }')
  
  local ffmpeg_params=(
    -hide_banner
    -y                      # Overwrite output file
    -v error                # Show only errors
    -loop 1                 # Loop input image
    -i "$last_frame_filename"
    -f lavfi                # Use lavfi filter for audio
    # Generate silent audio matching input audio format
    -i "anullsrc=channel_layout=${codec_params[aac_channels]}:sample_rate=${codec_params[aac_sample_rate]}"
    
    # Video codec parameters - match input video exactly
    -c:v "${codec_params[h264_codec_name]}"
    -pix_fmt "${codec_params[h264_pix_fmt]}"
    -t "$output_duration"
    -vf "scale=${codec_params[h264_width]}:${codec_params[h264_height]},fps=$fps_value"
    -b:v "${codec_params[h264_bit_rate]}"
    -profile:v "${codec_params[h264_profile]}"
    -level:v "${codec_params[h264_level]}"
    
    # Color space parameters - preserve color accuracy
    -colorspace "${codec_params[h264_color_space]}"
    -color_trc "${codec_params[h264_color_transfer]}"
    -color_primaries "${codec_params[h264_color_primaries]}"
    -color_range "${codec_params[h264_color_range]}"
    
    # MP4 container parameters
    -video_track_timescale "$time_base_denominator"
    -fps_mode passthrough   # Don't modify frame timing
    
    # Audio codec parameters - match input audio
    -c:a aac
    -ar "${codec_params[aac_sample_rate]}"
    -ac "${codec_params[aac_channels]}"
    
    "$output_video"
  )

  ffmpeg "${ffmpeg_params[@]}" 2>&1
  if [ $? -ne 0 ]; then
    error "ffmpeg failed to create the video."
    exit 1
  fi

  # Remove the temporary last frame file
  rm "$last_frame_filename"
}

# Main script execution
debug "getting video parameters"
extract_parameters "$input_video"
parse_parameters
print_parameters

debug "extracting last frame and generating video clip"
create_video