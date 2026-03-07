# EMO Server

Custom AI server for EMO robot. Replaces living.ai cloud with local AI processing.

## Architecture

```
EMO robot → nginx (SSL) → emo-proxy (Go) → emo-ai (Python)
```

- **emo-proxy** — Go reverse proxy, intercepts EMO API calls
- **emo-ai** — Python AI backend: Vosk STT → GPT → TTS
- **nginx** — SSL termination, routing

## Components

### proxy/
Go proxy that intercepts EMO's communication with living.ai servers.
- Routes `/process` (detectintent) to the AI server
- Background EMO voice caching via living.ai TTS
- Photo capture from `/emo/ai/imgrecog`
- Voice trigger detection (dance, zombie, etc.)

### ai-server/
Python AI server (port 9090):
- **Vosk** speech-to-text (Russian, large model)
- **GPT** for response generation
- **OpenAI TTS** / **Piper** for voice synthesis
- Action queue (dance, zombie, show_something)
- Response caching

### nginx/
SSL termination and request routing.

### deploy/
- `docker-compose.yml` — container orchestration
- `dnsmasq.conf` — DNS redirect living.ai domains to local server

## Setup

1. Configure DNS to redirect living.ai domains to your server
2. Generate SSL certs for api.living.ai
3. Copy `proxy/emoProxy.conf.example` to `proxy/emoProxy.conf`
4. Set your OpenAI API key in ai-server config
5. Run `docker compose up -d` from deploy/

## Voice Options

- **OpenAI TTS** — high quality, requires internet
- **Piper TTS** — local, fast (~400ms), Russian voices: denis, dmitri, irina, ruslan
- **espeak-ng** — local, instant (~50ms), robot voice
- **living.ai TTS** — original EMO voice (background caching)
