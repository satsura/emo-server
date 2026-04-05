"""Hikvision NVR watcher — smart events (human detection) → snapshot → Telegram."""
import requests, subprocess, time, json, os, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

NVR_IP = os.environ.get("NVR_IP", "192.168.1.180")
USER = os.environ.get("HIK_USER", "admin")
PASS = os.environ.get("HIK_PASS", "911HPExp")
PORT = int(os.environ.get("PORT", "8092"))
TG_TOKEN = os.environ.get("TG_TOKEN", "8532330447:AAHyf13dH1ySXqrLbLftrQNtgdS3XTjkYYc")
TG_CHAT = os.environ.get("TG_CHAT_ID", os.environ.get("TG_CHAT", "405695817"))
COOLDOWN = int(os.environ.get("COOLDOWN", "30"))

CAMERAS = {
    "1": "Двор", "2": "Дорога", "3": "Стройка", "4": "Детская площадка",
    "5": "Бассейн", "6": "Калитка", "7": "Веранда 2", "8": "Веранда 1",
}

# Only react to smart events (human/face), ignore VMD
SMART_EVENTS = {"fielddetection", "linedetection", "facedetection"}

stats = {"events": 0, "smart_events": 0, "vmd_skipped": 0, "snapshots": 0, "alerts": 0, "errors": 0}
last_alert = {}


def get_snapshot(channel_id):
    channel = int(channel_id) * 100 + 1
    tmp_path = f"/tmp/snap_{channel_id}.jpg"
    try:
        os.remove(tmp_path)
    except:
        pass
    try:
        subprocess.run([
            "ffmpeg", "-rtsp_transport", "tcp", "-loglevel", "quiet",
            "-i", f"rtsp://{USER}:{PASS}@{NVR_IP}:554/Streaming/Channels/{channel}",
            "-frames:v", "5", "-q:v", "2", tmp_path, "-y"
        ], capture_output=True, timeout=15)
    except Exception as e:
        print(f"  RTSP error ch{channel_id}: {e}")
    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 20000:
        with open(tmp_path, "rb") as f:
            stats["snapshots"] += 1
            return f.read()
    return None


def send_telegram(photo_bytes, caption):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                         data={"chat_id": TG_CHAT, "caption": caption},
                         files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
                         timeout=15)
        if r.status_code == 200:
            stats["alerts"] += 1
            print(f"  Telegram: sent ({len(photo_bytes)} bytes)")
        else:
            print(f"  Telegram error: {r.status_code}")
    except Exception as e:
        print(f"  Telegram error: {e}")
        stats["errors"] += 1


def process_event(channel_id, event_type):
    camera = CAMERAS.get(channel_id, f"Камера {channel_id}")
    now = time.time()
    if channel_id in last_alert and (now - last_alert[channel_id]) < COOLDOWN:
        return
    last_alert[channel_id] = now

    jpg = get_snapshot(channel_id)
    if not jpg:
        print(f"  [{camera}] snapshot failed")
        return
    print(f"  [{camera}] snapshot {len(jpg)} bytes")

    ts = time.strftime("%H:%M:%S")
    event_label = {"fielddetection": "Вторжение", "linedetection": "Пересечение линии", "facedetection": "Лицо"}.get(event_type, event_type)
    caption = f"🚨 {camera} [{ts}]\n{event_label}"
    send_telegram(jpg, caption)


def alert_stream_listener():
    url = f"http://{NVR_IP}/ISAPI/Event/notification/alertStream"
    auth = HTTPDigestAuth(USER, PASS)
    NS = "{http://www.hikvision.com/ver20/XMLSchema}"
    while True:
        try:
            print("Connecting to NVR alert stream...")
            r = requests.get(url, auth=auth, stream=True, timeout=(10, None))
            print("Connected to NVR alert stream")
            buf = ""
            for raw_chunk in r.iter_content(chunk_size=4096):
                chunk = raw_chunk.decode("utf-8", errors="replace") if isinstance(raw_chunk, bytes) else raw_chunk
                if chunk is None:
                    continue
                buf += chunk
                while "</EventNotificationAlert>" in buf:
                    end_idx = buf.find("</EventNotificationAlert>") + len("</EventNotificationAlert>")
                    block = buf[:end_idx]
                    buf = buf[end_idx:]
                    start = block.find("<EventNotificationAlert")
                    if start < 0:
                        continue
                    try:
                        root = ET.fromstring(block[start:])
                        event_type = root.findtext(f"{NS}eventType") or root.findtext("eventType", "")
                        event_state = root.findtext(f"{NS}eventState") or root.findtext("eventState", "")
                        channel_id = root.findtext(f"{NS}dynChannelID") or root.findtext(f"{NS}channelID") or "0"

                        if event_state != "active":
                            continue

                        stats["events"] += 1

                        if event_type in SMART_EVENTS:
                            camera = CAMERAS.get(channel_id, f"Камера {channel_id}")
                            stats["smart_events"] += 1
                            print(f"[{camera}] {event_type} (HUMAN)")
                            threading.Thread(target=process_event, args=(channel_id, event_type), daemon=True).start()
                        else:
                            stats["vmd_skipped"] += 1

                    except ET.ParseError:
                        pass
        except Exception as e:
            print(f"Alert stream error: {e}")
            stats["errors"] += 1
        print("Reconnecting in 5s...")
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "nvr": NVR_IP, "cameras": CAMERAS,
                              "stats": stats, "cooldown": COOLDOWN,
                              "mode": "smart_events_only"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/snapshot/"):
            ch = self.path.split("/snapshot/")[1]
            jpg = get_snapshot(ch)
            if jpg:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
            else:
                self.send_response(504)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Hik-watcher :{PORT}, NVR {NVR_IP}, {len(CAMERAS)} cameras")
    print(f"Mode: SMART events only (fielddetection, linedetection, facedetection)")
    print(f"VMD ignored — only human/face alerts → Telegram")
    print(f"Telegram: chat {TG_CHAT}, cooldown: {COOLDOWN}s")
    threading.Thread(target=alert_stream_listener, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
