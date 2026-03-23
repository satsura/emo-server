FROM python:3.12-slim

RUN apt-get update -qq && \
    apt-get install -y -qq bluetooth bluez libdbus-1-dev libglib2.0-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir bleak bleak-retry-connector

COPY server.py /app/server.py

WORKDIR /app

ENV PORT=8091
ENV EMO_ADDR=""

EXPOSE 8091

CMD ["python3", "server.py"]
