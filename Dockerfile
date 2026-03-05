# Use the Python Alpine image
FROM python:alpine

# Install system dependencies
RUN apk add --no-cache ffmpeg

# Add Intel hardware acceleration support for ffmpeg (optional)
# RUN apk add --no-cache \
#     libva-intel-driver \
#     intel-media-driver

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
