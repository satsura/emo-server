"""Hikvision NVR events + direct camera snapshots → n8n."""
import requests, subprocess, threading, time, json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

NVR_IP = os.environ.get("NVR_IP", "192.168.1.180")
USER = os.environ.get("HIK_USER", "admin")
PASS = os.environ.get("HIK_PASS", "911HPExp")
N8N_WEBHOOK = os.environ.get("N8N_WEBHOOK", "")
COOLDOWN = int(os.environ.get("COOLDOWN", "30"))
PORT = int(os.environ.get("PORT", "8092"))

# NVR channel → camera info
CAMERAS = {
    "1": {"name": "Веранда 2", "ip": "192.168.1.87"},
    "2": {"name": "Веранда 1", "ip": "192.168.1.136"},
    "3": {"name": "Двор", "ip": "192.168.1.145"},
    "4": {"name": "Стройка", "ip": "192.168.1.171"},
    "5": {"name": "Бассейн"},
    "6": {"name": "Детская площадка"},
    "7": {"name": "Дорога"},
    "8": {"name": "Калитка"},
}

auth = HTTPDigestAuth(USER, PASS)
last_alert = {}
stats = {"events": 0, "forwarded": 0, "snapshots": 0, "errors": 0}

def get_snapshot(channel_id):
    """Get HD snapshot via RTSP from NVR using ffmpeg."""
    import subprocess
    channel = int(channel_id) * 100 + 1
    tmp_path = f"/tmp/snap_{channel}.jpg"
    try:
        result = subprocess.run([
            "ffmpeg", "-rtsp_transport", "tcp", "-loglevel", "error",
            "-i", f"rtsp://{USER}:{PASS}@{NVR_IP}:554/Streaming/Channels/{channel}",
            "-frames:v", "1", "-q:v", "2",
            tmp_path, "-y"
        ], capture_output=True, timeout=10)
        if result.returncode == 0:
            with open(tmp_path, "rb") as f:
                data = f.read()
            if len(data) > 1000:
                stats["snapshots"] += 1
                return data
    except Exception as e:
        print(f"Snapshot error ch{channel_id}: {e}")
    return None

def watch_nvr():
    print(f"Watching NVR {NVR_IP}...")
    while True:
        try:
            r = requests.get(f"http://{NVR_IP}/ISAPI/Event/notification/alertStream",
                           auth=auth, stream=True, timeout=300)
            buffer = ""
            for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                if chunk:
                    buffer += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                    while "</EventNotificationAlert>" in buffer:
                        end = buffer.index("</EventNotificationAlert>") + len("</EventNotificationAlert>")
                        xml_str = buffer[:end]
                        buffer = buffer[end:]
                        try:
                            start = xml_str.index("<EventNotificationAlert")
                            root = ET.fromstring(xml_str[start:])
                            ns = {"h": "http://www.hikvision.com/ver20/XMLSchema"}
                            event_type = root.findtext("h:eventType", "", ns)
                            event_state = root.findtext("h:eventState", "", ns)
                            channel_id = root.findtext("h:channelID", "0", ns)
                            
                            if event_type in ("VMD", "linedetection", "fielddetection") and event_state == "active":
                                stats["events"] += 1
                                now = time.time()
                                key = f"ch{channel_id}"
                                if now - last_alert.get(key, 0) < COOLDOWN:
                                    continue
                                last_alert[key] = now
                                
                                cam = CAMERAS.get(channel_id, {"name": f"Камера {channel_id}"})
                                print(f"[{cam['name']}] Motion → n8n")
                                stats["forwarded"] += 1
                                
                                if N8N_WEBHOOK:
                                    try:
                                        requests.post(N8N_WEBHOOK, json={
                                            "event": "motion",
                                            "camera": cam["name"],
                                            "channel": channel_id,
                                            "event_type": event_type,
                                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        }, timeout=5)
                                    except:
                                        pass
                        except ET.ParseError:
                            pass
        except Exception as e:
            stats["errors"] += 1
            print(f"NVR error: {e}, reconnecting...")
            time.sleep(5)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path
        if path == "/health":
            body = json.dumps({
                "status": "ok", "nvr": NVR_IP,
                "cameras": {k: v["name"] for k, v in CAMERAS.items()},
                "stats": stats
            }).encode()
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
    def log_message(self, *a): pass

if __name__ == "__main__":
    # Verify cameras
    for cid, cam in CAMERAS.items():
        jpg = get_snapshot(cid)
        src = "direct" if cam.get("ip") else "NVR"
        print(f"  ch{cid} {cam['name']}: {'OK' if jpg else 'FAIL'} ({src}, {len(jpg) if jpg else 0}b)")
    
    threading.Thread(target=watch_nvr, daemon=True).start()
    print(f"Hik-watcher :{PORT}, NVR {NVR_IP}, {len(CAMERAS)} cameras")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
