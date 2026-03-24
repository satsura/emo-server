import os
import json
import struct
import uuid
import hashlib
import threading
import time
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import torch
import numpy as np
from faster_whisper import WhisperModel

# Load silero-vad
print("Loading silero-vad...")
vad_model, vad_utils = torch.hub.load(repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True)
(get_speech_timestamps, _, read_audio, _, _) = vad_utils
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.4"))

# Load faster-whisper
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_LANG = os.environ.get("WHISPER_LANG", "ru")
print(f"Loading faster-whisper {WHISPER_MODEL}...")
_whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print(f"faster-whisper {WHISPER_MODEL} loaded.")

AUDIO_DIR = "/tmp/emo-audio"
SERVE_HOST = os.environ.get("SERVE_HOST", "192.168.1.64")
AUDIO_URL_BASE = os.environ.get("AUDIO_URL_BASE", f"http://{SERVE_HOST}:{os.environ.get('PORT', '9090')}")
SERVE_PORT = int(os.environ.get("PORT", "9090"))
LIVING_AI_HOST = "eu1-api.living.ai"
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "http://127.0.0.1:5678/webhook/emo")
RHVOICE_URL = os.environ.get("RHVOICE_URL", "http://127.0.0.1:8080")
RHVOICE_VOICE = os.environ.get("RHVOICE_VOICE", "artemiy")
RHVOICE_RATE = os.environ.get("RHVOICE_RATE", "65")

os.makedirs(AUDIO_DIR, exist_ok=True)

SUPPORTED_ACTIONS = {
    # Dance
    "dance", "dance_lights", "dance_music",
    # Featured games
    "zombie", "show_something", "angry_emo", "fix_bugs", "paint_shot", "tic_tac_toe",
    # About EMO
    "about_health", "about_age", "about_name", "lucky_number",
    # Animals (play_animation)
    "animal_cat", "animal_dog", "animal_fox", "animal_snake",
    "animal_cattle", "animal_pig", "animal_tiger", "animal_wolf",
    # Other
    "bt_stop_music", "sleep", "play_around",
    # Greeting / interaction
    "greeting", "be_quiet", "look_at_me", "explore",
    # Movement
    "move_forward", "move_backward", "move_left", "move_right", "turn_around",
    # System / utilities
    "power_off", "show_time", "show_date", "show_battery",
    "volume_up", "volume_down", "volume_mid", "volume_mute",
    "light_on", "light_off",
    "check_update",
    "sing",
    "take_photo",
    # New from living.ai analysis
    "laser_eye", "go_home", "say_again", "roll_a_dice",
    "come_here", "about_comfort", "play_by_yourself",
    "featured_game_magic", "featured_game_camping",
    "listen_to_voice",
    "bt_start_music",
}

