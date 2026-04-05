FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests ultralytics opencv-python-headless
# Download YOLOv8n model at build time
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
COPY server.py /app/server.py
CMD ["python", "-u", "server.py"]
