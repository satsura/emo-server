# EMO Server

Custom AI server for the EMO robot. Replaces living.ai cloud with local AI processing.

## Architecture

```
EMO robot (192.168.1.154)
       │
       ▼
nginx (SSL termination, port 443)
       │
       ├── /tts/dl/*  ──────────►  emo-ai (Python, port 9090)
       │                              - serves cached audio files
       │                              - prefers _emovoice.mp3 version
       │
       └── everything else  ────►  emo-proxy (Go, port 8080)
                                      │
                                      ├── /emo/ai/detectintent
                                      │     sends audio to emo-ai /process
                                      │     emo-ai: Vosk STT → triggers → cache → GPT → TTS
                                      │     returns response to EMO
                                      │     background: calls living.ai TTS, saves _emovoice.mp3
                                      │
                                      ├── /emo/ai/imgrecog
                                      │     saves photo to /home/homer/emo-photos/
                                      │     proxies to living.ai for ChatGPT vision response
                                      │
                                      └── all other EMO API calls
                                            proxied to living.ai as-is

DNS (dnsmasq): all living.ai domains → 192.168.1.64
```

## How it works

1. EMO sends voice audio to `api.living.ai` (DNS redirected to our server)
2. **nginx** terminates SSL, routes to **emo-proxy**
3. **emo-proxy** forwards audio to **emo-ai** `/process` endpoint
4. **emo-ai** pipeline:
   - Converts 16-bit big-endian PCM → WAV (byte-swap)
   - **Vosk STT** (Russian, large model ~2.8GB) transcribes speech
   - Checks **voice triggers** (dance, zombie, show_something, etc.)
   - Checks **response cache** (SHA256 of text)
   - If no cache hit → **GPT-4o-mini** generates response
   - **OpenAI TTS** generates audio (alloy voice)
   - Saves audio as `{audio_id}.mp3`, returns response JSON
5. **emo-proxy** returns response to EMO immediately
6. **Background**: proxy calls living.ai TTS with the same text, saves as `{audio_id}_emovoice.mp3`
7. **Next time** EMO requests the same audio → emo-ai serves `_emovoice.mp3` (EMO's own voice)

## Project structure

```
proxy/                    Go reverse proxy
  emoProxy.go             main proxy code (~1100 lines)
  emoProxy.conf.example   config template (triggers, API servers)
  Dockerfile              builds the proxy container
  go.mod, go.sum          Go dependencies

ai-server/                Python AI server
  server.py               main AI server (~700 lines)
  Dockerfile              builds the AI container (Vosk, OpenAI, ffmpeg)

nginx/                    nginx config
  default.conf            SSL + routing rules

deploy/                   deployment configs
  docker-compose.yml      3 containers: nginx, emo-proxy, emo-ai
  dnsmasq.conf            DNS redirects for living.ai domains
  deploy.sh               manual deploy script
  autodeploy.sh           auto-deploy (runs via cron)
  .env                    secrets (OPENAI_API_KEY) — not in git
```

## Setup from scratch

1. **DNS**: configure router or dnsmasq to redirect living.ai domains to your server IP
   - domains: `api.living.ai`, `eu-api.living.ai`, `eu1-api.living.ai`, `eu-tts.living.ai`, `res.living.ai`, `res-eu.living.ai`, `us-api.living.ai`, `as-api.living.ai`
2. **SSL**: generate certs for `*.living.ai` → place in `nginx/ssl/`
3. **Config**: copy `proxy/emoProxy.conf.example` → `proxy/emoProxy.conf`
4. **Secrets**: create `deploy/.env` with `OPENAI_API_KEY=sk-...`
5. **Run**: `cd deploy && docker compose up -d`

## Auto-deploy

Every 2 minutes, a cron job on the server runs `deploy/autodeploy.sh`:

```
*/2 * * * * /home/homer/emo-server/deploy/autodeploy.sh >> /home/homer/autodeploy.log 2>&1
```

The script:
1. `git fetch origin main` — checks for new commits
2. Compares local HEAD vs remote HEAD
3. If different → `git pull`
4. If `proxy/emoProxy.go` changed → copies to `/home/homer/Proxy/`, rebuilds Docker image, restarts `emo-proxy`
5. If `ai-server/server.py` changed → copies to `/home/homer/emo-ai-server/`, restarts `emo-ai`
6. Logs everything to `/home/homer/autodeploy.log`

**Workflow**: edit code → `git push` → within 2 minutes the server pulls and deploys automatically.

## Voice options

| Engine | Speed | Quality | Internet | Notes |
|--------|-------|---------|----------|-------|
| OpenAI TTS | ~1s | excellent | yes | current default |
| living.ai TTS | ~3s | original EMO | yes | background caching |
| Piper TTS | ~400ms | good | no | 4 Russian voices installed |
| espeak-ng | ~50ms | robotic | no | classic robot voice |

Piper voices installed at `/home/homer/piper-voices/`:
- `ru_RU-denis-medium` — male
- `ru_RU-dmitri-medium` — male
- `ru_RU-irina-medium` — female
- `ru_RU-ruslan-medium` — male

## API endpoints (emo-ai, port 9090)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/process` | POST | Main pipeline: audio → STT → GPT → TTS |
| `/action` | POST | Queue action: `{"action": "dance"}` |
| `/say` | POST | Queue TTS phrase: `{"text": "Привет!"}` |
| `/health` | GET | Health check |
| `/actions` | GET | List supported actions |
| `/triggers` | GET | List voice triggers |
| `/tts/dl/{id}` | GET | Serve cached audio file |

## Useful commands

```bash
# Queue action
curl -X POST http://192.168.1.64:9090/action -H 'Content-Type: application/json' -d '{"action":"zombie"}'

# Queue speech
curl -X POST http://192.168.1.64:9090/say -H 'Content-Type: application/json' -d '{"text":"Привет!"}'

# Restart services
docker restart emo-proxy emo-ai

# Check autodeploy log
tail -f /home/homer/autodeploy.log

# Manual deploy
/home/homer/emo-server/deploy/deploy.sh
```