# Triggers: loaded from env or defaults
TRIGGERS = json.loads(os.environ.get("EMO_TRIGGERS", "[]")) or [
    # Dance
    {"phrase": "танцуй",                    "action": "dance"},
    {"phrase": "потанцуй",                  "action": "dance"},
    {"phrase": "станцуй",                   "action": "dance"},
    {"phrase": "давай потанцуем",            "action": "dance"},
    {"phrase": "танцуй с огнями",            "action": "dance_lights"},
    {"phrase": "потанцуй с огнями",          "action": "dance_lights"},
    {"phrase": "танцуй со светом",           "action": "dance_lights"},
    {"phrase": "танцуй под музыку",          "action": "dance_music"},
    # Games
    {"phrase": "зомби",                     "action": "zombie"},
    {"phrase": "стань зомби",               "action": "zombie"},
    {"phrase": "покажи что умеешь",         "action": "show_something"},
    {"phrase": "удиви меня",                "action": "show_something"},
    {"phrase": "рассердись",                "action": "angry_emo"},
    {"phrase": "покажи злость",             "action": "angry_emo"},
    {"phrase": "почини баги",               "action": "fix_bugs"},
    {"phrase": "исправь баги",              "action": "fix_bugs"},
    {"phrase": "надень очки",               "action": "paint_shot"},
    {"phrase": "пейнтбол",                  "action": "paint_shot"},
    {"phrase": "крестики нолики",           "action": "tic_tac_toe"},
    {"phrase": "сыграй в крестики",         "action": "tic_tac_toe"},
    # About EMO
    {"phrase": "как ты себя чувствуешь",    "action": "about_health"},
    {"phrase": "как ты",                    "action": "about_health"},
    {"phrase": "сколько тебе лет",          "action": "about_age"},
    {"phrase": "какой у тебя возраст",      "action": "about_age"},
    {"phrase": "как тебя зовут",            "action": "about_name"},
    {"phrase": "счастливое число",          "action": "lucky_number"},
    {"phrase": "назови счастливое",         "action": "lucky_number"},
    # Animals — keyword matching (any form: покажи/изобрази/стань/будь + животное)
    {"phrase": "кошк",                      "action": "animal_cat"},
    {"phrase": "промяукай",                 "action": "animal_cat"},
    {"phrase": "мяукни",                    "action": "animal_cat"},
    {"phrase": "собак",                     "action": "animal_dog"},
    {"phrase": "полай",                     "action": "animal_dog"},
    {"phrase": "лис",                       "action": "animal_fox"},
    {"phrase": "зме",                       "action": "animal_snake"},
    {"phrase": "зашипи",                    "action": "animal_snake"},
    {"phrase": "коров",                     "action": "animal_cattle"},
    {"phrase": "свин",                      "action": "animal_pig"},
    {"phrase": "хрюкни",                    "action": "animal_pig"},
    {"phrase": "тигр",                      "action": "animal_tiger"},
    {"phrase": "волк",                      "action": "animal_wolf"},
    {"phrase": "завой",                     "action": "animal_wolf"},
    # Sleep / music
    {"phrase": "иди спать",                 "action": "sleep"},
    {"phrase": "спи",                       "action": "sleep"},
    {"phrase": "засыпай",                   "action": "sleep"},
    {"phrase": "выключи музыку",            "action": "bt_stop_music"},
    {"phrase": "стоп музыка",               "action": "bt_stop_music"},
    {"phrase": "поиграй",                   "action": "play_around"},
    {"phrase": "побалуйся",                 "action": "play_around"},
    # Greeting
    {"phrase": "привет",                    "action": "greeting"},
    {"phrase": "здравствуй",                "action": "greeting"},
    {"phrase": "здарова",                   "action": "greeting"},
    {"phrase": "хай",                       "action": "greeting"},
    {"phrase": "добрый день",               "action": "greeting"},
    {"phrase": "доброе утро",               "action": "greeting"},
    {"phrase": "добрый вечер",              "action": "greeting"},
    {"phrase": "спокойной ночи",            "action": "greeting"},
    # Interaction
    {"phrase": "тихо",                      "action": "be_quiet"},
    {"phrase": "замолчи",                   "action": "be_quiet"},
    {"phrase": "стоп",                      "action": "be_quiet"},
    {"phrase": "хватит",                    "action": "be_quiet"},
    {"phrase": "посмотри на меня",          "action": "look_at_me"},
    {"phrase": "смотри на меня",            "action": "look_at_me"},
    {"phrase": "найди меня",               "action": "look_at_me"},
    {"phrase": "исследуй",                 "action": "explore"},
    {"phrase": "осмотрись",                  "action": "explore"},
    {"phrase": "прогуляйся",                "action": "explore"},
    {"phrase": "осмотри",                   "action": "explore"},
    {"phrase": "погуляй",                  "action": "explore"},
    {"phrase": "походи",                   "action": "explore"},
    # Movement
    {"phrase": "вперёд",                   "action": "move_forward"},
    {"phrase": "вперед",                   "action": "move_forward"},
    {"phrase": "иди вперёд",               "action": "move_forward"},
    {"phrase": "назад",                    "action": "move_backward"},
    {"phrase": "иди назад",                "action": "move_backward"},
    {"phrase": "налево",                   "action": "move_left"},
    {"phrase": "влево",                    "action": "move_left"},
    {"phrase": "направо",                  "action": "move_right"},
    {"phrase": "вправо",                   "action": "move_right"},
    {"phrase": "развернись",               "action": "turn_around"},
    {"phrase": "повернись",                "action": "turn_around"},
    {"phrase": "кругом",                   "action": "turn_around"},
    # System
    {"phrase": "выключись",                "action": "power_off"},
    {"phrase": "отключись",                "action": "power_off"},
    {"phrase": "который час",              "action": "show_time"},
    {"phrase": "сколько времени",          "action": "show_time"},
    {"phrase": "какое число",              "action": "show_date"},
    {"phrase": "какой день",               "action": "show_date"},
    {"phrase": "какая дата",               "action": "show_date"},
    {"phrase": "заряд батареи",            "action": "show_battery"},
    {"phrase": "сколько батареи",          "action": "show_battery"},
    {"phrase": "уровень заряда",           "action": "show_battery"},
    # Volume
    {"phrase": "громче",                   "action": "volume_up"},
    {"phrase": "тише",                     "action": "volume_down"},
    {"phrase": "нормальная громкость",     "action": "volume_mid"},
    {"phrase": "без звука",                "action": "volume_mute"},
    # Light
    {"phrase": "включи свет",              "action": "light_on"},
    {"phrase": "включи лампу",             "action": "light_on"},
    {"phrase": "выключи свет",             "action": "light_off"},
    {"phrase": "выключи лампу",            "action": "light_off"},
    # Update
    {"phrase": "проверь обновлен",         "action": "check_update"},
    # Sing
    {"phrase": "спой",                      "action": "sing"},
    {"phrase": "песн",                      "action": "sing"},
    {"phrase": "песенк",                    "action": "sing"},
    {"phrase": "попой",                     "action": "sing"},
    # Photo
    {"phrase": "сфотографируй",             "action": "take_photo"},
    {"phrase": "сделай фото",               "action": "take_photo"},
    {"phrase": "сделай фотк",               "action": "take_photo"},
    {"phrase": "фотограф",                  "action": "take_photo"},
    {"phrase": "сфоткай",                   "action": "take_photo"},
]

