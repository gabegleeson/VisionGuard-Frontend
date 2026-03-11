FROM python:3.11-slim

# Install OpenCV's system-level dependencies (cv2 needs these)
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy and install Python dependencies first (for caching efficiency)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your source code
COPY src/ ./src/

# Run the app
CMD ["python", "-m", "visionguard.main"]