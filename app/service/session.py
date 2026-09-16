"""Supervise the single browser worker and expose safe runtime state."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from model import atomic_json, browser_text
from performance import video_stats


class Session:
    def __init__(self, directory, quality, notify=lambda: None):
        self.directory = Path(directory)
        self.quality, self.notify = quality, notify
        self.process = self.reader_task = self.ready = None
        self.preview = None
        self.sequence = 0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.control_mode = os.environ.get('DOUBLETAKE_BROWSER_CONTROL', 'native')
        if self.control_mode not in {'native', 'diagnostic'}:
            raise ValueError('Unknown browser control mode')
        self.audio_enabled = os.environ.get('DOUBLETAKE_AUDIO', 'true') == 'true'
        self.runtime = {"browser": "closed", "airplay": "idle", "page_id": None, "tv_id": None, "receivers": {}, "error": None}

    def state(self):
        return {**self.runtime, 'receivers': {key: {**value, 'performance_age_seconds': round(time.monotonic() - value.get('performance_at', time.monotonic()), 1)} for key, value in self.runtime['receivers'].items()},
                'control_mode': self.control_mode,
                'display': {key: self.quality.get(key, default) for key, default in [('width', 1920), ('height', 1080), ('fps', 30)]},
                'audio_enabled': self.audio_enabled}

    def update(self, **fields):
        self.runtime.update(fields)
        self.notify()

    def set_receivers(self, receivers):
        # Keep the original single-TV fields for older API consumers. The map
        # is authoritative; one failed TV must not hide another sending TV.
        states = {value['state'] for value in receivers.values()}
        airplay = next((state for state in ('sending', 'pairing', 'starting', 'error') if state in states), 'idle')
        tv_id = next((key for key, value in receivers.items() if value['state'] == airplay), None)
        self.update(receivers=receivers, tv_id=tv_id, airplay=airplay)

    def receiver_event(self, value):
        tv_id, state = value.get('tv_id'), value.get('state')
        # The worker always identifies the TV. Ignore late events after Stop
        # and never apply an unidentified event to an arbitrary receiver.
        if tv_id not in self.runtime['receivers'] or state not in {'starting', 'pairing', 'sending', 'error'}:
            return
        receivers = dict(self.runtime['receivers'])
        receivers[tv_id] = {**receivers[tv_id], 'state': state, 'error': 'The TV connection ended. Try Show again.' if state == 'error' else None}
        if state == 'error' and self.audio_enabled:
            receivers[tv_id]['audio'] = 'error'
        self.set_receivers(receivers)

    def audio_event(self, value):
        tv_id, state = value.get('tv_id'), value.get('state')
        if tv_id not in self.runtime['receivers'] or state not in {'starting', 'active', 'unavailable', 'error', 'disabled'}:
            return
        self.set_receivers({**self.runtime['receivers'], tv_id: {**self.runtime['receivers'][tv_id], 'audio': state}})

    def receivers_failed(self):
        self.set_receivers({tv_id: {'state': 'error', 'error': 'The browser session ended.', 'audio': 'error' if self.audio_enabled else 'disabled'} for tv_id in self.runtime['receivers']})

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
                    self.receiver_event(value)
                elif value.get('type') == 'audio':
                    self.audio_event(value)
                elif value.get('type') == 'performance':
                    stats = video_stats(value.get('data'))
                    receiver = self.runtime['receivers'].get(value.get('tv_id'))
                    if receiver is not None and stats and value.get('encoder') in {'vaapi', 'none'}:
                        # Polling clients receive metrics without republishing
                        # all MQTT discovery/state for every five-second sample.
                        receiver.update(performance=stats, performance_at=time.monotonic(), encoder=value['encoder'])
                elif value.get("type") == "fatal":
                    stage = value.get("stage")
                    detail = " (" + stage + ")" if stage in {"dependencies", "display", "audio", "browser", "browser control", "preview"} else ""
                    self.receivers_failed()
                    self.update(browser="error", error="The browser session could not run" + detail + ". Check app health and browser sandbox support.")
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
                self.receivers_failed()
                self.update(browser="error")

    async def ensure(self, page):
        if self.process and self.process.returncode is None and self.preview:
            return False
        await self.close_worker()
        self.set_receivers({})
        self.update(browser="starting", error=None)
        config = {"url": page["url"], "quality": self.quality, "control_mode": self.control_mode,
                  "profile_dir": str(self.directory / "browser"), "receivers_dir": str(self.directory / "receivers")}
        if os.environ.get("DOUBLETAKE_BROWSER"):
            config["browser"] = os.environ["DOUBLETAKE_BROWSER"]
        if os.environ.get("DOUBLETAKE_SENDER"):
            config["sender"] = os.environ["DOUBLETAKE_SENDER"]
        path = self.directory / "worker.json"
        atomic_json(path, config)
        # Explicit allowlist: credentials for Supervisor/MQTT never cross into
        # the page-rendering process or its browser/encoder children.
        env = {key: os.environ[key] for key in ["PATH", "LANG", "LC_ALL", "HOME", "DOUBLETAKE_LAUNCHER", "DOUBLETAKE_HARDWARE_DECODING", "DOUBLETAKE_HARDWARE_ENCODING", "DOUBLETAKE_AUDIO"] if key in os.environ}
        self.ready = asyncio.get_running_loop().create_future()
        command = [sys.executable, "-u", "-B", str(Path(__file__).with_name("worker.py")), str(path)]
        self.process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
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
            return await asyncio.wait_for(future, 40)
        except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
            raise ValueError("The browser did not respond") from None
        finally:
            self.pending.pop(message_id, None)

    async def open(self, page, receiver=None, *, preserve_view=False):
        async with self.lock:
            await self.open_page(page, preserve_view=preserve_view)
            if receiver:
                await self.add_receiver(receiver)

    async def open_page(self, page, *, preserve_view):
        """Open the one shared page; caller holds the session lock."""
        created = await self.ensure(page)
        if not created and not (preserve_view and self.runtime['page_id'] == page['id']):
            await self.request('navigate', url=page['url'])
        self.update(page_id=page['id'], error=None)

    async def add_receiver(self, receiver):
        """Add or retry a receiver without interrupting its peers."""
        tv_id = receiver['id']
        current = self.runtime['receivers'].get(tv_id, {})
        if current.get('state') in {'starting', 'pairing', 'sending'}:
            return
        self.set_receivers({**self.runtime['receivers'], tv_id: {'state': 'starting', 'error': None, 'audio': 'starting' if self.audio_enabled else 'disabled'}})
        try:
            await self.request('cast', receiver=receiver)
        except (ValueError, OSError):
            self.receiver_event({'tv_id': tv_id, 'state': 'error'})
            raise

    async def cast(self, page, receivers):
        """Reconcile the UI's explicit selection against connected TVs."""
        async with self.lock:
            await self.open_page(page, preserve_view=True)
            desired = {receiver['id'] for receiver in receivers}
            for tv_id in list(self.runtime['receivers']):
                if tv_id not in desired:
                    await self.stop_receiver(tv_id)
            # Attempt every selected TV even when one connection fails.
            failed = False
            for receiver in receivers:
                try:
                    await self.add_receiver(receiver)
                except (ValueError, OSError):
                    failed = True
            if failed:
                raise ValueError('One or more TVs could not connect')

    async def stop(self, tv_id=None):
        async with self.lock:
            await self.stop_receiver(tv_id)

    async def stop_receiver(self, tv_id=None):
        """Stop one TV or every TV; caller holds the session lock."""
        if tv_id is not None and tv_id not in self.runtime['receivers']:
            return
        if self.process and self.process.returncode is None:
            await self.request('stop', **({'tv_id': tv_id} if tv_id is not None else {}))
        self.set_receivers({key: value for key, value in self.runtime['receivers'].items() if tv_id is not None and key != tv_id})

    async def browser_action(self, action):
        async with self.lock:
            await self.request(action)

    async def insert_text(self, value):
        browser_text(value)
        async with self.lock:
            if self.runtime["browser"] != "ready":
                raise ValueError("Open a page first")
            await self.request("insert_text", value=value)

    async def pin(self, value, tv_id=None):
        async with self.lock:
            pairing = [key for key, receiver in self.runtime['receivers'].items() if receiver['state'] == 'pairing']
            if tv_id is None:
                if len(pairing) != 1:
                    raise ValueError('Choose the TV waiting for a pairing code')
                tv_id = pairing[0]
            if tv_id not in pairing:
                raise ValueError("The TV is not waiting for a pairing code")
            await self.request("pin", tv_id=tv_id, value=value)
            # A fast worker may already have reported sending while its reply
            # was in flight; do not regress that state to starting.
            if self.runtime['receivers'].get(tv_id, {}).get('state') == 'pairing':
                self.receiver_event({'tv_id': tv_id, 'state': 'starting'})

    async def close_worker(self):
        process = self.process
        if process:
            if process.returncode is None:
                # Ask the worker to flush Chrome before its private bus exits.
                with contextlib.suppress(ValueError, OSError):
                    await self.request('close')
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
            self.update(browser="closed", airplay="idle", page_id=None, tv_id=None, receivers={}, error=None)
