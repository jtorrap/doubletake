#!/usr/bin/env python3
"""One isolated browser and one AirPlay sender, controlled over private pipes.

stdout is a bounded JSON event channel, not a log. Never forward browser output
or raw AirPlay messages to it. This process has no MQTT/Supervisor credentials.
"""
import asyncio
import contextlib
import html
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time


def load_engine():
    loader = importlib.machinery.SourceFileLoader("browser_engine", os.environ.get("DOUBLETAKE_LAUNCHER", "/opt/doubletake/launcher.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


class CDP:
    def __init__(self, command_fd, response_fd):
        self.command_fd, self.response_fd = command_fd, response_fd
        self.loop = asyncio.get_running_loop()
        self.buffer = b""
        self.sequence = 100
        self.pending = {}
        self.loop.add_reader(response_fd, self.read)

    def read(self):
        try:
            chunk = os.read(self.response_fd, 65536)
            if not chunk:
                self.close()
                return
            self.buffer += chunk
            if len(self.buffer) > 2 * 1024 * 1024:
                self.close()
                return
            while b"\0" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\0", 1)
                value = json.loads(raw)
                future = self.pending.pop(value.get("id"), None)
                if future and not future.done():
                    if "error" in value:
                        future.set_exception(RuntimeError("Browser command failed"))
                    else:
                        future.set_result(value.get("result", {}))
        except (OSError, ValueError):
            self.close()

    async def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = self.loop.create_future()
        self.pending[self.sequence] = future
        try:
            os.write(self.command_fd, json.dumps(message).encode() + b"\0")
            return await asyncio.wait_for(future, 8)
        finally:
            self.pending.pop(message["id"], None)

    def close(self):
        self.loop.remove_reader(self.response_fd)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Browser connection ended"))
        self.pending.clear()


class Worker:
    def __init__(self, config):
        self.config = config
        self.engine = load_engine()
        self.browser = self.display = self.vnc = self.sender = None
        self.command_fd = self.response_fd = None
        self.cdp = None
        self.environment = None
        self.window = None
        self.page_session = None
        self.sender_buffer = ""
        self.stop_event = asyncio.Event()

    async def start(self, runtime):
        args = self.engine.arguments(["--url", self.config["url"], "--setup", "--state-dir", self.config["profile_dir"]])
        args.browser = self.config.get("browser")
        self.engine_config = vars(args).copy()
        self.engine_config.update(self.config["quality"])
        self.engine_config["state_dir"] = self.config["profile_dir"]
        self.engine_config["executables"] = self.engine.dependencies(args)
        self.engine_config["executables"]["sender"] = self.engine.executable(self.config.get("sender", "doubletake"))
        # No production sandbox-disable switch is accepted by this worker.
        self.engine_config["no_browser_sandbox"] = False
        Path(self.config["profile_dir"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.display, self.environment = self.engine.start_display(self.engine_config, runtime)
        bootstrap = Path(runtime) / "launch.html"
        bootstrap.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=' + html.escape(self.config["url"], quote=True) + '">')
        self.browser, self.command_fd, self.response_fd = self.engine.start_browser(self.engine_config, bootstrap, self.environment)
        self.cdp = CDP(self.command_fd, self.response_fd)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise RuntimeError("startup_cancelled")
            if self.browser.poll() is not None:
                raise RuntimeError("browser_start_failed")
            found = subprocess.run([self.engine_config["executables"]["xdotool"], "search", "--onlyvisible", "--class", "^DoubletakeBrowser$"], env=self.environment, capture_output=True, text=True, timeout=3)
            ids = found.stdout.split()
            if len(ids) == 1 and ids[0].isdigit():
                self.window = int(ids[0])
                break
            await asyncio.sleep(0.2)
        if not self.window:
            raise RuntimeError("browser_window_missing")
        for operation in [["windowsize", str(self.window), str(self.engine_config["width"]), str(self.engine_config["height"])], ["windowmove", str(self.window), "0", "0"]]:
            subprocess.run([self.engine_config["executables"]["xdotool"], *operation], env=self.environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        targets = await self.cdp.call("Target.getTargets")
        page = next(t for t in targets["targetInfos"] if t["type"] == "page")
        attached = await self.cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
        self.page_session = attached["sessionId"]
        # This file and the control channel remain private; no VNC password
        # appears in process arguments, logs, MQTT, or persistent settings.
        password = secrets.token_urlsafe(6)
        password_file = Path(runtime) / "vnc-password"
        password_file.write_text(password + "\n")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.vnc = subprocess.Popen([self.engine_config["executables"]["vnc"], "-display", self.environment["DISPLAY"], "-auth", self.environment["XAUTHORITY"], "-localhost", "-rfbport", str(port), "-passwdfile", str(password_file), "-forever", "-shared", "-noxdamage", "-quiet"], env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            if self.vnc.poll() is not None:
                raise RuntimeError("preview_start_failed")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                banner = await asyncio.wait_for(reader.readexactly(12), 2)
                writer.close()
                await writer.wait_closed()
                if banner.startswith(b"RFB "):
                    break
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("preview_start_failed")
        emit("ready", port=port, password=password)

    def stop_sender(self):
        if self.sender:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.sender.stdout.fileno())
            self.engine.stop(self.sender)
            self.sender.stdout.close()
            self.sender.stdin.close()
            self.sender = None
        self.sender_buffer = ""

    def sender_output(self):
        if not self.sender:
            return
        try:
            chunk = os.read(self.sender.stdout.fileno(), 8192)
        except OSError:
            return
        if not chunk:
            asyncio.get_running_loop().remove_reader(self.sender.stdout.fileno())
            return
        self.sender_buffer = (self.sender_buffer + chunk.decode("utf-8", errors="replace"))[-16384:]
        # Prompts have no newline. Recognize only known states; raw messages
        # can contain authentication material and must never reach the UI.
        if "Enter " in self.sender_buffer:
            emit("airplay", state="pairing")
            self.sender_buffer = ""
        elif "mirror session ready" in self.sender_buffer:
            emit("airplay", state="sending")
            self.sender_buffer = ""

    async def command(self, value):
        action = value["action"]
        if action == "navigate":
            self.engine.arguments(["--url", value["url"], "--setup"])
            result = await self.cdp.call("Page.navigate", {"url": value["url"]}, self.page_session)
            if result.get("errorText"):
                raise RuntimeError("page_load_failed")
        elif action in {"back", "forward"}:
            history = await self.cdp.call("Page.getNavigationHistory", session=self.page_session)
            index = history["currentIndex"] + (-1 if action == "back" else 1)
            if 0 <= index < len(history["entries"]):
                await self.cdp.call("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]}, self.page_session)
        elif action == "reload":
            await self.cdp.call("Page.reload", session=self.page_session)
        elif action == "cast":
            self.stop_sender()
            receiver = value["receiver"]
            config = {**self.engine_config, "target": receiver["host"], "port": receiver["port"], "state_dir": str(Path(self.config["receivers_dir"]) / receiver["id"]), "pair": False}
            Path(config["state_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
            self.sender = subprocess.Popen(self.engine.sender_command(config, self.window), env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            asyncio.get_running_loop().add_reader(self.sender.stdout.fileno(), self.sender_output)
        elif action == "pin":
            pin = value["value"]
            if not self.sender or self.sender.poll() is not None or not isinstance(pin, str) or not 1 <= len(pin) <= 128 or any(ord(c) < 32 for c in pin):
                raise RuntimeError("pairing_not_waiting")
            self.sender.stdin.write(pin.encode() + b"\n")
            self.sender.stdin.flush()
        elif action == "stop":
            self.stop_sender()
        elif action == "close":
            self.stop_event.set()
        else:
            raise RuntimeError("unknown_command")

    async def monitor(self):
        while not self.stop_event.is_set():
            if any(process.poll() is not None for process in [self.browser, self.display, self.vnc]):
                emit("fatal", code="browser_session_ended")
                self.stop_event.set()
                return
            if self.sender and self.sender.poll() is not None:
                self.stop_sender()
                emit("airplay", state="error", code="receiver_session_ended")
            await asyncio.sleep(0.25)

    async def commands(self):
        reader = asyncio.StreamReader(limit=16384)
        await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        while not self.stop_event.is_set():
            try:
                raw = await reader.readline()
                if not raw:
                    self.stop_event.set()
                    return
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, OSError):
                emit("fatal", code="invalid_control_message")
                self.stop_event.set()
                return
            try:
                await self.command(value)
                emit("reply", id=value["id"], ok=True)
            except Exception:
                emit("reply", id=value.get("id"), ok=False, code="browser_command_failed")

    async def close(self):
        self.stop_sender()
        self.engine.stop(self.vnc)
        if self.cdp:
            with contextlib.suppress(Exception):
                await self.cdp.call("Browser.close")
            self.cdp.close()
        self.engine.close_browser(self.browser, self.command_fd)
        for fd in [self.command_fd, self.response_fd]:
            if fd is not None:
                os.close(fd)
        self.engine.stop(self.display)


async def main():
    os.umask(0o077)
    config = json.loads(Path(sys.argv[1]).read_text())
    worker = Worker(config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, worker.stop_event.set)
    with tempfile.TemporaryDirectory(prefix="doubletake-app-") as runtime:
        tasks = []
        try:
            await worker.start(runtime)
            tasks = [asyncio.create_task(worker.commands()), asyncio.create_task(worker.monitor())]
            await worker.stop_event.wait()
        except Exception:
            emit("fatal", code="browser_start_failed")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
