import os
import io
import json
import struct
import uuid
import hashlib
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from openai import OpenAI
from vosk import Model, KaldiRecognizer, SetLogLevel

SetLogLevel(-1)  # suppress vosk logs

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Load Vosk model once at startup
print("Loading Vosk model...")
_vosk_model = Model("/app/model")
print("Vosk model loaded")

AUDIO_DIR = "/tmp/emo-audio"
SERVE_HOST = os.environ.get("SERVE_HOST", "192.168.1.64")
AUDIO_URL_BASE = os.environ.get("AUDIO_URL_BASE", f"http://{SERVE_HOST}:{os.environ.get('PORT', '9090')}")
SERVE_PORT = int(os.environ.get("PORT", "9090"))
LIVING_AI_HOST = "eu1-api.living.ai"

os.makedirs(AUDIO_DIR, exist_ok=True)

SYSTEM_PROMPT = """You are EMO, a small cute desktop robot.
Keep responses short (1-2 sentences max). Be playful and friendly.
Always respond in the same language the user speaks.
Never mention that you are an AI or language model."""

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

# Response cache: normalized text → {audio_id, text, ts}
response_cache = {}
cache_lock = threading.Lock()
CACHE_TTL = 3600
CACHE_MAX = 200


# ── helpers ──────────────────────────────────────────────────────────────────

def normalize(text):
    return text.lower().strip().rstrip("?!.,")

def cache_get(text):
    key = normalize(text)
    with cache_lock:
        e = response_cache.get(key)
        if e and (time.time() - e["ts"]) < CACHE_TTL:
            if os.path.exists(os.path.join(AUDIO_DIR, f"{e['audio_id']}.mp3")):
                return e
    return None

def cache_set(text, audio_id, resp_text):
    key = normalize(text)
    with cache_lock:
        if len(response_cache) >= CACHE_MAX:
            oldest = min(response_cache, key=lambda k: response_cache[k]["ts"])
            del response_cache[oldest]
        response_cache[key] = {"audio_id": audio_id, "text": resp_text, "ts": time.time()}

def match_trigger(text):
    lower = text.lower()
    for t in TRIGGERS:
        if t["phrase"].lower() in lower:
            return t["action"]
    return None

def make_audio_url(audio_id):
    return f"{AUDIO_URL_BASE}/tts/dl/{audio_id}"

def tts_sync(text, audio_id):
    path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
    if os.path.exists(path):
        return True
    try:
        tts = client.audio.speech.create(model="tts-1", voice="nova", input=text)
        tts.write_to_file(path)
        return True
    except Exception as e:
        print(f"TTS error: {e}")
        return False

def _pcm_be_to_le(pcm_be):
    # EMO sends 16-bit big-endian signed PCM; convert to little-endian for Vosk
    n = len(pcm_be) // 2 * 2
    samples = struct.unpack(">" + "h" * (n // 2), pcm_be[:n])
    return struct.pack("<" + "h" * len(samples), *samples)

def vosk_transcribe(audio_bytes):
    # EMO sends raw 16-bit big-endian signed PCM at 16000 Hz (no WAV header)
    try:
        pcm_le = _pcm_be_to_le(audio_bytes)
        rec = KaldiRecognizer(_vosk_model, 16000)
        rec.AcceptWaveform(pcm_le)
        result = json.loads(rec.FinalResult())
        text = result.get("text", "").strip()
        print(f"Vosk: {text!r}")
        return text
    except Exception as e:
        print(f"Vosk error: {e}")
        return ""

def gpt_respond(query_text):
    try:
        chat = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query_text},
            ],
            max_tokens=60,
        )
        return chat.choices[0].message.content.strip()
    except Exception as e:
        print(f"GPT error: {e}")
        return ""

# ── response builders ─────────────────────────────────────────────────────────

