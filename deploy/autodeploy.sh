#!/bin/bash
cd /home/homer/emo-server
LOCAL=$(git rev-parse HEAD)
git fetch origin main -q 2>/dev/null
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date): New changes detected, deploying..."
    git pull origin main -q

    CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")

    # Rebuild proxy if Go code changed
    if echo "$CHANGED" | grep -q "proxy/"; then
        echo "  Proxy code changed, rebuilding..."
        cd deploy && docker compose up -d --build emo-proxy 2>/dev/null
        cd /home/homer/emo-server
        echo "  Proxy updated and restarted"
    fi

    # Restart AI server if Python code changed (mounted as volume)
    if echo "$CHANGED" | grep -q "ai-server/"; then
        docker restart emo-ai 2>/dev/null
        echo "  AI server restarted"
    fi

    # Restart nginx if config changed
    if echo "$CHANGED" | grep -q "nginx/"; then
        docker restart emo-nginx 2>/dev/null
        echo "  Nginx restarted"
    fi

    echo "$(date): Deploy complete"
fi
