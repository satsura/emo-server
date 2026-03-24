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
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
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
    import subprocess, os
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
            "-t", "2", "-update", "1", "-q:v", "1",
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

def poll_cameras():
    """Poll cameras every COOLDOWN seconds, detect persons, forward to n8n."""
    import subprocess, os
    print(f"Polling {len(CAMERAS)} cameras every {COOLDOWN}s...")
    last_persons = {}  # channel → set of labels
    
    while True:
        for cid, cam in CAMERAS.items():
            try:
                jpg = get_snapshot(cid)
                if not jpg or len(jpg) < 2000:
                    continue
                
                # Coral detect
                coral_url = os.environ.get("CORAL_URL", "http://127.0.0.1:8090/detect?lang=ru&threshold=0.4")
                r = requests.post(coral_url, data=jpg,
                                 headers={"Content-Type": "application/octet-stream"}, timeout=10)
                result = r.json()
                objects = result.get("objects", [])
                labels = set(o["label"] for o in objects if o["score"] > 0.4)
                has_person = any("человек" in l for l in labels)
                
                prev = last_persons.get(cid, set())
                
                # Alert only if NEW person appeared (not already seen)
                if has_person and "человек" not in prev:
                    stats["events"] += 1
                    ts = time.strftime("%H:%M:%S")
                    name = cam["name"]
                    caption = f"На камере {name} обнаружен человек ({ts}). Объекты: {', '.join(labels)}"
                    print(f"[ALERT] {caption}")
                    stats["forwarded"] += 1
                    
                    # Telegram
                    tg_token = os.environ.get("TG_TOKEN", "")
                    tg_chat = os.environ.get("TG_CHAT_ID", "")
                    if tg_token and tg_chat:
                        try:
                            url = f"https://api.telegram.org/bot{tg_token}/sendPhoto"
                            files = {"photo": ("alert.jpg", jpg, "image/jpeg")}
                            data = {"chat_id": tg_chat, "caption": caption}
                            requests.post(url, files=files, data=data, timeout=10)
                        except Exception as e:
                            print(f"TG error: {e}")
                    
                    # n8n
                    if N8N_WEBHOOK:
                        try:
                            requests.post(N8N_WEBHOOK, json={
                                "event": "motion",
                                "camera": name,
                                "channel": cid,
                                "objects": list(labels),
                                "timestamp": ts,
                            }, timeout=5)
                        except:
                            pass
                
                last_persons[cid] = labels
            except Exception as e:
                stats["errors"] += 1
        
        time.sleep(COOLDOWN)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = self.path if hasattr(self, 'path') else ''
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b''
        
        if '/nvr-event' in path:
            # Parse Hikvision event XML
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(body.decode('utf-8', errors='replace'))
                ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
                event_type = root.findtext('.//h:eventType', '', ns) or root.findtext('.//eventType', '')
                event_state = root.findtext('.//h:eventState', '', ns) or root.findtext('.//eventState', '')
                channel_id = root.findtext('.//h:channelID', '0', ns) or root.findtext('.//channelID', '0')
                
                if event_type in ('VMD', 'linedetection', 'fielddetection') and event_state == 'active':
                    now = time.time()
                    key = f'ch{channel_id}'
                    if now - last_alert.get(key, 0) >= COOLDOWN:
                        last_alert[key] = now
                        cam = CAMERAS.get(channel_id, {'name': f'Камера {channel_id}'})
                        name = cam['name']
                        stats['events'] += 1
                        print(f'[{name}] Motion event! Snapshot + Coral...')
                        
                        # Snapshot + Coral in background
                        import threading
                        def process_event(cid, cname):
                            jpg = get_snapshot(cid)
                            if not jpg or len(jpg) < 2000:
                                return
                            try:
                                coral_url = os.environ.get('CORAL_URL', 'http://127.0.0.1:8090/detect?lang=ru&threshold=0.4')
                                r = requests.post(coral_url, data=jpg,
                                                 headers={'Content-Type': 'application/octet-stream'}, timeout=10)
                                result = r.json()
                                objects = result.get('objects', [])
                                labels = list(set(o['label'] for o in objects if o['score'] > 0.4))
                                has_person = any('человек' in l for l in labels)
                                
                                if has_person:
                                    stats['forwarded'] += 1
                                    ts = time.strftime('%H:%M:%S')
                                    caption = 'На камере ' + cname + ' обнаружен человек (' + ts + '). Объекты: ' + ', '.join(labels)
                                    print(f'[ALERT] {caption}')
                                    
                                    if TG_TOKEN and TG_CHAT_ID:
                                        url = f'https://api.telegram.org/bot{TG_TOKEN}/sendPhoto'
                                        files = {'photo': ('alert.jpg', jpg, 'image/jpeg')}
                                        data = {'chat_id': TG_CHAT_ID, 'caption': caption}
                                        requests.post(url, files=files, data=data, timeout=10)
                                    
                                    if N8N_WEBHOOK:
                                        requests.post(N8N_WEBHOOK, json={
                                            'event': 'motion', 'camera': cname,
                                            'channel': cid, 'objects': labels,
                                            'timestamp': ts,
                                        }, timeout=5)
                                else:
                                    print(f'[{cname}] Coral: {labels} (no person)')
                            except Exception as e:
                                print(f'Process error: {e}')
                        
                        threading.Thread(target=process_event, args=(channel_id, name), daemon=True).start()
            except Exception as e:
                print(f'Event parse error: {e}')
            
            self.send_response(200)
            self.end_headers()
            return
        
        self.send_response(404)
        self.end_headers()

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
    
    print("Waiting for NVR HTTP push events on /nvr-event...")
    print(f"Hik-watcher :{PORT}, NVR {NVR_IP}, {len(CAMERAS)} cameras")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
