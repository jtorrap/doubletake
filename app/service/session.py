"""Supervise the single browser worker and expose safe runtime state."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
from model import atomic_json, browser_text


class Session:
    def __init__(self, directory, quality, notify=lambda: None):
        self.directory = Path(directory)
        self.quality, self.notify = quality, notify
        self.process = self.reader_task = self.ready = None
        self.preview = None
        self.sequence = 0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.runtime = {"browser": "closed", "airplay": "idle", "page_id": None, "tv_id": None, "error": None}

    def state(self):
        return dict(self.runtime)

    def update(self, **fields):
        self.runtime.update(fields)
        self.notify()

    async def read_events(self, process):
        try:
            while raw := await process.stdout.readline():
                value = json.loads(raw)
                if value.get("type") == "ready":
                    self.preview = {"port": int(value["port"]), "password": value["password"]}
                    self.update(browser="ready", error=None)
                    if not self.ready.done():
                        self.ready.set_result(True)
                elif value.get("type") == "reply":
                    future = self.pending.pop(value.get("id"), None)
                    if future and not future.done():
                        if value.get("ok"):
                            future.set_result(value.get("data"))
                        else:
                            future.set_exception(ValueError("The browser could not complete that action"))
                elif value.get("type") == "airplay":
                    if value.get("state") in {"pairing", "sending", "error"}:
                        self.update(airplay=value["state"], error="The TV connection ended. Try Show again." if value["state"] == "error" else None)
                elif value.get("type") == "fatal":
                    stage = value.get("stage")
                    detail = " (" + stage + ")" if stage in {"dependencies", "display", "browser", "browser control", "preview"} else ""
                    self.update(browser="error", airplay="error" if self.runtime["tv_id"] else "idle", error="The browser session could not run" + detail + ". Check app health and browser sandbox support.")
                    if not self.ready.done():
                        self.ready.set_exception(ValueError("The browser could not start"))
        except (OSError, ValueError, KeyError, TypeError):
            self.update(browser="error", error="The browser control connection ended")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ValueError("The browser session ended"))
            self.pending.clear()
            if self.ready and not self.ready.done():
                self.ready.set_exception(ValueError("The browser could not start"))
            if self.process is process and self.runtime["browser"] != "closed":
                self.preview = None
                self.update(browser="error", airplay="error" if self.runtime["tv_id"] else "idle")

    async def ensure(self, page):
        if self.process and self.process.returncode is None and self.preview:
            return False
        await self.close_worker()
        self.update(browser="starting", error=None)
        config = {"url": page["url"], "quality": self.quality,
                  "profile_dir": str(self.directory / "browser"), "receivers_dir": str(self.directory / "receivers")}
        if os.environ.get("DOUBLETAKE_BROWSER"):
            config["browser"] = os.environ["DOUBLETAKE_BROWSER"]
        if os.environ.get("DOUBLETAKE_SENDER"):
            config["sender"] = os.environ["DOUBLETAKE_SENDER"]
        path = self.directory / "worker.json"
        atomic_json(path, config)
        # Explicit allowlist: credentials for Supervisor/MQTT never cross into
        # the page-rendering process or its browser/encoder children.
        env = {key: os.environ[key] for key in ["PATH", "LANG", "LC_ALL", "HOME", "DOUBLETAKE_LAUNCHER", "DOUBLETAKE_HARDWARE_DECODING"] if key in os.environ}
        self.ready = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-B", str(Path(__file__).with_name("worker.py")), str(path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        self.reader_task = asyncio.create_task(self.read_events(self.process))
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), 50)
        except (ValueError, asyncio.TimeoutError):
            await self.close_worker()
            self.update(browser="error", error=self.runtime["error"] or "The browser could not start. Check app health and sandbox support.")
            raise ValueError("The browser could not start") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return True

    async def request(self, action, **fields):
        if not self.process or self.process.returncode is not None:
            raise ValueError("Open a page first")
        self.sequence += 1
        message_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        try:
            self.process.stdin.write(json.dumps({"id": message_id, "action": action, **fields}).encode() + b"\n")
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, 30 if action == "diagnostics" else 15)
        except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
            raise ValueError("The browser did not respond") from None
        finally:
            self.pending.pop(message_id, None)

    async def open(self, page, receiver=None, *, preserve_view=False):
        async with self.lock:
            created = await self.ensure(page)
            # The UI's Show action sends the view the user is interacting with.
            # MQTT launch buttons always open their configured URL explicitly.
            if not created and not (preserve_view and self.runtime["page_id"] == page["id"]):
                await self.request("navigate", url=page["url"])
            self.update(page_id=page["id"], error=None)
            if receiver and (self.runtime["tv_id"] != receiver["id"] or self.runtime["airplay"] not in {"starting", "pairing", "sending"}):
                # One sender is replaced only after the prior one stops.
                self.update(tv_id=receiver["id"], airplay="starting")
                await self.request("cast", receiver=receiver)

    async def stop(self, tv_id=None):
        async with self.lock:
            # A Stop button for an inactive TV must not stop another TV.
            if tv_id is not None and tv_id != self.runtime["tv_id"]:
                return
            if self.process and self.process.returncode is None:
                await self.request("stop")
            self.update(tv_id=None, airplay="idle", error=None)

    async def browser_action(self, action):
        async with self.lock:
            await self.request(action)

    async def insert_text(self, value):
        browser_text(value)
        async with self.lock:
            if self.runtime["browser"] != "ready":
                raise ValueError("Open a page first")
            await self.request("insert_text", value=value)

    async def pin(self, value):
        async with self.lock:
            if self.runtime["airplay"] != "pairing":
                raise ValueError("The TV is not waiting for a pairing code")
            await self.request("pin", value=value)
            self.update(airplay="starting", error=None)

    async def close_worker(self):
        process = self.process
        if process:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 45)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self.process = None
            if self.reader_task:
                await self.reader_task
        self.preview = None

    async def close(self):
        async with self.lock:
            await self.close_worker()
            self.update(browser="closed", airplay="idle", page_id=None, tv_id=None, error=None)
