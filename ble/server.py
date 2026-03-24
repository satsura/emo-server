#!/usr/bin/env python3
"""EMO BLE Bridge — HTTP API to control EMO robot via Bluetooth LE.

Features:
  - Auto-reconnect watchdog (checks every 30s)
  - Retry on BLE errors (up to 2 retries per request)
  - Disconnection detection via client.is_connected

Endpoints:
  GET  /health, /status, /status/full, /connect, /disconnect, /dances
  POST /dance, /stop_dance, /move, /animation, /volume, /power_off
  POST /photo, /face, /raw
"""

import asyncio
import json
import os
import logging
import socket
import base64
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from bleak import BleakScanner, BLEDevice, AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

logging.basicConfig(format="[BLE] %(asctime)s %(message)s", level=logging.INFO)
logger = logging.getLogger()

PORT = int(os.environ.get("PORT", "8091"))
PHOTO_PORT = int(os.environ.get("PHOTO_PORT", "8099"))
EMO_ADDR = os.environ.get("EMO_ADDR", "")
BLE_ADAPTER = os.environ.get("BLE_ADAPTER", "")
SERVER_IP = os.environ.get("SERVER_IP", "")
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

DANCE_LIST = [
    "d1_EmoDance", "d2_WontLetGo", "d3_Blindless", "d4_Click1",
    "d5_TimeOfMyLife", "d6_Rollercoaster", "d7_FlashBack", "d8_Click2",
    "d9_BlameYourself", "d10_CanITakeYouThere", "d11_OceanBlue",
]

SEQ = 1


def encode_text(payload: str) -> bytes:
    data = payload.encode("utf-8")
    return bytes([0xBB, 0xAA]) + len(data).to_bytes(2, "little") + data


def encode_cmd(data: list, sequential=True) -> bytes:
    global SEQ
    payload = bytes([0xDD, 0xCC, SEQ if sequential else 0]) + bytes(data) + bytes(17 - len(data))
    if sequential:
        SEQ = (SEQ % 254) + 1
    return payload


def detect_server_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.109"


