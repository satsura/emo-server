"""Hikvision NVR watcher — 2-stage detection pipeline.
Stage 1: Local YOLOv8n (fast pre-filter)
Stage 2: n8n → Coral TPU (confirmation)
→ Telegram if confirmed
"""
import requests, subprocess, time, json, os, threading, base64
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

NVR_IP = os.environ.get("NVR_IP", "192.168.1.180")
USER = os.environ.get("HIK_USER", "admin")
PASS = os.environ.get("HIK_PASS", "911HPExp")
PORT = int(os.environ.get("PORT", "8092"))
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "8532330447:AAHyf13dH1ySXqrLbLftrQNtgdS3XTjkYYc")
TG_CHAT = os.environ.get("TG_CHAT_ID", os.environ.get("TG_CHAT", "405695817"))
NAS_URL = os.environ.get("NAS_URL", "https://192.168.1.53:5001")
NAS_USER = os.environ.get("NAS_USER", "valera")
NAS_PASS = os.environ.get("NAS_PASS", "vujwA0-pubhak-kybqus")
NAS_FOLDER = os.environ.get("NAS_FOLDER", "/home/cameras")
YADISK_TOKEN = os.environ.get("YADISK_TOKEN", "")
YADISK_FOLDER = os.environ.get("YADISK_FOLDER", "cameras")
COOLDOWN = int(os.environ.get("COOLDOWN", "10"))
YOLO_CONF = float(os.environ.get("YOLO_CONF", "0.3"))

CAMERAS = {
    "1": "Двор", "2": "Дорога", "3": "Стройка", "4": "Детская площадка",
    "5": "Бассейн", "6": "Калитка", "7": "Веранда 2", "8": "Веранда 1",
}

INTERESTING_CLASSES = {
    0: "человек", 1: "велосипед", 2: "машина", 3: "мотоцикл",
    5: "автобус", 7: "грузовик",
    14: "птица", 15: "кошка", 16: "собака", 17: "лошадь",
    18: "овца", 19: "корова", 21: "медведь",
}

stats = {"events": 0, "snapshots": 0,
         "yolo_detected": 0, "yolo_nothing": 0,
         "n8n_sent": 0, "n8n_confirmed": 0,
         "alerts": 0, "nas_saved": 0, "yadisk_saved": 0, "errors": 0}
last_event = {}

# ── YOLO ────────────────────────────────────────────────────────────────────

yolo_model = None

def load_yolo():
    global yolo_model
    try:
        from ultralytics import YOLO
        yolo_model = YOLO("yolov8n.pt")
        import numpy as np
        yolo_model.predict(np.zeros((640, 640, 3), dtype=np.uint8), verbose=False)
        print("YOLOv8n loaded")
    except Exception as e:
        print(f"YOLO load error: {e}")


def yolo_detect(jpg_data):
    if yolo_model is None:
        return []
    try:
        import numpy as np, cv2
        img = cv2.imdecode(np.frombuffer(jpg_data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []
        results = yolo_model.predict(img, conf=YOLO_CONF, verbose=False)
        detections = []
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                if cls_id in INTERESTING_CLASSES:
                    detections.append({
                        "label": INTERESTING_CLASSES[cls_id],
                        "class_id": cls_id,
                        "score": round(float(box.conf[0]), 3),
                    })
        return detections
    except Exception as e:
        print(f"  YOLO error: {e}")
        return []


# ── Snapshot ────────────────────────────────────────────────────────────────

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
    except:
        pass
    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 20000:
        with open(tmp_path, "rb") as f:
            stats["snapshots"] += 1
            return f.read()
    return None


# ── Telegram ────────────────────────────────────────────────────────────────

# ── NAS ──────────────────────────────────────────────────────────────────────

nas_sid = None
nas_sid_time = 0

def nas_login():
    global nas_sid, nas_sid_time
    if nas_sid and (time.time() - nas_sid_time) < 3600:
        return nas_sid
    try:
        r = requests.get(f"{NAS_URL}/webapi/entry.cgi",
            params={"api": "SYNO.API.Auth", "version": "6", "method": "login",
                    "account": NAS_USER, "passwd": NAS_PASS,
                    "format": "cookie", "session": "FileStation"},
            verify=False, timeout=10)
        data = r.json()
        if data.get("success"):
            nas_sid = r.cookies
            nas_sid_time = time.time()
            return nas_sid
    except Exception as e:
        print(f"  NAS login error: {e}")
    return None


def save_to_nas(camera, jpg_data):
    """Save snapshot to Synology NAS."""
    sid = nas_login()
    if not sid:
        return False
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ts}.jpg"
    folder = f"{NAS_FOLDER}/{camera}"
    try:
        r = requests.post(f"{NAS_URL}/webapi/entry.cgi",
            data={"api": "SYNO.FileStation.Upload", "version": "2", "method": "upload",
                  "path": folder, "create_parents": "true", "overwrite": "true"},
            files={"file": (filename, jpg_data, "image/jpeg")},
            cookies=sid,
            verify=False, timeout=15)
        if r.json().get("success"):
            stats["nas_saved"] += 1
            print(f"  NAS: {folder}/{filename}")
            return True
        else:
            print(f"  NAS upload error: {r.json()}")
    except Exception as e:
        print(f"  NAS error: {e}")
    return False


def save_to_yadisk(camera, jpg_data):
    """Save snapshot to Yandex Disk."""
    if not YADISK_TOKEN:
        return False
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    safe_cam = camera.replace(" ", "_")
    path = f"{YADISK_FOLDER}/{safe_cam}_{ts}.jpg"
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    try:
        requests.put(f"https://cloud-api.yandex.net/v1/disk/resources?path={YADISK_FOLDER}",
                    headers=headers, timeout=5)
        r = requests.get(f"https://cloud-api.yandex.net/v1/disk/resources/upload?path={path}&overwrite=true",
                        headers=headers, timeout=10)
        upload_url = r.json().get("href")
        if not upload_url:
            print(f"  YaDisk: no upload URL")
            return False
        r2 = requests.put(upload_url, data=jpg_data,
                         headers={"Content-Type": "image/jpeg"}, timeout=15)
        if r2.status_code in (201, 202):
            stats["yadisk_saved"] += 1
            print(f"  YaDisk: {path}")
            return True
        print(f"  YaDisk error: {r2.status_code}")
    except Exception as e:
        print(f"  YaDisk error: {e}")
    return False


def send_telegram(photo_bytes, caption):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                         data={"chat_id": TG_CHAT, "caption": caption},
                         files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
                         timeout=15)
        if r.status_code == 200:
            stats["alerts"] += 1
            print(f"  Telegram: sent")
    except Exception as e:
        print(f"  Telegram error: {e}")
        stats["errors"] += 1


