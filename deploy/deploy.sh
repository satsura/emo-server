#!/bin/bash
# Auto-deploy: pull latest code and restart services
set -e

REPO_DIR="/home/homer/emo-server"
cd "$REPO_DIR"

echo "Pulling latest..."
git pull origin main

echo "Updating proxy..."
cp proxy/emoProxy.go /home/homer/Proxy/emoProxy.go
cd /home/homer/Proxy && docker build -t emo-proxy . && cd "$REPO_DIR"

echo "Updating AI server..."
cp ai-server/server.py /home/homer/emo-ai-server/server.py

echo "Restarting containers..."
docker restart emo-proxy emo-ai

echo "Done! $(date)"
