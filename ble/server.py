#!/usr/bin/env python3
"""EMO BLE Bridge — HTTP API to control EMO robot via Bluetooth LE."""

import asyncio
import json
import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from bleak import BleakClient, BleakScanner, BLEDevice, AdvertisementData
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

logging.basicConfig(format="[BLE] %(asctime)s %(message)s", level=logging.INFO)
logger = logging.getLogger()

PORT = int(os.environ.get("PORT", "8091"))
EMO_ADDR = os.environ.get("EMO_ADDR", "")

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


class EmoConnection:
    def __init__(self):
        self.client = None
        self.char = None
        self.response = None
        self.response_event = asyncio.Event()
        self.last_command = None
        self.connected = False
        self.loop = None

    def _handle_rx(self, _sender, data: bytearray):
        if data[0] == 0xBB and data[1] == 0xAA:
            size = int.from_bytes(data[2:4], "little")
            payload = data[4:]
            # Simple case: response fits in one packet
            if len(payload) >= size:
                try:
                    self.response = json.loads(payload[:size].decode("utf-8"))
                    self.response_event.set()
                except Exception as e:
                    logger.error(f"Parse error: {e}")
            else:
                self._buffer = payload
                self._expected = size
        elif hasattr(self, "_buffer"):
            self._buffer += data
            if len(self._buffer) >= self._expected:
                try:
                    self.response = json.loads(self._buffer[:self._expected].decode("utf-8"))
                    self.response_event.set()
                except Exception as e:
                    logger.error(f"Parse error: {e}")
                del self._buffer, self._expected
        elif data[0] == 0xDD and data[1] == 0xCC:
            self.last_command = data[3:]
            logger.info(f"Command from EMO: {data.hex()}")

    async def connect(self):
        if self.connected and self.client and self.client.is_connected:
            return True

        logger.info("Scanning for EMO...")
        if EMO_ADDR:
            device = await BleakScanner.find_device_by_address(EMO_ADDR, timeout=10)
        else:
            def match(d: BLEDevice, adv: AdvertisementData):
                return SERVICE_UUID.lower() in adv.service_uuids
            device = await BleakScanner.find_device_by_filter(match, timeout=10)

        if not device:
            logger.error("EMO not found")
            return False

        logger.info(f"Found: {device.name} ({device.address})")
        self.client = await establish_connection(BleakClientWithServiceCache, device, "EMO")
        await self.client.start_notify(CHAR_UUID, self._handle_rx)
        service = self.client.services.get_service(SERVICE_UUID)
        self.char = service.get_characteristic(CHAR_UUID)
        self.connected = True
        logger.info("Connected to EMO")
        return True

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.connected = False

    async def send_request(self, payload: bytes, timeout=5) -> dict:
        if not self.connected:
            await self.connect()
        self.response = None
        self.response_event.clear()
        await self.client.write_gatt_char(self.char, payload)
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout)
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        return self.response or {"error": "no response"}

    async def send_command(self, payload: bytes):
        if not self.connected:
            await self.connect()
        await self.client.write_gatt_char(self.char, payload)

    # High-level API
    async def get_status(self):
        req = encode_text('{"data":{"request":[0,1,2,7,8,11,12]},"type":"sta_req"}')
        return await self.send_request(req)

    async def dance(self, num=0):
        if num < 0 or num >= len(DANCE_LIST):
            num = 0
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        name = DANCE_LIST[num]
        resp = await self.send_request(
            encode_text(json.dumps({"data": {"name": name, "op": "play"}, "type": "anim_req"})))
        return resp

    async def stop_dance(self):
        return await self.send_request(encode_text('{"data":{"op":"out"},"type":"anim_req"}'))

    async def move(self, direction: str):
        dirs = {"forward": 5, "back": 6, "left": 7, "right": 8, "stop": 9}
        code = dirs.get(direction, 9)
        await self.send_command(encode_cmd([3, 4, code], sequential=False))
        return {"ok": True, "direction": direction}

    async def power_off(self):
        return await self.send_request(encode_text('{"data":{},"type":"off_req"}'))

    async def set_volume(self, level: int):
        req = encode_text(json.dumps({"data": {"volume": level}, "type": "setting_req"}))
        return await self.send_request(req)

    async def play_animation(self, name: str):
        await self.send_request(encode_text('{"data":{"op":"in"},"type":"anim_req"}'))
        await asyncio.sleep(0.3)
        resp = await self.send_request(
            encode_text(json.dumps({"data": {"name": name, "op": "play"}, "type": "anim_req"})))
        return resp


emo = EmoConnection()


def json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def _run_async(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, emo.loop)
        return future.result(timeout=15)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            json_response(self, 200, {
                "status": "ok",
                "connected": emo.connected,
                "emo_addr": EMO_ADDR,
            })
        elif path == "/status":
            try:
                result = self._run_async(emo.get_status())
                json_response(self, 200, result)
            except Exception as e:
                json_response(self, 500, {"error": str(e)})
        elif path == "/dances":
            json_response(self, 200, {"dances": DANCE_LIST})
        elif path == "/connect":
            try:
                ok = self._run_async(emo.connect())
                json_response(self, 200, {"connected": ok})
            except Exception as e:
                json_response(self, 500, {"error": str(e)})
        elif path == "/disconnect":
            try:
                self._run_async(emo.disconnect())
                json_response(self, 200, {"disconnected": True})
            except Exception as e:
                json_response(self, 500, {"error": str(e)})
        else:
            json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}
        path = urlparse(self.path).path

        try:
            if path == "/dance":
                num = body.get("num", 0)
                result = self._run_async(emo.dance(num))
                json_response(self, 200, result)
            elif path == "/stop_dance":
                result = self._run_async(emo.stop_dance())
                json_response(self, 200, result)
            elif path == "/move":
                direction = body.get("direction", "stop")
                result = self._run_async(emo.move(direction))
                json_response(self, 200, result)
            elif path == "/volume":
                level = body.get("level", 3)
                result = self._run_async(emo.set_volume(level))
                json_response(self, 200, result)
            elif path == "/animation":
                name = body.get("name", "")
                result = self._run_async(emo.play_animation(name))
                json_response(self, 200, result)
            elif path == "/power_off":
                result = self._run_async(emo.power_off())
                json_response(self, 200, result)
            elif path == "/raw":
                # Send raw JSON command
                cmd = body.get("cmd", "")
                result = self._run_async(emo.send_request(encode_text(cmd)))
                json_response(self, 200, result)
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
    # Start async loop in background thread
    loop = asyncio.new_event_loop()
    emo.loop = loop
    thread = threading.Thread(target=run_async_loop, args=(loop,), daemon=True)
    thread.start()

    # Auto-connect on startup
    try:
        future = asyncio.run_coroutine_threadsafe(emo.connect(), loop)
        future.result(timeout=15)
    except Exception as e:
        logger.warning(f"Auto-connect failed: {e}. Use GET /connect to retry.")

    logger.info(f"EMO BLE Bridge starting on port {PORT}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