def build_speak_response(query_text, resp_text, audio_url, lang, idx):
    return {
        "queryId": str(uuid.uuid4()),
        "queryResult": {
            "queryText": query_text,
            "intent": {"name": "chatgpt_speak", "confidence": 1},
            "rec_behavior": "speak",
            "behavior_paras": {
                "txt": resp_text,
                "url": audio_url,
                "pre_animation": "",
                "post_animation": "",
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
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "time"
        qr["behavior_paras"] = {"utility_type": "time"}
    elif action == "show_date":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "date"
        qr["behavior_paras"] = {"utility_type": "date"}
    elif action == "show_battery":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "battery"
        qr["behavior_paras"] = {"utility_type": "battery"}

    # ── Volume ─────────────────────────────────────────────────────────────────
    elif action == "volume_up":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "max"}}
    elif action == "volume_down":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "min"}}
    elif action == "volume_mid":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "mid"}}
    elif action == "volume_mute":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "volume_to"
        qr["behavior_paras"] = {"utility_type": "volume", "volume": {"operation": "mute"}}

    # ── Light ──────────────────────────────────────────────────────────────────
    elif action == "light_on":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "turn_on_light"
        qr["behavior_paras"] = {"utility_type": "light", "light": {"hsl": [50, 100, 100], "room": ""}}
    elif action == "light_off":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "turn_off_light"
        qr["behavior_paras"] = {"utility_type": "turn_off_light"}

    # ── Photo ──────────────────────────────────────────────────────────────
    elif action == "take_photo":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "take_photo"
        qr["behavior_paras"] = {"utility_type": "take_photo"}

    # ── Sing ───────────────────────────────────────────────────────────────
    elif action == "sing":
        qr["rec_behavior"] = "play_music"
        qr["intent"]["name"] = "play_music_sing"
        qr["behavior_paras"] = {"type": "sing"}

    # ── Update ─────────────────────────────────────────────────────────────────
    elif action == "check_update":
        qr["rec_behavior"] = "utilities"
        qr["intent"]["name"] = "check_update"
        qr["behavior_paras"] = {"utility_type": "check_update"}

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
    """Main pipeline: pending_action → pending_say → Whisper → triggers → cache → GPT+TTS"""
    global pending_action, pending_say

    # 1. Pending action (injected via API)
    with pending_action_lock:
        action = pending_action
        pending_action = None
    if action:
        print(f"Process: pending action → {action}")
        return build_action_response(action, "", lang, idx)

    # 2. Pending say (injected via /say)
    with pending_say_lock:
        say = pending_say
        pending_say = None
    if say:
        print(f"Process: pending say → {say['text']}")
        return build_speak_response("", say["text"], say["url"], lang, idx)

    # 3. Vosk STT
    query_text = vosk_transcribe(audio_bytes)

    if not query_text:
        return build_out_of_scope(lang, idx)

    # 4. Trigger match
    action = match_trigger(query_text)
    if action:
        print(f"Trigger: {query_text!r} → {action}")
        return build_action_response(action, query_text, lang, idx)

    # 5. Cache hit
    cached = cache_get(query_text)
    if cached:
        print(f"Cache hit: {query_text!r}")
        return build_speak_response(query_text, cached["text"], make_audio_url(cached["audio_id"]), lang, idx)

    # 6. GPT + TTS
    resp_text = gpt_respond(query_text)
    if not resp_text:
        return build_out_of_scope(lang, idx)

    audio_id = hashlib.md5(f"{query_text}{time.time()}".encode()).hexdigest()[:12]
    ok = tts_sync(resp_text, audio_id)
    if not ok:
        return build_out_of_scope(lang, idx)
    cache_set(query_text, audio_id, resp_text)
    print(f"GPT done (no TTS): {query_text!r} → {resp_text!r}")
    return build_speak_response(query_text, resp_text, make_audio_url(audio_id), lang, idx)


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
            self._json_response({"status": "ok", "cache": len(response_cache)})

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
