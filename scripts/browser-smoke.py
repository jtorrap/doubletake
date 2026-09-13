#!/usr/bin/env python3
"""Linux-only integration check: real browser, live JS/video, capture, AirPlay.

Only synthetic local content is used. No real Home Assistant server or TV is
contacted. Artifacts are suitable for CI upload because they contain no login.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/browser"
PAGE = b'''<!doctype html><meta charset="utf-8"><title>Doubletake Browser Test</title>
<style>body{margin:0;background:#101923;color:#edf6ff;font:24px sans-serif}main{padding:32px}h1{font-size:36px;margin:0 0 12px}p{color:#adbac8}section{display:flex;gap:30px}canvas,video{width:540px;height:304px;background:#192938;border-radius:12px}</style>
<main><h1>Headless browser / live capture</h1><p id="status">Connecting to live sensor stream</p>
<section><canvas id="chart" width="540" height="304"></canvas><video muted autoplay loop playsinline src="/clip.mp4"></video></section>
<p>Local test data: WebSocket chart and looping H.264 video</p></main>
<script>
const video=document.querySelector('video'), c=document.querySelector('canvas'), ctx=c.getContext('2d');
const hadProfile=!!localStorage.getItem('profileToken'), hadCookie=document.cookie.includes('fixture=retained');
const profileToken=localStorage.getItem('profileToken')||crypto.randomUUID();
localStorage.setItem('profileToken',profileToken);document.cookie='fixture=retained; Max-Age=3600; SameSite=Strict';
let updates=0, samples=[], last=0, moving=0;
const ws=new WebSocket('ws://'+location.host+'/events');
ws.onmessage=e=>{samples.push(JSON.parse(e.data).value); if(samples.length>60)samples.shift();updates++;};
function draw(){ctx.fillStyle='#192938';ctx.fillRect(0,0,540,304);ctx.strokeStyle='#263e53';ctx.lineWidth=1;
for(let y=30;y<300;y+=50){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(540,y);ctx.stroke();}
ctx.strokeStyle='#51e0b1';ctx.lineWidth=4;ctx.beginPath();samples.forEach((v,i)=>{let x=i*9,y=250-v*2;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();requestAnimationFrame(draw);}
draw(); setInterval(()=>{if(video.currentTime!==last)moving++;last=video.currentTime;
document.getElementById('status').textContent='WebSocket updates: '+updates+' / video time: '+last.toFixed(2)+'s';
fetch('/metrics',{method:'POST',body:JSON.stringify({updates,moving,videoTime:last,videoWidth:video.videoWidth,paused:video.paused,readyState:video.readyState,mediaError:video.error?.code,profileToken,hadProfile,hadCookie})});},500);
</script>'''


class Fixture(http.server.BaseHTTPRequestHandler):
    metrics = {}
    clip = b""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/events":
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            try:
                for i in range(600):
                    data = json.dumps({"value": 20 + (i * 7 % 80)}).encode()
                    self.wfile.write(b"\x81" + bytes([len(data)]) + data)
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body, content_type = (self.clip, "video/mp4") if self.path == "/clip.mp4" else (PAGE, "text/html")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Fixture.metrics = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204)
        self.end_headers()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError("timed out: " + description)


def stop(process):
    if process and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser-sandbox", action="store_true", help="CI-only browser sandbox exception for synthetic content")
    args = parser.parse_args()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    receiver = browser = None
    with tempfile.TemporaryDirectory(prefix="browser-smoke-") as directory:
        work = Path(directory)
        clip = work / "clip.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-t", "4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)], check=True)
        Fixture.clip = clip.read_bytes()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = free_port()
        status_path = work / "state/status.json"

        def status(phase="sender_started"):
            if browser and browser.poll() is not None:
                raise RuntimeError("browser launcher exited; see browser.log")
            try:
                data = json.loads(status_path.read_text())
                return data if data.get("phase") == phase else None
            except (OSError, ValueError):
                return None

        try:
            with (ARTIFACTS / "receiver.log").open("w") as receiver_log, (ARTIFACTS / "browser.log").open("w") as browser_log:
                receiver = subprocess.Popen([str(ROOT / "bin/doubletake-test-receiver"), "-listen", f"127.0.0.1:{port}", "-profile", "uxplay", "-stats-interval", "1s"], stdout=receiver_log, stderr=subprocess.STDOUT)
                wait_for(lambda: "listening" in (ARTIFACTS / "receiver.log").read_text(), 5, "test receiver")
                command = [sys.executable, "-B", str(ROOT / "scripts/doubletake-browser"), "--url", f"http://127.0.0.1:{server.server_port}/", "--state-dir", str(work / "state"), "--sender", str(ROOT / "bin/doubletake"), "--width", "1280", "--height", "720", "--fps", "15", "--hwaccel", "none"]
                if args.no_browser_sandbox:
                    command.append("--no-browser-sandbox")
                setup_port = free_port()
                browser = subprocess.Popen(command + ["--setup", "--setup-port", str(setup_port)], stdout=browser_log, stderr=subprocess.STDOUT)
                setup = wait_for(lambda: status("setup"), 50, "private setup browser")
                wait_for(lambda: Fixture.metrics.get("updates", 0) >= 20, 20, "setup browser storage")
                if setup["sender_pid"] is not None:
                    raise RuntimeError("setup mode started an AirPlay sender")
                with socket.create_connection(("127.0.0.1", setup_port), timeout=5) as vnc:
                    if not vnc.recv(12).startswith(b"RFB "):
                        raise RuntimeError("setup VNC server did not answer")
                token = Fixture.metrics["profileToken"]
                stop(browser)
                if browser.returncode != 0 or Path(setup["xauthority"]).exists():
                    raise RuntimeError(f"setup shutdown failed: exit={browser.returncode}, authority_exists={Path(setup['xauthority']).exists()}")
                Fixture.metrics = {}
                browser = subprocess.Popen(command + ["--target", "127.0.0.1", "--port", str(port)], stdout=browser_log, stderr=subprocess.STDOUT)
                active = wait_for(status, 50, "private browser window")
                wait_for(lambda: Fixture.metrics, 15, "streaming browser page")

                # Decode an independent sample from the exact selected X11
                # window, not a browser screenshot API or synthetic sender.
                capture_env = dict(os.environ, DISPLAY=active["display"], XAUTHORITY=active["xauthority"])
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab", "-window_id", str(active["window_id"]), "-framerate", "15", "-i", active["display"], "-t", "3", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(ARTIFACTS / "browser.mp4")], env=capture_env, check=True, timeout=20)
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(ARTIFACTS / "browser.mp4"), "-frames:v", "1", "-y", str(ARTIFACTS / "browser.png")], check=True, timeout=10)
                wait_for(lambda: Fixture.metrics.get("updates", 0) >= 30 and Fixture.metrics.get("moving", 0) >= 4 and Fixture.metrics.get("videoWidth") == 640, 30, "live WebSocket and decoded video")
                if Fixture.metrics["profileToken"] != token or not Fixture.metrics["hadProfile"] or not Fixture.metrics["hadCookie"]:
                    raise RuntimeError("setup browser storage did not survive into streaming mode")

                # Streaming must stop when the browser goes away. A stale
                # browser video must not continue indefinitely.
                os.kill(active["browser_pid"], signal.SIGTERM)
                browser.wait(timeout=25)
                if browser.returncode == 0:
                    raise RuntimeError("unexpected browser exit must fail the session for service recovery")
                stopped = json.loads(status_path.read_text())
                if stopped != {"phase": "stopped"}:
                    raise RuntimeError("launcher did not record stopped state")
                if Path(active["xauthority"]).exists():
                    raise RuntimeError("private Xauthority directory survived cleanup")
                for name in ["display_pid", "browser_pid", "sender_pid"]:
                    try:
                        os.kill(active[name], 0)
                    except ProcessLookupError:
                        continue
                    raise RuntimeError(f"managed {name} survived cleanup")
                if not (work / "state/profile").is_dir():
                    raise RuntimeError("browser profile was not preserved")
                stop(receiver)
                log = (ARTIFACTS / "receiver.log").read_text()
                frames = [int(value) for value in re.findall(r"video=(\d+)/", log)]
                if not frames or max(frames) < 30:
                    raise RuntimeError("test receiver did not receive sustained browser video")
                result = {"browser": Fixture.metrics, "airplay_video_packets": max(frames), "browser_failure_stops_session": True,
                          "profile_preserved": True, "setup_cookies_and_local_storage_retained": True,
                          "setup_vnc_answered": True, "private_display_cleaned": True,
                          "real_apple_tv_tested": False, "real_home_assistant_tested": False,
                          "browser_sandbox_disabled_for_fixture": args.no_browser_sandbox}
                (ARTIFACTS / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2))
        finally:
            (ARTIFACTS / "metrics.json").write_text(json.dumps(Fixture.metrics, indent=2) + "\n")
            stop(browser)
            stop(receiver)
            server.shutdown()


if __name__ == "__main__":
    main()
