#!/usr/bin/env python3
"""Minimal root setup, then run the app and Chromium without root privileges."""
import json
import os
from pathlib import Path
import sys
import urllib.request
from acceleration import drm_groups


def supervisor(path, token):
    request = urllib.request.Request("http://supervisor" + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get("result") != "ok":
        raise RuntimeError("Supervisor configuration is unavailable")
    return result["data"]


def main():
    os.umask(0o077)
    token = os.environ.pop("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("Start this app through Home Assistant Supervisor")
    info = supervisor("/addons/self/info", token)
    mqtt = supervisor("/services/mqtt", token)
    port = int(info["ingress_port"])
    if not 1024 <= port <= 65535:
        raise RuntimeError("Supervisor did not allocate an ingress port")
    options = json.loads(Path("/data/options.json").read_text())
    qualities = {
        "1080p_15": {"width": 1920, "height": 1080, "fps": 15, "bitrate": 6000, "hwaccel": "none"},
        "1080p_30": {"width": 1920, "height": 1080, "fps": 30, "bitrate": 8000, "hwaccel": "none"},
        "720p_30": {"width": 1280, "height": 720, "fps": 30, "bitrate": 4500, "hwaccel": "none"},
    }
    quality = qualities[options.get("quality", "1080p_30")]
    for path in (Path("/data/doubletake"), Path("/run/doubletake")):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        os.chown(path, 1000, 1000)
    bootstrap = Path("/run/doubletake/bootstrap.json")
    bootstrap.write_text(json.dumps({"port": port, "mqtt": mqtt, "quality": quality}))
    os.chown(bootstrap, 1000, 1000)
    token = ""
    os.setgroups(drm_groups())
    os.setgid(1000)
    os.setuid(1000)
    keep = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
    keep.update(HOME="/home/browser", DOUBLETAKE_BROWSER="/opt/browser-app/acceleration.py",
                DOUBLETAKE_BROWSER_CONTROL=options.get('browser_control', 'native'),
                DOUBLETAKE_AUDIO="true" if options.get("audio", True) else "false",
                DOUBLETAKE_HARDWARE_ENCODING="true" if options.get("hardware_encoding", True) else "false",
                DOUBLETAKE_DISPLAY_BACKEND=options.get('display_backend', 'auto'),
                DOUBLETAKE_HARDWARE_DECODING="true" if options.get("hardware_decoding", True) else "false")
    os.environ.clear()
    os.environ.update(keep)
    os.execv(sys.executable, [sys.executable, "-u", "-B", "/opt/browser-app/server.py"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never stringify Supervisor errors/options: they may hold secrets.
        print("Doubletake Browser could not load its Supervisor/MQTT configuration", file=sys.stderr)
        sys.exit(1)
