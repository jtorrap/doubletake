"""Private, bounded native-extension channel; never expose a TCP control port."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import struct

from youtube import launch


MAX_MESSAGE = 32768
HELLO_TIMEOUT = 5
CONNECT_TIMEOUT = 12
REPLY_TIMEOUT = 10


class YouTubeBridge:
    def __init__(self, on_status):
        self.on_status = on_status
        self.server = self.writer = None
        self.connected = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.sequence = 0
        self.pending = {}
        self.clients = set()
        self.active = None
        self.closed = False

    async def start(self, runtime):
        self.path = Path(runtime) / 'youtube.sock'
        self.server = await asyncio.start_unix_server(self.connection, str(self.path), limit=MAX_MESSAGE + 1)
        self.path.chmod(0o600)
        return str(self.path)

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        accepted = False
        try:
            peer = writer.get_extra_info('socket')
            if hasattr(socket, 'SO_PEERCRED'):
                _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.geteuid():
                    return
            hello = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            if len(hello) > MAX_MESSAGE or json.loads(hello) != {'type': 'hello'} or self.writer or self.closed:
                return
            self.writer = writer
            accepted = True
            self.connected.set()
            while raw := await reader.readline():
                if len(raw) > MAX_MESSAGE:
                    return
                value = json.loads(raw)
                if not isinstance(value, dict):
                    return
                if value.get('type') == 'reply' and type(value.get('id')) is int:
                    future = self.pending.get(value['id'])
                    if future and not future.done():
                        if value.get('ok') is True:
                            future.set_result(True)
                        else:
                            future.set_exception(ValueError('YouTube launch failed'))
                elif value.get('type') == 'status':
                    if (self.active and type(value.get('launch_id')) is int
                            and value['launch_id'] == self.active['launch_id']
                            and value.get('mode') == self.active['mode']
                            and value.get('state') in {'loading', 'playing', 'paused', 'finished', 'needs_interaction', 'error'}):
                        self.on_status({key: value[key] for key in ('launch_id', 'mode', 'state')})
        except (OSError, ValueError, TypeError, RecursionError, asyncio.TimeoutError):
            pass
        finally:
            self.clients.discard(task)
            if accepted and self.writer is writer:
                self.writer = None
                self.connected.clear()
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ValueError('YouTube control disconnected'))
                if self.active and not self.closed:
                    self.on_status({**self.active, 'state': 'needs_interaction'})
                    self.active = None
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def wait_connected(self):
        if self.closed:
            raise ValueError('YouTube control is closed')
        connected = asyncio.create_task(self.connected.wait())
        closed = asyncio.create_task(self.closed_event.wait())
        try:
            done, _ = await asyncio.wait({connected, closed}, timeout=CONNECT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if self.closed:
                raise ValueError('YouTube control is closed')
            if not done or not self.writer:
                raise ValueError('YouTube control is unavailable')
        finally:
            for task in (connected, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connected, closed, return_exceptions=True)

    async def request(self, action, **fields):
        await self.wait_connected()
        self.sequence += 1
        sequence = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = future
        writer = self.writer
        try:
            writer.write(json.dumps({'id': sequence, 'action': action, **fields}).encode() + b'\n')
            await writer.drain()
            await asyncio.wait_for(future, REPLY_TIMEOUT)
        except (OSError, AttributeError, asyncio.TimeoutError):
            await self.disconnect(writer)
            raise ValueError('YouTube control is unavailable') from None
        except asyncio.CancelledError:
            # The extension may have applied a written command already. Its
            # onDisconnect handler revokes any autonomous playback queue.
            await self.disconnect(writer)
            raise
        finally:
            self.pending.pop(sequence, None)

    async def disconnect(self, writer):
        if writer:
            if self.writer is writer:
                self.connected.clear()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def play(self, mode, *, launch_id, url=None, resume=True):
        intent = launch(mode, url=url, resume=resume)
        if type(launch_id) is not int or launch_id < 1:
            raise ValueError('Invalid launch')
        active = {'launch_id': launch_id, 'mode': mode}
        self.active = active
        try:
            await self.request('launch', launch_id=launch_id, **intent)
        except BaseException:
            if self.active is active:
                self.active = None
            raise
        return {'mode': mode, 'state': 'loading'}

    async def cancel(self):
        self.active = None
        if self.writer:
            await self.request('cancel')

    async def close(self):
        self.closed = True
        self.closed_event.set()
        self.active = None
        if self.server:
            self.server.close()
        if self.writer:
            self.writer.close()
        for task in list(self.clients):
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        if self.server:
            # Newer asyncio versions include accepted transports in this
            # wait. Close those clients before waiting for the server.
            await self.server.wait_closed()
        if hasattr(self, 'path'):
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
