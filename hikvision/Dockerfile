FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY server.py /app/server.py
CMD ["python", "-u", "server.py"]
