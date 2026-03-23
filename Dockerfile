# Use the Python Alpine image
FROM python:alpine

# Install system dependencies (ttf-freefont provides fonts for FFmpeg drawtext overlay)
RUN apk add --no-cache ffmpeg ttf-freefont

# Uncomment ONE of the following blocks for hardware-accelerated encoding:

# Intel VAAPI (broadest Intel/AMD GPU support)
# RUN apk add --no-cache libva-intel-driver intel-media-driver mesa-va-gallium

# Intel QSV (Intel Quick Sync, higher quality, Intel GPUs only)
# RUN apk add --no-cache intel-media-driver

# NVIDIA NVENC — requires nvidia-container-toolkit on host;
# consider using nvidia/cuda base image instead of python:alpine

# Set the working directory
WORKDIR /app/

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Add blinkbridge source
COPY blinkbridge /app/blinkbridge

# Set the entry point to run the blinkbridge main module
ENTRYPOINT ["python", "-m", "blinkbridge.main"]
