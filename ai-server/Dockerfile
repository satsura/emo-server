FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends wget unzip libatomic1     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Vosk Russian large model (~1.5GB), remove RNNLM to reduce RAM usage
RUN wget -q https://alphacephei.com/vosk/models/vosk-model-ru-0.42.zip     && unzip -q vosk-model-ru-0.42.zip     && rm vosk-model-ru-0.42.zip     && mv vosk-model-ru-0.42 /app/model     && rm -rf /app/model/rnnlm

COPY server.py .
CMD ["python", "-u", "server.py"]