pending_say = None
pending_say_lock = threading.Lock()

pending_action = None
pending_action_lock = threading.Lock()

# ── helpers ──────────────────────────────────────────────────────────────────

def match_trigger(text):
    lower = text.lower()
    for t in TRIGGERS:
        if t["phrase"].lower() in lower:
            return t["action"]
    return None

def make_audio_url(audio_id):
    return f"{AUDIO_URL_BASE}/tts/dl/{audio_id}"

def tts_sync(text, audio_id, voice_params=None):
    path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
    if os.path.exists(path):
        return True
    vp = voice_params or {}
    rate = str(vp.get("rate", RHVOICE_RATE))
    pitch = str(vp.get("pitch", 900))
    tremolo_freq = str(vp.get("tremolo_freq", 3))
    tremolo_depth = str(vp.get("tremolo_depth", 25))
    tempo = vp.get("tempo")  # None = no tempo change
    try:
        # RHVoice → WAV
        encoded = urllib.request.quote(text)
        url = f"{RHVOICE_URL}/say?text={encoded}&voice={RHVOICE_VOICE}&format=wav&rate={rate}"
        print(f"TTS URL: {url[:120]}")
        wav_path = os.path.join(AUDIO_DIR, f"{audio_id}_raw.wav")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            wav_data = resp.read()
        # Fix WAV header — RHVoice streams with placeholder sizes
        if len(wav_data) > 44 and wav_data[:4] == b'RIFF':
            file_size = len(wav_data) - 8
            data_offset = wav_data.find(b'data')
            if data_offset > 0:
                data_size = len(wav_data) - data_offset - 8
                wav_data = (wav_data[:4] + struct.pack('<I', file_size) +
                           wav_data[8:data_offset+4] + struct.pack('<I', data_size) +
                           wav_data[data_offset+8:])
        with open(wav_path, "wb") as f:
            f.write(wav_data)
        # sox effects
        sox_cmd = ["sox", wav_path, path, "pitch", pitch, "tremolo", tremolo_freq, tremolo_depth]
        if tempo:
            sox_cmd.extend(["tempo", str(tempo)])
        subprocess.run(sox_cmd, check=True, timeout=10)
        os.remove(wav_path)
        return True
    except Exception as e:
        print(f"TTS error: {e}")
        for p in [wav_path, path]:
            if os.path.exists(p):
                os.remove(p)
        return False

