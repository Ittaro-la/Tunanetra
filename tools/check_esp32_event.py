#!/usr/bin/env python3
"""Pemicu deteksi manual tanpa perangkat keras (uji backend -> ESP32-CAM -> AI -> suara).

Pada pemakaian normal tidak ada tombol deteksi: objek dipicu sensor ultrasonik dan
rambu berjalan terus-menerus. Skrip ini hanya untuk menguji jalurnya.

    python tools/check_esp32_event.py --status
    python tools/check_esp32_event.py --event object_detection
    python tools/check_esp32_event.py --event sign_detection --host 192.168.1.100
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def call(url, payload=None, token=""):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-ESP32-Token"] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"message": body}
    except urllib.error.URLError as exc:
        raise SystemExit(f"Tidak bisa menghubungi server: {exc.reason}. Pastikan main.py berjalan.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--token", default="", help="ESP32_SHARED_TOKEN bila dipakai")
    parser.add_argument("--device-id", default="esp32-A1")
    parser.add_argument("--event", choices=["object_detection", "sign_detection"])
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    base = f"http://{args.host}:{args.port}"
    if args.status or not args.event:
        _, data = call(f"{base}/api/esp32/status", token=args.token)
        print(f"Layanan deteksi terpasang: {data.get('trigger_ready')}")
        for item in data.get("mapping", []):
            print(f"  {item['event']:<18} -> {item['label']}")
        return 0
    status, data = call(f"{base}/api/esp32/event",
                        {"event": args.event, "device_id": args.device_id, "source": "test-tool"}, args.token)
    print(status, json.dumps(data, ensure_ascii=False))
    return 0 if data.get("status") == "started" else 1


if __name__ == "__main__":
    raise SystemExit(main())
