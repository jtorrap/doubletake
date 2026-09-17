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
from hdhomerun import ChannelError
from performance import video_stats, audio_stats
from youtube import launch as youtube_launch, WATCH_LATER_URL


def worker_environment():
    return {key: os.environ[key] for key in [
        'PATH', 'LANG', 'LC_ALL', 'HOME', 'DOUBLETAKE_LAUNCHER',
        'DOUBLETAKE_HARDWARE_DECODING', 'DOUBLETAKE_HARDWARE_ENCODING',
        'DOUBLETAKE_DISPLAY_BACKEND', 'DOUBLETAKE_TARGET_LATENCY_MS', 'DOUBLETAKE_AUDIO',
    ] if key in os.environ}


class Session:
    def __init__(self, directory, quality, notify=lambda: None):
        self.directory = Path(directory)
        self.quality, self.notify = quality, notify
        self.process = self.reader_task = self.ready = None
        self.preview = None
        self.source_identity = None
        self.channel_generation = 0
        self.warming_process = None
        self.receiver_configs = {}
        self.closing_worker = False
        self.sequence = 0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.control_mode = os.environ.get('DOUBLETAKE_BROWSER_CONTROL', 'native')
        if self.control_mode not in {'native', 'diagnostic'}:
            raise ValueError('Unknown browser control mode')
        self.audio_enabled = os.environ.get('DOUBLETAKE_AUDIO', 'true') == 'true'
        self.youtube_identity = None
        self.youtube_sequence = 0
        self.youtube_launch_id = None
        self.runtime = {"browser": "closed", "airplay": "idle", "page_id": None, "tv_id": None, "receivers": {}, "error": None,
                        'source_label': None, 'youtube': None, 'source_kind': 'browser', 'channel': None}

    def state(self):
        return {**self.runtime, 'receivers': {key: {**value, 'performance_age_seconds': round(time.monotonic() - value.get('performance_at', time.monotonic()), 1)} for key, value in self.runtime['receivers'].items()},
                'control_mode': self.control_mode,
                'display': {key: (30 if key == 'fps' and self.runtime.get('source_kind') == 'hdhomerun' else self.quality.get(key, default)) for key, default in [('width', 1920), ('height', 1080), ('fps', 30)]},
                'audio_enabled': self.audio_enabled}

    def youtube_event(self, value):
        current = self.runtime['youtube']
        if not current or self.youtube_launch_id is None or type(value.get('launch_id')) is not int or value['launch_id'] != self.youtube_launch_id or value.get('mode') != current['mode']:
            return
        state = value.get('state')
        if state not in {'loading', 'playing', 'paused', 'finished', 'needs_interaction', 'error'}:
            return
        status = {'mode': current['mode'], 'state': state}
        if state == 'error':
            status['error'] = 'YouTube playback could not continue. Open the preview to check the page.'
        self.update(youtube=status)

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
        if self.runtime.get('source_kind') == 'hdhomerun' and state == 'sending':
            self.runtime['channel'] = {**(self.runtime.get('channel') or {}), 'state': 'playing'}
        if state == 'error':
            self.receiver_configs.pop(tv_id, None)
        if state == 'error' and self.audio_enabled:
            receivers[tv_id]['audio'] = 'error'
        self.set_receivers(receivers)

    def audio_event(self, value):
        tv_id, state = value.get('tv_id'), value.get('state')
        if tv_id not in self.runtime['receivers'] or state not in {'starting', 'active', 'unavailable', 'error', 'disabled'}:
            return
        self.set_receivers({**self.runtime['receivers'], tv_id: {**self.runtime['receivers'][tv_id], 'audio': state}})

    def audio_performance_event(self, value):
        receiver = self.runtime['receivers'].get(value.get('tv_id'))
        stats = audio_stats(value.get('data'))
        if receiver is not None and stats:
            receiver.update(audio_performance=stats, audio_performance_at=time.monotonic())

    def receivers_failed(self):
        self.receiver_configs.clear()
        self.set_receivers({tv_id: {'state': 'error', 'error': 'The source session ended.', 'audio': 'error' if self.audio_enabled else 'disabled'} for tv_id in self.runtime['receivers']})

    async def read_events(self, process):
        try:
            while raw := await process.stdout.readline():
                value = json.loads(raw)
                if value.get("type") == "ready":
                    self.preview = None if value.get("media") else {"port": int(value["port"]), "password": value["password"]}
                    self.update(browser="closed" if value.get("media") else "ready", error=None)
                    if not self.ready.done():
                        self.ready.set_result(True)
                elif value.get("type") == "channel":
                    self.update(channel={**(self.runtime.get("channel") or {}), "state": "error"}, error=ChannelError(value.get("code")).safe_message)
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
                elif value.get('type') == 'youtube':
                    self.youtube_event(value)
                elif value.get('type') == 'performance':
                    stats = video_stats(value.get('data'))
                    receiver = self.runtime['receivers'].get(value.get('tv_id'))
                    if receiver is not None and stats and value.get('encoder') in {'vaapi', 'none'}:
                        # Polling clients receive metrics without republishing
                        # all MQTT discovery/state for every five-second sample.
                        receiver.update(performance=stats, performance_at=time.monotonic(), encoder=value['encoder'])
                elif value.get('type') == 'audio_performance':
                    self.audio_performance_event(value)
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
            if not self.closing_worker and self.process is process and (self.runtime["browser"] != "closed" or self.runtime.get("source_kind") == "hdhomerun"):
                self.preview = None
                self.receivers_failed()
                if self.runtime.get("source_kind") == "hdhomerun":
                    self.update(browser="closed", channel={**(self.runtime.get("channel") or {}), "state":"error"},
                                error=self.runtime.get('error') or "Channel playback ended. Press Play to try again.")
                else:
                    self.update(browser="error")

    async def ensure(self, page):
        identity = (page["device_id"], page["channel"]) if page.get("kind") == "hdhomerun" else None
        if self.process and self.process.returncode is None and ((identity is None and self.preview) or (identity is not None and identity == self.source_identity)):
            return False
        await self.close_worker()
        self.set_receivers({})
        self.source_identity = identity
        self.update(browser="closed" if identity else "starting", source_kind="hdhomerun" if identity else "browser",
                    channel={"device_id": page["device_id"], "number": page["channel"], "state": "starting"} if identity else None, error=None)
        config = {"url": page["url"], "quality": self.quality, "control_mode": self.control_mode,
                  "profile_dir": str(self.directory / "browser"), "receivers_dir": str(self.directory / "receivers")}
        if os.environ.get("DOUBLETAKE_BROWSER"):
            config["browser"] = os.environ["DOUBLETAKE_BROWSER"]
        if os.environ.get("DOUBLETAKE_SENDER"):
            config["sender"] = os.environ["DOUBLETAKE_SENDER"]
        if identity:
            config["source"] = page
        path = self.directory / "worker.json"
        atomic_json(path, config)
        # Explicit allowlist: credentials for Supervisor/MQTT never cross into
        # the page-rendering process or its browser/encoder children.
        env = worker_environment()
        self.ready = asyncio.get_running_loop().create_future()
        command = [sys.executable, "-u", "-B", str(Path(__file__).with_name("media_worker.py" if identity else "worker.py")), str(path)]
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
            restore = list(self.receiver_configs.values()) if self.runtime.get("source_kind") == "hdhomerun" else []
            await self.open_page(page, preserve_view=preserve_view)
            for target in restore:
                await self.add_receiver(target)
            if receiver:
                await self.add_receiver(receiver)

    async def open_page(self, page, *, preserve_view):
        """Open the one shared page; caller holds the session lock."""
        await self.cancel_youtube()
        created = await self.ensure(page)
        if not created and not (preserve_view and self.runtime['page_id'] == page['id']):
            await self.request('navigate', url=page['url'])
        self.update(page_id=page['id'], source_label=page.get('name'), error=None)

    async def cancel_youtube(self):
        """Invalidate pending playback events before issuing normal navigation."""
        active = self.youtube_launch_id is not None
        self.youtube_launch_id = self.youtube_identity = None
        if self.runtime['youtube'] is not None:
            self.update(youtube=None, source_label=None)
        if active and self.process and self.process.returncode is None:
            await self.request('youtube_cancel')

    async def youtube(self, mode, *, url=None, resume=True, receivers=None, replace_receivers=True):
        intent = youtube_launch(mode, url=url, resume=resume)
        if receivers is not None and not receivers:
            raise ValueError('Select at least one TV or omit the selection')
        identity = (intent['mode'], intent.get('url'), intent['resume'])
        async with self.lock:
            if self.runtime.get('source_kind') == 'hdhomerun' and (receivers is None or not replace_receivers):
                joined = {**self.receiver_configs, **{r['id']: r for r in receivers or []}}
                receivers = list(joined.values())
            if receivers is not None and replace_receivers:
                desired = {receiver['id'] for receiver in receivers}
                # An unselected TV must never briefly show the new source.
                for tv_id in list(self.runtime['receivers']):
                    if tv_id not in desired:
                        await self.stop_receiver(tv_id)
            page = {'id': None, 'url': intent.get('url', WATCH_LATER_URL)}
            created = await self.ensure(page)
            current = self.runtime['youtube'] or {}
            preserved_states = {'loading', 'playing'} | ({'paused'} if not replace_receivers else set())
            if created or identity != self.youtube_identity or current.get('state') not in preserved_states:
                await self.cancel_youtube()
                self.youtube_sequence += 1
                self.youtube_launch_id = self.youtube_sequence
                self.youtube_identity = identity
                self.update(page_id=None, source_label='Watch Later' if mode == 'watch_later' else 'YouTube',
                            youtube={'mode': mode, 'state': 'loading'}, error=None)
                try:
                    result = await self.request('youtube', **intent, launch_id=self.youtube_launch_id)
                    if isinstance(result, dict):
                        if result.get('state') != 'loading' or self.runtime['youtube']['state'] == 'loading':
                            self.youtube_event({**result, 'launch_id': self.youtube_launch_id, 'mode': mode})
                except (ValueError, OSError):
                    self.youtube_event({'launch_id': self.youtube_launch_id, 'mode': mode, 'state': 'error'})
                    raise
            failed = False
            for receiver in receivers or []:
                try:
                    await self.add_receiver(receiver)
                except (ValueError, OSError):
                    failed = True
            if failed:
                raise ValueError('One or more TVs could not connect')

    async def add_receiver(self, receiver):
        """Add or retry a receiver without interrupting its peers."""
        tv_id = receiver['id']
        self.receiver_configs[tv_id] = dict(receiver)
        current = self.runtime['receivers'].get(tv_id, {})
        if current.get('state') in {'starting', 'pairing', 'sending'}:
            return
        self.set_receivers({**self.runtime['receivers'], tv_id: {'state': 'starting', 'error': None, 'audio': 'starting' if self.audio_enabled else 'disabled'}})
        try:
            await self.request('cast', receiver=receiver)
        except (ValueError, OSError):
            self.receiver_event({'tv_id': tv_id, 'state': 'error'})
            raise

    async def prepare_channel(self, source):
        """Make the new tuner input ready while the current view keeps playing."""
        generation = self.channel_generation
        config = {'source': source, 'quality': {**self.quality, 'fps':30},
                  'receivers_dir': str(self.directory / 'receivers')}
        if os.environ.get('DOUBLETAKE_SENDER'):
            config['sender'] = os.environ['DOUBLETAKE_SENDER']
        path = self.directory / 'channel-worker.json'
        atomic_json(path, config)
        process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-B',
            str(Path(__file__).with_name('media_worker.py')), str(path),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=worker_environment(), start_new_session=True)
        self.warming_process = process
        try:
            raw = await asyncio.wait_for(process.stdout.readline(), 20)
            if generation != self.channel_generation:
                raise ChannelError('cancelled')
            event = json.loads(raw)
            if event.get('type') != 'ready' or event.get('media') is not True:
                raise ChannelError(event.get('code'))
            return process
        except BaseException:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 8)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        finally:
            if self.warming_process is process:
                self.warming_process = None
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def adopt_channel(self, source):
        generation = self.channel_generation
        identity = (source['device_id'], source['channel'])
        if self.source_identity == identity and self.process and self.process.returncode is None:
            return
        try:
            candidate = await self.prepare_channel(source)
        except ValueError as error:
            if not isinstance(error, ChannelError) or error.code != 'busy' or self.runtime.get('source_kind') != 'hdhomerun':
                raise
            # No spare tuner: release only our previous input, then try once.
            await self.close_worker()
            self.set_receivers({})
            self.source_identity = None
            self.update(channel={**(self.runtime.get('channel') or {}), 'state':'stopped'})
            candidate = await self.prepare_channel(source)
        try:
            if generation != self.channel_generation:
                raise ChannelError('cancelled')
            await self.cancel_youtube()
            await self.close_worker()
            if generation != self.channel_generation:
                raise ChannelError('cancelled')
        except BaseException:
            candidate.terminate()
            await candidate.wait()
            raise
        self.set_receivers({})
        self.process = candidate
        self.preview = None
        self.source_identity = identity
        self.ready = asyncio.get_running_loop().create_future()
        self.ready.set_result(True)
        self.update(browser='closed', source_kind='hdhomerun', page_id=None, youtube=None,
                    channel={'device_id':source['device_id'], 'number':source['channel'], 'state':'starting'},
                    source_label=source['label'], error=None)
        self.reader_task = asyncio.create_task(self.read_events(candidate))

    async def channel(self, source, receivers, *, replace_receivers=True, generation=None):
        """Play one resolved channel on the exact UI set or add MQTT receivers."""
        if not receivers:
            raise ValueError('Select at least one TV')
        if generation is None:
            generation = self.channel_generation
        async with self.lock:
            if generation != self.channel_generation:
                raise ChannelError('cancelled')
            desired = {r['id']: r for r in receivers}
            if not replace_receivers:
                desired = {**self.receiver_configs, **desired}
            for tv_id in list(self.runtime['receivers']):
                if tv_id not in desired:
                    await self.stop_receiver(tv_id)
            await self.adopt_channel(source)
            self.update(page_id=None, source_label=source['label'], error=None)
            failed = False
            for receiver in desired.values():
                try:
                    await self.add_receiver(receiver)
                except (ValueError, OSError):
                    failed = True
            if failed:
                if not any(r['state'] in {'starting','pairing','sending'} for r in self.runtime['receivers'].values()):
                    await self.close_worker()
                    self.source_identity = None
                    self.update(channel={**(self.runtime.get('channel') or {}), 'state':'error'})
                raise ValueError('One or more TVs could not connect')

    async def cast(self, page, receivers):
        """Reconcile the UI's explicit selection against connected TVs."""
        async with self.lock:
            desired = {receiver['id'] for receiver in receivers}
            for tv_id in list(self.runtime['receivers']):
                if tv_id not in desired:
                    await self.stop_receiver(tv_id)
            await self.open_page(page, preserve_view=True)
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
        self.cancel_channel_warmup()
        async with self.lock:
            await self.stop_receiver(tv_id)

    def cancel_channel_warmup(self):
        # Stop must invalidate tuning before waiting for the source lock. A
        # slow/failed channel must never take over a TV after the user stops.
        self.channel_generation += 1
        if self.warming_process and self.warming_process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.warming_process.terminate()

    async def stop_receiver(self, tv_id=None):
        """Stop one TV or every TV; caller holds the session lock."""
        if tv_id is not None and tv_id not in self.runtime['receivers']:
            return
        if self.process and self.process.returncode is None:
            await self.request('stop', **({'tv_id': tv_id} if tv_id is not None else {}))
        self.set_receivers({key: value for key, value in self.runtime['receivers'].items() if tv_id is not None and key != tv_id})
        self.receiver_configs = {key: value for key, value in self.receiver_configs.items() if tv_id is not None and key != tv_id}
        if not self.runtime["receivers"] and self.runtime.get("source_kind") == "hdhomerun":
            await self.close_worker()
            self.source_identity = None
            self.update(channel={**(self.runtime.get("channel") or {}), "state": "stopped"}, error=None)

    async def browser_action(self, action):
        async with self.lock:
            await self.cancel_youtube()
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
            self.closing_worker = True
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
        self.closing_worker = False

    async def close(self):
        self.cancel_channel_warmup()
        async with self.lock:
            with contextlib.suppress(ValueError, OSError):
                await self.cancel_youtube()
            await self.close_worker()
            self.source_identity = None
            self.receiver_configs.clear()
            self.update(browser="closed", airplay="idle", page_id=None, tv_id=None, receivers={}, error=None, source_label=None, youtube=None, channel=None, source_kind="browser")