def _pcm_be_to_le(pcm_be):
    # EMO sends 16-bit big-endian signed PCM; convert to little-endian
    n = len(pcm_be) // 2 * 2
    samples = struct.unpack(">" + "h" * (n // 2), pcm_be[:n])
    return struct.pack("<" + "h" * len(samples), *samples)

def vad_check(pcm_le_bytes):
    """Check if audio contains speech using silero-vad. Returns True if speech detected."""
    try:
        audio = np.frombuffer(pcm_le_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        audio_tensor = torch.from_numpy(audio)
        timestamps = get_speech_timestamps(audio_tensor, vad_model,
                                            sampling_rate=16000,
                                            threshold=VAD_THRESHOLD,
                                            min_speech_duration_ms=250)
        has_speech = len(timestamps) > 0
        if has_speech:
            total_ms = sum(t["end"] - t["start"] for t in timestamps) / 16  # samples to ms
            print(f"VAD: speech detected ({len(timestamps)} segments, {total_ms:.0f}ms)")
        return has_speech
    except Exception as e:
        print(f"VAD error: {e}")
        return True  # on error, proceed with STT

def whisper_transcribe(pcm_le_bytes):
    """Transcribe audio using faster-whisper."""
    try:
        audio = np.frombuffer(pcm_le_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = _whisper.transcribe(audio, language=WHISPER_LANG,
                                              beam_size=3,
                                              vad_filter=True,
                                              vad_parameters=dict(
                                                  min_speech_duration_ms=250,
                                                  max_speech_duration_s=30,
                                                  speech_pad_ms=200,
                                              ))
        text = " ".join(seg.text.strip() for seg in segments).strip()
        print(f"Whisper: {text!r} (lang={info.language} prob={info.language_probability:.2f})")
        # Filter low-confidence or non-target language
        if info.language_probability < 0.5:
            print(f"  Low language confidence, ignoring")
            return ""
        return text
    except Exception as e:
        print(f"Whisper error: {e}")
        return ""

def transcribe(audio_bytes):
    """Pipeline: PCM BE→LE → VAD → Whisper."""
    pcm_le = _pcm_be_to_le(audio_bytes)
    if not vad_check(pcm_le):
        print("VAD: no speech")
        return ""
    return whisper_transcribe(pcm_le)

def n8n_query(text):
    """Send text to n8n webhook, return response text or None."""
    try:
        payload = json.dumps({"event": "voice", "text": text, "language": "ru"}).encode()
        req = urllib.request.Request(
            N8N_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        answer = data.get("text", "").strip()
        action = data.get("action", "").strip()
        voice = data.get("voice")  # optional: {rate, pitch, tempo, ...}
        animation = data.get("animation")  # optional: {pre, post}
        if action and action in SUPPORTED_ACTIONS:
            return {"type": "action", "action": action}
        if answer:
            return {"type": "text", "text": answer, "voice": voice, "animation": animation}
    except Exception as e:
        print(f"n8n error: {e}")
    return None


# ── response builders ─────────────────────────────────────────────────────────


def get_livingai_tts(text):
    """Get TTS URL from living.ai cloud (EMO trusts these URLs)."""
    try:
        encoded = urllib.request.quote(text)
        url = f"https://eu1-api.living.ai/emo/speech/tts?q={encoded}&l=ru"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("code") == 200 and data.get("url"):
            return data["url"]
    except Exception as e:
        print(f"Living.ai TTS error: {e}")
    return None

def build_speak_response(query_text, resp_text, audio_url, lang, idx, animation=None):
    anim = animation or {}
    # Default post_animation: chatgpt_end for long responses without animation
    post_anim = ""  # disabled for testing
    if not post_anim and len(resp_text) > 30:
        post_anim = ""
    return {
        "queryId": str(uuid.uuid4()),
        "queryResult": {
            "queryText": query_text,
            "intent": {"name": "chatgpt_speak", "confidence": 1},
            "rec_behavior": "speak",
            "behavior_paras": {
                "txt": resp_text,
                "url": audio_url,
                "pre_animation": anim.get("pre", ""),
                "post_animation": post_anim,
                "post_behavior": "",
                "sentiment": "",
                "listen": 0,
            },
        },
        "languageCode": lang,
        "index": int(idx) if str(idx).isdigit() else 0,
    }

def build_action_response(action, query_text, lang, idx):
    idx_int = int(idx) if str(idx).isdigit() else 0
    base = {
        "queryId": str(uuid.uuid4()),
        "queryResult": {"queryText": query_text, "intent": {"name": action, "confidence": 1}},
        "languageCode": lang,
        "index": idx_int,
    }
    qr = base["queryResult"]

    # ── Dance ────────────────────────────────────────────────────────────────
    if action == "dance":
        qr["rec_behavior"] = "dance"
        qr["behavior_paras"] = []
    elif action == "dance_lights":
        qr["rec_behavior"] = "dance"
        qr["intent"]["name"] = "dance_with_lights"
        qr["behavior_paras"] = {"dance_type": "with_lights"}
    elif action == "dance_music":
        qr["rec_behavior"] = "dance"
        qr["intent"]["name"] = "dance_to_music"
        qr["behavior_paras"] = {"dance_type": "to_music"}

    # ── Featured games ────────────────────────────────────────────────────────
    elif action == "zombie":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_zombie"
        qr["behavior_paras"] = {"game_name": "zombie"}
    elif action == "show_something":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "show_something"
        qr["behavior_paras"] = {"game_name": "show_something"}
    elif action == "angry_emo":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_angry_emo"
        qr["behavior_paras"] = {"game_name": "angry_emo"}
    elif action == "fix_bugs":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_fix_bugs"
        qr["behavior_paras"] = {"game_name": "fix_bugs"}
    elif action == "paint_shot":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_shot"
        qr["behavior_paras"] = {"game_name": "shot"}
    elif action == "tic_tac_toe":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_ttt"
        qr["behavior_paras"] = {"game_name": "ttt"}

    # ── About EMO ─────────────────────────────────────────────────────────────
    elif action == "about_health":
        qr["rec_behavior"] = "about_emo"
        qr["intent"]["name"] = "about_health"
        qr["behavior_paras"] = {"type": "about_health"}
    elif action == "about_age":
        qr["rec_behavior"] = "about_emo"
        qr["intent"]["name"] = "about_age"
        qr["behavior_paras"] = {"type": "age"}
    elif action == "about_name":
        qr["rec_behavior"] = "about_emo"
        qr["intent"]["name"] = "about_name"
        qr["behavior_paras"] = {"type": "about_name"}
    elif action == "lucky_number":
        qr["rec_behavior"] = "about_emo"
        qr["intent"]["name"] = "about_lucky_number"
        qr["behavior_paras"] = {"type": "lucky_number"}

    # ── Animals (play_animation) ──────────────────────────────────────────────
    elif action.startswith("animal_"):
        animal_map = {
            "animal_cat":    "Cat",
            "animal_dog":    "Dog",
            "animal_fox":    "Fox",
            "animal_snake":  "Snake",
            "animal_cattle": "Cattle",
            "animal_pig":    "Pig",
            "animal_tiger":  "Tiger",
            "animal_wolf":   "Wolf",
        }
        anim_name = animal_map.get(action, "Cat")
        qr["rec_behavior"] = "play_animation"
        qr["intent"]["name"] = "animal"
        qr["behavior_paras"] = {"animation_name": anim_name}

    # ── Music / sleep / play ──────────────────────────────────────────────────
    elif action == "bt_stop_music":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "bt_stop_music"
        qr["behavior_paras"] = {"type": "bluetooth", "action": "stop"}
    elif action == "sleep":
        qr["rec_behavior"] = "tree_sleep_and_wake_up"
        qr["intent"]["name"] = "sleep"
        qr["behavior_paras"] = {}
    elif action == "play_around":
        qr["rec_behavior"] = "play_around"
        qr["intent"]["name"] = "play_by_yourself"
        qr["behavior_paras"] = {"play_type": "normal"}

    # ── Greeting ───────────────────────────────────────────────────────────────
    elif action == "greeting":
        qr["rec_behavior"] = "play_animation"
        qr["intent"]["name"] = "greeting"
        qr["behavior_paras"] = {"animation_name": "Hi"}

    # ── Interaction ────────────────────────────────────────────────────────────
    elif action == "be_quiet":
        qr["rec_behavior"] = "stay_still"
        qr["intent"]["name"] = "be_quiet"
        qr["behavior_paras"] = {}
    elif action == "look_at_me":
        qr["rec_behavior"] = "tree_search_and_interact"
        qr["intent"]["name"] = "look_at_me"
        qr["behavior_paras"] = {}
    elif action == "explore":
        qr["rec_behavior"] = "explore"
        qr["intent"]["name"] = "explore"
        qr["behavior_paras"] = {}

    # ── Movement ───────────────────────────────────────────────────────────────
    elif action == "move_forward":
        qr["rec_behavior"] = "basic_move"
        qr["intent"]["name"] = "forward"
        qr["behavior_paras"] = {"type": "forward"}
    elif action == "move_backward":
        qr["rec_behavior"] = "basic_move"
        qr["intent"]["name"] = "backward"
        qr["behavior_paras"] = {"type": "backward"}
    elif action == "move_left":
        qr["rec_behavior"] = "basic_move"
        qr["intent"]["name"] = "left"
        qr["behavior_paras"] = {"type": "left"}
    elif action == "move_right":
        qr["rec_behavior"] = "basic_move"
        qr["intent"]["name"] = "right"
        qr["behavior_paras"] = {"type": "right"}
    elif action == "turn_around":
        qr["rec_behavior"] = "basic_move"
        qr["intent"]["name"] = "turn_around"
        qr["behavior_paras"] = {"type": "turn_around"}

    # ── System ─────────────────────────────────────────────────────────────────
    elif action == "power_off":
        qr["rec_behavior"] = "power_off"
        qr["intent"]["name"] = "power_off"
        qr["behavior_paras"] = {}
    elif action == "show_time":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "time"
        qr["behavior_paras"] = {"utility_type": "time"}
    elif action == "show_date":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "date"
        qr["behavior_paras"] = {"utility_type": "date"}
    elif action == "show_battery":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "battery"
        qr["behavior_paras"] = {"utility_type": "battery"}

    # ── Volume ─────────────────────────────────────────────────────────────────
    elif action == "volume_up":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "max"}}
    elif action == "volume_down":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "min"}}
    elif action == "volume_mid":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "mid"}}
    elif action == "volume_mute":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "mute"}}

    # ── Light ──────────────────────────────────────────────────────────────────
    elif action == "light_on":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "turn_on_light"
        qr["behavior_paras"] = {"utility_type": "light", "light": {"hsl": [50, 100, 100], "room": ""}}
    elif action == "light_off":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "turn_off_light"
        qr["behavior_paras"] = {"utility_type": "turn_off_light"}

    # ── Photo ──────────────────────────────────────────────────────────────
    elif action == "take_photo":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "take_photo"
        qr["behavior_paras"] = {"utility_type": "take_photo"}

    # ── Sing ───────────────────────────────────────────────────────────────
    elif action == "sing":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "play_music_sing"
        qr["behavior_paras"] = {"type": "sing"}

    # ── Update ─────────────────────────────────────────────────────────────────
    elif action == "check_update":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "check_update"
        qr["behavior_paras"] = {"utility_type": "check_update"}

    elif action == "laser_eye":
        qr["rec_behavior"] = "play_animation"
        qr["intent"]["name"] = "laser_eye"
        qr["behavior_paras"] = {"animation_name": "laser_eye"}

    elif action == "go_home":
        qr["rec_behavior"] = "go_home"
        qr["intent"]["name"] = "go_home"
        qr["behavior_paras"] = {}

    elif action == "say_again":
        qr["rec_behavior"] = "speak"
        qr["intent"]["name"] = "say_again"
        qr["behavior_paras"] = {"type": "say_again"}

    elif action == "roll_a_dice":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_roll_a_dice"
        qr["behavior_paras"] = {"game_name": "roll_a_dice"}

    elif action == "come_here":
        qr["rec_behavior"] = "move_to_target"
        qr["intent"]["name"] = "come_here"
        qr["behavior_paras"] = {"should_go": 1}

    elif action == "about_comfort":
        qr["rec_behavior"] = "about_emo"
        qr["intent"]["name"] = "about_comfort"
        qr["behavior_paras"] = {"type": "about_comfort"}

    elif action == "featured_game_magic":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_magic"
        qr["behavior_paras"] = {"game_name": "magic"}

    elif action == "featured_game_camping":
        qr["rec_behavior"] = "featured_game"
        qr["intent"]["name"] = "featured_game_camping"
        qr["behavior_paras"] = {"game_name": "camping"}

    elif action == "listen_to_voice":
        qr["rec_behavior"] = "listen"
        qr["intent"]["name"] = "listen_to_voice"
        qr["behavior_paras"] = {}

    elif action == "bt_start_music":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "bt_enter_music"
        qr["behavior_paras"] = {"type": "bluetooth", "action": "enter"}

    elif action == "play_by_yourself":
        qr["rec_behavior"] = "play_around"
        qr["intent"]["name"] = "play_by_yourself"
        qr["behavior_paras"] = {"play_type": "normal"}

    return base