class EmoConnection:
    def __init__(self):
        self.client = None
        self.char = None
        self.response = None
        self.response_event = asyncio.Event()
        self.connected = False
        self.loop = None
        self._buf = bytearray()
        self._expected = 0
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._last_ok = 0
        self._reconnect_count = 0

    @property
    def is_alive(self):
        return self.connected and self.client and self.client.is_connected

    def _handle_rx(self, _sender, data: bytearray):
        if data[0] == 0xBB and data[1] == 0xAA:
            self._expected = int.from_bytes(data[2:4], "little")
            self._buf = bytearray(data[4:])
        elif data[0] == 0xDD and data[1] == 0xCC:
            self.response = {"_bin": data.hex()}
            self.response_event.set()
            return
        else:
            self._buf += data

        if len(self._buf) >= self._expected and self._expected > 0:
            try:
                self.response = json.loads(self._buf[: self._expected].decode("utf-8"))
            except Exception:
                self.response = {"_raw": self._buf[: self._expected].hex()}
            self.response_event.set()
            self._buf = bytearray()
            self._expected = 0

    async def connect(self):
        async with self._connect_lock:
            if self.is_alive:
                return True

            # Clean up old connection
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                self.char = None
                self.connected = False

            logger.info("Scanning for EMO...")
            scan_kwargs = {"adapter": BLE_ADAPTER} if BLE_ADAPTER else {}

            try:
                if EMO_ADDR:
                    device = await BleakScanner.find_device_by_address(
                        EMO_ADDR, timeout=15, **scan_kwargs
                    )
                else:
                    def match(d: BLEDevice, adv: AdvertisementData):
                        return SERVICE_UUID.lower() in adv.service_uuids
                    device = await BleakScanner.find_device_by_filter(
                        match, timeout=15, **scan_kwargs
                    )
            except Exception as e:
                logger.error(f"Scan error: {e}")
                return False

            if not device:
                logger.error("EMO not found")
                return False

            logger.info(f"Found: {device.name} ({device.address})")
            try:
                connect_kwargs = {"adapter": BLE_ADAPTER} if BLE_ADAPTER else {}
                self.client = await establish_connection(
                    BleakClientWithServiceCache, device, "EMO", **connect_kwargs
                )
                svc = self.client.services.get_service(SERVICE_UUID)
                self.char = svc.get_characteristic(CHAR_UUID)
                await self.client.start_notify(self.char, self._handle_rx)
                self.connected = True
                self._last_ok = time.monotonic()
                self._reconnect_count += 1
                logger.info(f"Connected (attempt #{self._reconnect_count})")
                return True
            except Exception as e:
                logger.error(f"Connect error: {e}")
                self.connected = False
                return False

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.char = None
        self.connected = False

    async def ensure_connected(self):
        if not self.is_alive:
            logger.warning("Connection lost, reconnecting...")
            return await self.connect()
        return True

    async def _send_fragmented(self, payload: bytes):
        for i in range(0, len(payload), 20):
            await self.client.write_gatt_char(self.char, payload[i : i + 20], response=False)
            await asyncio.sleep(0.05)

    async def send_request(self, payload: bytes, timeout=5) -> dict:
        for attempt in range(MAX_RETRIES + 1):
            if not await self.ensure_connected():
                return {"error": "not connected"}
            try:
                async with self._lock:
                    self.response = None
                    self.response_event.clear()
                    await self._send_fragmented(payload)
                    try:
                        await asyncio.wait_for(self.response_event.wait(), timeout)
                    except asyncio.TimeoutError:
                        return {"error": "timeout"}
                    self._last_ok = time.monotonic()
                    return self.response or {"error": "no response"}
            except (BleakError, OSError, AttributeError) as e:
                logger.warning(f"Send error (attempt {attempt+1}/{MAX_RETRIES+1}): {e}")
                self.connected = False
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue
                return {"error": str(e)}

    async def send_command(self, payload: bytes):
        for attempt in range(MAX_RETRIES + 1):
            if not await self.ensure_connected():
                return {"error": "not connected"}
            try:
                await self.client.write_gatt_char(self.char, payload, response=False)
                self._last_ok = time.monotonic()
                return {"ok": True}
            except (BleakError, OSError, AttributeError) as e:
                logger.warning(f"Command error (attempt {attempt+1}): {e}")
                self.connected = False
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)

    # ── Watchdog ──────────────────────────────────────────────────

    async def watchdog(self):
        """Background task: check connection health every WATCHDOG_INTERVAL seconds."""
        logger.info(f"Watchdog started (interval={WATCHDOG_INTERVAL}s)")
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if not self.is_alive:
                logger.info("Watchdog: not connected, attempting reconnect...")
                ok = await self.connect()
                if ok:
                    logger.info("Watchdog: reconnected!")
                else:
                    logger.warning("Watchdog: reconnect failed, will retry")

    # ── High-level API ────────────────────────────────────────────

    async def get_status(self):
        return await self.send_request(
            encode_text('{"data":{"request":[0,1,2,7,8,11,12,13,14]},"type":"sta_req"}')
        )

    async def get_full_status(self):
        result = {}
        for i in range(15):
            r = await self.send_request(
                encode_text(json.dumps({"data": {"request": [i]}, "type": "sta_req"})), timeout=3
            )
            if r and "data" in r:
                result.update(r["data"])
            await asyncio.sleep(0.3)
        return {"type": "sta_rsp", "data": result}

    async def dance(self, num=0):
        num = max(0, min(num, len(DANCE_LIST) - 1))
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        return await self.send_request(
            encode_text(json.dumps({"data": {"name": DANCE_LIST[num], "op": "play"}, "type": "anim_req"}))
        )

    async def stop_dance(self):
        return await self.send_request(encode_text('{"data":{"op":"out"},"type":"anim_req"}'))

    async def play_animation(self, name: str):
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        return await self.send_request(
            encode_text(json.dumps({"data": {"name": name, "op": "play"}, "type": "anim_req"}))
        )

    async def move(self, direction: str):
        dirs = {"forward": 5, "back": 6, "left": 7, "right": 8, "stop": 9}
        await self.send_command(encode_cmd([3, 4, dirs.get(direction, 9)], sequential=False))
        return {"ok": True, "direction": direction}

    async def power_off(self):
        return await self.send_request(encode_text('{"data":{},"type":"off_req"}'))

    async def set_volume(self, level: int):
        return await self.send_request(
            encode_text(json.dumps({"data": {"volume": level}, "type": "setting_req"}))
        )

    async def face_op(self, data: dict):
        return await self.send_request(
            encode_text(json.dumps({"data": data, "type": "face_req"}))
        )

    async def take_photo(self) -> bytes:
        server_ip = SERVER_IP or detect_server_ip()
        photo_data = None
        photo_event = threading.Event()

        def tcp_receiver():
            nonlocal photo_data
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PHOTO_PORT))
            srv.listen(1)
            srv.settimeout(20)
            try:
                conn, addr = srv.accept()
                logger.info(f"Photo TCP from {addr}")
                header = b""
                while b"#" not in header:
                    chunk = conn.recv(1)
                    if not chunk:
                        break
                    header += chunk
                header_str = header.decode("utf-8", errors="replace").strip("#")
                parts = {}
                for p in header_str.split(";"):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        parts[k] = v
                filesize = int(parts.get("filesize", 0))
                logger.info(f"Photo: {parts.get('name')}, {filesize} bytes")
                data = b""
                while len(data) < filesize:
                    chunk = conn.recv(min(filesize - len(data), 8192))
                    if not chunk:
                        break
                    data += chunk
                conn.recv(1024)  # trailing delimiter
                conn.close()
                photo_data = data
                logger.info(f"Photo received: {len(data)} bytes")
            except socket.timeout:
                logger.warning("Photo TCP timeout")
            except Exception as e:
                logger.error(f"Photo TCP error: {e}")
            finally:
                srv.close()
                photo_event.set()

        tcp_thread = threading.Thread(target=tcp_receiver, daemon=True)
        tcp_thread.start()
        await asyncio.sleep(0.3)

        await self.send_request(encode_text('{"data":{"op":"in"},"type":"photo_req"}'), timeout=3)
        await asyncio.sleep(0.5)
        cmd = json.dumps({
            "type": "photo_req",
            "data": {"op": "syn", "server": {"ip": server_ip, "port": PHOTO_PORT}},
        })
        await self.send_request(encode_text(cmd), timeout=10)

        photo_event.wait(timeout=20)
        return photo_data

    async def set_eye(self, image_data: bytes, name: str = "eye.png", tran: int = 100) -> dict:
        """Send custom image to EMO screen via BLE + TCP."""
        server_ip = SERVER_IP or detect_server_ip()
        result = {"ok": False}
        done_event = threading.Event()

        def tcp_sender():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PHOTO_PORT))
            srv.listen(1)
            srv.settimeout(15)
            try:
                conn, addr = srv.accept()
                logger.info(f"set_eye TCP from {addr}")
                conn.settimeout(5)
                try:
                    req = conn.recv(4096)
                    logger.info(f"set_eye EMO request: {req}")
                except:
                    pass
                conn.sendall(image_data)
                logger.info(f"set_eye sent {len(image_data)} bytes")
                try:
                    resp = conn.recv(4096)
                    logger.info(f"set_eye EMO response: {resp}")
                    result["ok"] = resp == b"OK"
                    result["response"] = resp.decode("utf-8", errors="replace")
                except:
                    pass
                conn.close()
            except socket.timeout:
                logger.warning("set_eye TCP timeout — EMO did not connect")
                result["error"] = "tcp_timeout"
            except Exception as e:
                logger.error(f"set_eye TCP error: {e}")
                result["error"] = str(e)
            finally:
                srv.close()
                done_event.set()

        # 1. Start TCP server FIRST
        tcp_thread = threading.Thread(target=tcp_sender, daemon=True)
        tcp_thread.start()
        await asyncio.sleep(0.5)

        # 2. Enter customize mode
        await self.send_request(encode_text('{"type":"customize_req","data":{"op":"in"}}'), timeout=3)
        await asyncio.sleep(1)

        # 3. Send set_eye command
        cmd = json.dumps({
            "type": "customize_req",
            "data": {
                "image": {"length": len(image_data), "name": name, "tran": tran},
                "op": "set_eye",
                "server": {"ip": server_ip, "port": PHOTO_PORT},
            },
        })
        await self.send_request(encode_text(cmd), timeout=10)

        # 4. Wait for TCP transfer
        done_event.wait(timeout=20)

        # 5. Exit customize mode
        await asyncio.sleep(1)
        await self.send_request(encode_text('{"type":"customize_req","data":{"op":"out"}}'), timeout=3)

        return result


