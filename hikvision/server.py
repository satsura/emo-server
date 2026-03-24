"""Hikvision NVR event forwarder + RTSP snapshot endpoint."""
import requests, subprocess, time, json, os, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.auth import HTTPDigestAuth

NVR_IP = os.environ.get("NVR_IP", "192.168.1.180")
USER = os.environ.get("HIK_USER", "admin")
PASS = os.environ.get("HIK_PASS", "911HPExp")
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK", "")
PORT = int(os.environ.get("PORT", "8092"))

CAMERAS = {
    "1": "Веранда 2", "2": "Веранда 1", "3": "Двор", "4": "Стройка",
    "5": "Бассейн", "6": "Детская площадка", "7": "Дорога", "8": "Калитка",
}

stats = {"events": 0, "forwarded": 0, "snapshots": 0}

def get_snapshot(channel_id):
    channel = int(channel_id) * 100 + 1
    tmp_path = f"/tmp/snap_{channel}.jpg"
    try:
        os.remove(tmp_path)
    except:
        pass
    try:
        subprocess.run([
            "ffmpeg", "-rtsp_transport", "tcp", "-loglevel", "quiet",
            "-i", f"rtsp://{USER}:{PASS}@{NVR_IP}:554/Streaming/Channels/{channel}",
            "-t", "3", "-update", "1", "-q:v", "1",
            tmp_path, "-y"
        ], capture_output=True, timeout=15)
    except:
        pass
    try:
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            with open(tmp_path, "rb") as f:
                data = f.read()
            stats["snapshots"] += 1
            return data
    except:
        pass
    return None

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""

        if "/nvr-event" in path:
            try:
                import xml.etree.ElementTree as ET
                xml = body.decode("utf-8", errors="replace")
                start = xml.find("<EventNotificationAlert")
                if start >= 0:
                    root = ET.fromstring(xml[start:])
                    ns = {"h": "http://www.hikvision.com/ver20/XMLSchema"}
                    event_type = root.findtext("h:eventType", "", ns) or root.findtext("eventType", "")
                    event_state = root.findtext("h:eventState", "", ns) or root.findtext("eventState", "")
                    channel_id = root.findtext("h:channelID", "0", ns) or root.findtext("channelID", "0")

                    if event_state == "active" and event_type in ("VMD", "linedetection", "fielddetection"):
                        stats["events"] += 1
                        camera = CAMERAS.get(channel_id, f"Камера {channel_id}")
                        ts = time.strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{camera}] {event_type} → n8n")

                        if N8N_WEBHOOK:
                            stats["forwarded"] += 1
                            def forward(cam_name, ch_id, evt_type, evt_ts):
                                jpg = get_snapshot(ch_id)
                                if not jpg or len(jpg) < 1000:
                                    return

                                # Coral detect
                                labels = []
                                coral_url = os.environ.get("CORAL_URL", "http://127.0.0.1:8090/detect?lang=ru&threshold=0.3")
                                try:
                                    r = requests.post(coral_url, data=jpg,
                                        headers={"Content-Type": "application/octet-stream"}, timeout=10)
                                    objects = r.json().get("objects", [])
                                    labels = list(set(o["label"] for o in objects if o["score"] > 0.3))
                                except:
                                    pass

                                # Telegram — send photo + what Coral found
                                tg_token = os.environ.get("TG_TOKEN", "")
                                tg_chat = os.environ.get("TG_CHAT_ID", "")
                                if tg_token and tg_chat:
                                    try:
                                        caption = cam_name + " (" + evt_ts + "): " + (", ".join(labels) or "движение")
                                        url = "https://api.telegram.org/bot" + tg_token + "/sendPhoto"
                                        files = {"photo": ("cam.jpg", jpg, "image/jpeg")}
                                        data = {"chat_id": tg_chat, "caption": caption}
                                        requests.post(url, files=files, data=data, timeout=10)
                                        print(f"[TG] {caption}")
                                    except Exception as e:
                                        print(f"TG error: {e}")

                                # n8n notification (without photo, just results)
                                if N8N_WEBHOOK:
                                    try:
                                        requests.post(N8N_WEBHOOK, json={
                                            "event": "motion",
                                            "camera": cam_name,
                                            "channel": ch_id,
                                            "objects": labels,
                                            "timestamp": evt_ts,
                                        }, timeout=5)
                                    except:
                                        pass
                            threading.Thread(target=forward, args=(camera, channel_id, event_type, ts), daemon=True).start()
            except Exception as e:
                print(f"Event error: {e}")

            self.send_response(200)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        path = self.path
        if path == "/health":
            body = json.dumps({"status": "ok", "nvr": NVR_IP, "cameras": CAMERAS, "stats": stats}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/snapshot/"):
            ch = path.split("/snapshot/")[1]
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
    print(f"Events → {N8N_WEBHOOK}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
