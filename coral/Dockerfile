FROM python:3.9-slim

RUN apt-get update -qq && \
    apt-get install -y -qq libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "numpy<2" \
    tflite-runtime \
    Pillow \
    --extra-index-url https://google-coral.github.io/py-repo/ \
    pycoral

COPY recognize.py /app/recognize.py

WORKDIR /app

ENV MODELS_DIR=/models
ENV PORT=8090
ENV THRESHOLD=0.4
ENV TOP_K=10

EXPOSE 8090

CMD ["python3", "recognize.py"]