# ── Process event ──────────────────────────────────────────────────────────

def process_event(channel_id, event_type):
    camera = CAMERAS.get(channel_id, f"ch{channel_id}")
    now = time.time()
    if channel_id in last_event and (now - last_event[channel_id]) < COOLDOWN:
        return
    last_event[channel_id] = now

    jpg = get_snapshot(channel_id)
    if not jpg:
        return

    # Save ALL snapshots to NAS
    threading.Thread(target=save_to_nas, args=(camera, jpg), daemon=True).start()

    # Stage 1: YOLO
    t0 = time.time()
    detections = yolo_detect(jpg)
    yolo_ms = int((time.time() - t0) * 1000)

    if not detections:
        stats["yolo_nothing"] += 1
        return

    labels = list(set(d["label"] for d in detections))
    stats["yolo_detected"] += 1
    print(f"  [{camera}] YOLO ({yolo_ms}ms): {labels}")

    # Only send to Coral/Telegram if person detected by YOLO
    has_person = "человек" in labels
    if not has_person:
        
        return

    # Stage 2: n8n → Coral (only for persons)
    if N8N_WEBHOOK:
        try:
            b64 = base64.b64encode(jpg).decode()
            r = requests.post(N8N_WEBHOOK, json={
                "event": "camera_detection",
                "camera": camera,
                "channel": channel_id,
                "yolo_labels": labels,
                "photo_base64": b64,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, timeout=30)
            stats["n8n_sent"] += 1
            result = r.json() if r.status_code == 200 else {}
            status = result.get("status", "")
            if status in ("sent", "send"):
                stats["n8n_confirmed"] += 1
                print(f"  [{camera}] Coral confirmed")
            else:
                print(f"  [{camera}] n8n: {status}")
        except Exception as e:
            print(f"  [{camera}] n8n error: {e}")

    else:
        pass



# ── Alert stream ───────────────────────────────────────────────────────────

def alert_stream_listener():
    url = f"http://{NVR_IP}/ISAPI/Event/notification/alertStream"
    auth = HTTPDigestAuth(USER, PASS)
    NS = "{http://www.hikvision.com/ver20/XMLSchema}"
    while True:
        try:
            print("Connecting to NVR alert stream...")
            r = requests.get(url, auth=auth, stream=True, timeout=(10, None))
            print("Connected")
            buf = ""
            for raw_chunk in r.iter_content(chunk_size=4096):
                chunk = raw_chunk.decode("utf-8", errors="replace") if isinstance(raw_chunk, bytes) else raw_chunk
                if not chunk:
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
                        etype = root.findtext(f"{NS}eventType") or root.findtext("eventType", "")
                        estate = root.findtext(f"{NS}eventState") or root.findtext("eventState", "")
                        chid = root.findtext(f"{NS}dynChannelID") or root.findtext(f"{NS}channelID") or "0"
                        if estate == "active" and etype in ("VMD", "fielddetection", "linedetection", "facedetection"):
                            stats["events"] += 1
                            camera = CAMERAS.get(chid, f"ch{chid}")
                            print(f"[{camera}] {etype}")
                            threading.Thread(target=process_event, args=(chid, etype), daemon=True).start()
                    except ET.ParseError:
                        pass
        except Exception as e:
            print(f"Stream error: {e}")
            stats["errors"] += 1
        print("Reconnecting in 5s...")
        time.sleep(5)


# ── HTTP API ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "nvr": NVR_IP, "cameras": CAMERAS,
                        "stats": stats, "cooldown": COOLDOWN,
                        "yolo": yolo_model is not None,
                        "n8n": bool(N8N_WEBHOOK), "yadisk": bool(YADISK_TOKEN)})
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

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Hik-watcher :{PORT}, NVR {NVR_IP}, {len(CAMERAS)} cameras")
    print(f"NAS: {NAS_URL} → {NAS_FOLDER}")
    print(f"Pipeline: event → YOLO (local) → n8n → Coral (confirm) → Telegram")
    print(f"Cooldown: {COOLDOWN}s per camera")
    print("Loading YOLOv8n...")
    load_yolo()
    threading.Thread(target=alert_stream_listener, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
