FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends wget sox libsox-fmt-mp3 ffmpeg     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download faster-whisper small model and silero-vad
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
RUN python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True)"

COPY server.py .

ENV WHISPER_MODEL=small
ENV WHISPER_LANG=ru
ENV VAD_THRESHOLD=0.4

CMD ["python", "-u", "server.py"]
