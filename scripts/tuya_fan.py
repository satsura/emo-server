#!/usr/bin/env python3
"""Tuya fan control via Cloudflare Worker proxy."""
import hashlib, hmac, time, json, sys, urllib.request

CF_PROXY = "https://tuya-proxy.cloudflare-account-9a6.workers.dev"
PROXY_KEY = "emo-2024-proxy"
ACCESS_ID = "wvnguucwtqtfqq8xt4ca"
ACCESS_SECRET = "b5c4f732abf74931a978dcc80e86edcf"

DEVICES = {
    "sanuzel": "bf70e18a13f7388b7fhqrn",
    "komnaty": "bf7258170edf574330p9bf",
    "kotelnaya": "bf6de0a21be4688a4a5cep",
}

def sign(msg):
    return hmac.new(ACCESS_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()

def api(method, path, body=None, token=None):
    t = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    content_hash = hashlib.sha256(body_str.encode()).hexdigest()
    if token:
        sign_str = f"{ACCESS_ID}{token}{t}{method}\n{content_hash}\n\n{path}"
    else:
        sign_str = ACCESS_ID + t
    s = sign(sign_str)
    headers = {"client_id": ACCESS_ID, "sign": s, "t": t, "sign_method": "HMAC-SHA256", "X-Proxy-Key": PROXY_KEY}
    if token:
        headers["access_token"] = token
    if body:
        headers["Content-Type"] = "application/json"
    url = CF_PROXY + path
    data = body_str.encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def get_token():
    result = api("GET", "/v1.0/token?grant_type=1")
    return result["result"]["access_token"]

def set_fan(device_name, on=True, speed=None):
    device_id = DEVICES.get(device_name)
    if not device_id:
        return False
    token = get_token()
    path = f"/v1.0/devices/{device_id}/commands"
    commands = [{"code": "switch", "value": on}]
    if on:
        commands.append({"code": "mode", "value": "auto"})
    result = api("POST", path, {"commands": commands}, token)
    print(f"{device_name} {'on' if on else 'off'}: {result.get('success')}")
    return result.get("success", False)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: tuya_fan.py <sanuzel|komnaty|kotelnaya> <on|off>")
        sys.exit(1)
    device = sys.argv[1]
    action = sys.argv[2]
    set_fan(device, on=(action == "on"))
