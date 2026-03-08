#!/bin/bash
# Manual deploy: pull latest code and restart services
set -e

cd /home/homer/emo-server

echo "Pulling latest..."
git pull origin main

echo "Rebuilding and restarting..."
cd deploy
docker compose up -d --build

echo "Done! $(date)"