emo = EmoConnection()


def json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def binary_response(handler, status, data, content_type="image/jpeg"):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, emo.loop)
        return future.result(timeout=30)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                json_response(self, 200, {
                    "status": "ok",
                    "connected": emo.is_alive,
                    "emo_addr": EMO_ADDR,
                    "adapter": BLE_ADAPTER,
                    "reconnects": emo._reconnect_count,
                    "last_ok_ago": round(time.monotonic() - emo._last_ok, 1) if emo._last_ok else None,
                })
            elif path == "/status":
                json_response(self, 200, self._run(emo.get_status()))
            elif path == "/status/full":
                json_response(self, 200, self._run(emo.get_full_status()))
            elif path == "/dances":
                json_response(self, 200, {"dances": DANCE_LIST})
            elif path == "/connect":
                json_response(self, 200, {"connected": self._run(emo.connect())})
            elif path == "/disconnect":
                self._run(emo.disconnect())
                json_response(self, 200, {"disconnected": True})
            else:
                json_response(self, 404, {"error": "not found"})
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}
        path = urlparse(self.path).path

        try:
            if path == "/dance":
                json_response(self, 200, self._run(emo.dance(body.get("num", 0))))
            elif path == "/stop_dance":
                json_response(self, 200, self._run(emo.stop_dance()))
            elif path == "/move":
                json_response(self, 200, self._run(emo.move(body.get("direction", "stop"))))
            elif path == "/animation":
                json_response(self, 200, self._run(emo.play_animation(body.get("name", ""))))
            elif path == "/volume":
                json_response(self, 200, self._run(emo.set_volume(body.get("level", 3))))
            elif path == "/power_off":
                json_response(self, 200, self._run(emo.power_off()))
            elif path == "/face":
                json_response(self, 200, self._run(emo.face_op(body)))
            elif path == "/photo":
                photo = self._run(emo.take_photo())
                if photo:
                    binary_response(self, 200, photo)
                else:
                    json_response(self, 504, {"error": "photo capture failed"})
            elif path == "/set_eye":
                # Accept PNG/image as raw body or base64 in JSON
                ct = self.headers.get("Content-Type", "")
                if "image" in ct or "octet" in ct:
                    image_data = self.rfile.read(length)
                else:
                    image_data = base64.b64decode(body.get("image", ""))
                name = body.get("name", "eye.png") if isinstance(body, dict) else "eye.png"
                tran = body.get("tran", 100) if isinstance(body, dict) else 100
                result = self._run(emo.set_eye(image_data, name, tran))
                json_response(self, 200, result)
            elif path == "/raw":
                cmd = body.get("cmd", "")
                json_response(self, 200, self._run(emo.send_request(encode_text(cmd))))
            else:
                json_response(self, 404, {"error": "not found"})
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def log_message(self, fmt, *args):
        logger.info(fmt % args)


def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    emo.loop = loop
    threading.Thread(target=run_async_loop, args=(loop,), daemon=True).start()

    if not SERVER_IP:
        SERVER_IP = detect_server_ip()
        logger.info(f"Server IP: {SERVER_IP}")

    # Auto-connect
    try:
        future = asyncio.run_coroutine_threadsafe(emo.connect(), loop)
        future.result(timeout=20)
    except Exception as e:
        logger.warning(f"Auto-connect failed: {e}")

    # Start watchdog
    asyncio.run_coroutine_threadsafe(emo.watchdog(), loop)

    logger.info(f"EMO BLE Bridge on port {PORT} (watchdog every {WATCHDOG_INTERVAL}s)")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
