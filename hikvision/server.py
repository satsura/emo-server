"""Hikvision NVR watcher — alert stream → snapshot → n8n."""
import requests, subprocess, time, json, os, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

NVR_IP = os.environ.get("NVR_IP", "192.168.1.180")
USER = os.environ.get("HIK_USER", "admin")
PASS = os.environ.get("HIK_PASS", "911HPExp")
PORT = int(os.environ.get("PORT", "8092"))
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK", "")
COOLDOWN = int(os.environ.get("COOLDOWN", "30"))

CAMERAS = {
    "1": "Двор", "2": "Дорога", "3": "Стройка", "4": "Детская площадка",
    "5": "Бассейн", "6": "Калитка", "7": "Веранда 2", "8": "Веранда 1",
}

stats = {"events": 0, "forwarded": 0, "snapshots": 0, "errors": 0}
last_alert = {}


def get_snapshot(channel_id):
    """Get HD snapshot via RTSP (ffmpeg)."""
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
            "-frames:v", "5", "-q:v", "2",
            tmp_path, "-y"
        ], capture_output=True, timeout=15)
    except Exception as e:
        print(f"  RTSP error ch{channel_id}: {e}")

    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 5000:
        with open(tmp_path, "rb") as f:
            data = f.read()
        stats["snapshots"] += 1
        return data

    # Fallback: ISAPI
    try:
        auth = HTTPDigestAuth(USER, PASS)
        r = requests.get(
            f"http://{NVR_IP}/ISAPI/Streaming/channels/{channel}/picture",
            auth=auth, timeout=10)
        if r.status_code == 200 and len(r.content) > 5000:
            stats["snapshots"] += 1
            return r.content
    except:
        pass
    return None


def forward_to_n8n(channel_id, event_type):
    """Take snapshot and send to n8n for processing."""
    camera = CAMERAS.get(channel_id, f"Камера {channel_id}")

    # Cooldown
    now = time.time()
    if channel_id in last_alert and (now - last_alert[channel_id]) < COOLDOWN:
        return
    last_alert[channel_id] = now

    # Snapshot
    jpg = get_snapshot(channel_id)
    if not jpg:
        print(f"  [{camera}] snapshot failed")
        return
    print(f"  [{camera}] snapshot {len(jpg)} bytes → n8n")

    # Send to n8n
    if not N8N_WEBHOOK:
        return
    try:
        b64 = base64.b64encode(jpg).decode()
        payload = {
            "event": "camera_motion",
            "camera": camera,
            "channel": channel_id,
            "event_type": event_type,
            "photo_base64": b64,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        r = requests.post(N8N_WEBHOOK, json=payload, timeout=10)
        stats["forwarded"] += 1
        print(f"  [{camera}] n8n: {r.status_code}")
    except Exception as e:
        print(f"  [{camera}] n8n error: {e}")
        stats["errors"] += 1


def alert_stream_listener():
    """Connect to NVR alert stream and process events."""
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
                    end_tag = "</EventNotificationAlert>"
                    end_idx = buf.find(end_tag) + len(end_tag)
                    block = buf[:end_idx]
                    buf = buf[end_idx:]
                    start = block.find("<EventNotificationAlert")
                    if start < 0:
                        continue
                    xml_str = block[start:]
                    try:
                        root = ET.fromstring(xml_str)
                        event_type = root.findtext(f"{NS}eventType") or root.findtext("eventType", "")
                        event_state = root.findtext(f"{NS}eventState") or root.findtext("eventState", "")
                        channel_id = root.findtext(f"{NS}dynChannelID") or root.findtext(f"{NS}channelID") or root.findtext("dynChannelID", "0")

                        if event_state == "active" and event_type in ("VMD", "linedetection", "fielddetection"):
                            camera = CAMERAS.get(channel_id, f"Камера {channel_id}")
                            stats["events"] += 1
                            print(f"[{camera}] {event_type}")
                            threading.Thread(target=forward_to_n8n, args=(channel_id, event_type), daemon=True).start()
                    except ET.ParseError as e:
                        print(f"XML parse error: {e}")
        except requests.exceptions.ConnectionError as e:
            print(f"NVR connection lost: {e}")
            stats["errors"] += 1
        except Exception as e:
            print(f"Alert stream error: {e}")
            stats["errors"] += 1
        print("Reconnecting in 5s...")
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "nvr": NVR_IP, "cameras": CAMERAS, "stats": stats}).encode()
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
    print(f"n8n webhook: {N8N_WEBHOOK}")
    print(f"Cooldown: {COOLDOWN}s per camera")
    threading.Thread(target=alert_stream_listener, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
