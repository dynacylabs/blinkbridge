# Use the Python Alpine image
FROM python:alpine

# Install necessary packages
RUN apk add --no-cache \
    ffmpeg \
    && pip install --upgrade pip \
    && pip install rich blinkpy aiohttp

# Add Intel hardware acceleration support for ffmpeg
# RUN apk add --no-cache \
#     libva-intel-driver \
#     intel-media-driver

# Add blinkbridge source
COPY blinkbridge /app/blinkbridge

# Set the working directory
WORKDIR /app/

# Set the entry point to run the blinkbridge main module
ENTRYPOINT ["python", "-m", "blinkbridge.main"]