def build_out_of_scope(lang, idx):
    return {
        "queryId": str(uuid.uuid4()),
        "queryResult": {
            "queryText": "",
            "intent": {"name": "out_of_scope", "confidence": 1},
            "rec_behavior": "play_animation",
            "behavior_paras": {"animation_name": "dont_understand"},
        },
        "languageCode": lang,
        "index": int(idx) if str(idx).isdigit() else 0,
    }

# ── main process handler ──────────────────────────────────────────────────────

def process_audio(audio_bytes, lang, idx):
    """Main pipeline: pending_action → pending_say → VAD+Whisper STT → n8n (brain)"""
    global pending_action, pending_say

    # 1. Pending action (injected via /action API)
    with pending_action_lock:
        action = pending_action
        pending_action = None
    if action:
        print(f"Process: pending action → {action}")
        return build_action_response(action, "", lang, idx)

    # 2. Pending say (injected via /say API)
    with pending_say_lock:
        say = pending_say
        pending_say = None
    if say:
        print(f"Process: pending say → {say['text']}")
        return build_speak_response("", say["text"], say["url"], lang, idx)

    # 3. VAD + Whisper STT
    query_text = transcribe(audio_bytes)
    if not query_text:
        return build_out_of_scope(lang, idx)

    # 4. n8n — the brain
    n8n_result = n8n_query(query_text)
    if n8n_result:
        if n8n_result["type"] == "action":
            print(f"n8n → action: {query_text!r} → {n8n_result['action']}")
            return build_action_response(n8n_result["action"], query_text, lang, idx)
        resp_text = n8n_result["text"]
        voice_params = n8n_result.get("voice")
        animation = n8n_result.get("animation")
        print(f"n8n → speak: {query_text!r} → {resp_text!r}" + (f" voice={voice_params}" if voice_params else "") + (f" anim={animation}" if animation else ""))
        audio_id = hashlib.md5(f"{query_text}{time.time()}".encode()).hexdigest()[:12]
        # Try living.ai cloud TTS first (EMO trusts these URLs)
        tts_url = get_livingai_tts(resp_text)
        if tts_url:
            print(f"Living.ai TTS: {tts_url}")
            return build_speak_response("", resp_text, tts_url, lang, idx, animation)
        # Fallback: local RHVoice TTS
        if tts_sync(resp_text, audio_id, voice_params):
            return build_speak_response("", resp_text, make_audio_url(audio_id), lang, idx, animation)

    # 5. Emergency fallback (n8n unreachable) — local trigger match
    action = match_trigger(query_text)
    if action:
        print(f"FALLBACK trigger: {query_text!r} → {action}")
        return build_action_response(action, query_text, lang, idx)

    # 6. n8n down, no trigger match → out of scope
    print(f"No response for: {query_text!r}")
    return build_out_of_scope(lang, idx)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))

        if self.path == "/process":
            audio_bytes = self.rfile.read(length)
            lang = self.headers.get("X-Language", "ru")
            idx = self.headers.get("X-Index", "0")
            result = process_audio(audio_bytes, lang, idx)
            self._json_response(result)

        elif self.path == "/say":
            global pending_say
            body, err = self._parse_json(length)
            if err:
                self.send_error(400, err); return
            text = body.get("text", "").strip()
            if not text:
                self.send_error(400, "text is required"); return
            audio_id = hashlib.md5(f"say:{text}".encode()).hexdigest()[:12]
            audio_url = make_audio_url(audio_id)
            if not tts_sync(text, audio_id):
                self._json_response({"status": "error"}); return
            with pending_say_lock:
                pending_say = {"url": audio_url, "text": text}
            print(f"Queued say: {text}")
            self._json_response({"status": "ok", "url": audio_url, "text": text})

        elif self.path == "/action":
            global pending_action
            body, err = self._parse_json(length)
            if err:
                self.send_error(400, err); return
            action = body.get("action", "").strip()
            if action not in SUPPORTED_ACTIONS:
                self.send_error(400, f"Unknown action. Supported: {', '.join(sorted(SUPPORTED_ACTIONS))}"); return
            with pending_action_lock:
                pending_action = action
            print(f"Queued action: {action}")
            self._json_response({"status": "ok", "action": action})

        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/tts/dl/"):
            audio_id = self.path[len("/tts/dl/"):]
            # Prefer EMO voice version if available
            emo_path = os.path.join(AUDIO_DIR, f"{audio_id}_emovoice.mp3")
            path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
            if os.path.exists(emo_path):
                print(f"Serving EMO voice: {emo_path}")
                self._serve_audio(emo_path)
            elif os.path.exists(path):
                self._serve_audio(path)
            else:
                self._proxy_to_living_ai(self.path)

        elif self.path == "/pending":
            # legacy — kept for compatibility
            global pending_say
            with pending_say_lock:
                result = pending_say or {"url": "", "text": ""}
                pending_say = None
            self._json_response(result)

        elif self.path == "/pending-action":
            global pending_action
            with pending_action_lock:
                result = {"action": pending_action or ""}
                pending_action = None
            self._json_response(result)

        elif self.path == "/health":
            self._json_response({"status": "ok"})

        elif self.path == "/actions":
            self._json_response({"supported": sorted(SUPPORTED_ACTIONS)})

        elif self.path == "/triggers":
            self._json_response(TRIGGERS)

        else:
            self.send_error(404)

    def _parse_json(self, length):
        if length == 0:
            return None, "empty body"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as e:
            return None, str(e)

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_audio(self, filepath):
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_to_living_ai(self, path):
        url = f"https://{LIVING_AI_HOST}{path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EMO/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "audio/mpeg"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"Proxy error: {e}")
            self.send_error(502)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


if __name__ == "__main__":
    print(f"AI server starting on port {SERVE_PORT}")
    print(f"Triggers: {len(TRIGGERS)}, Actions: {', '.join(sorted(SUPPORTED_ACTIONS))}")
    ThreadingHTTPServer(("0.0.0.0", SERVE_PORT), Handler).serve_forever()
