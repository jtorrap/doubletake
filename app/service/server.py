#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from aiohttp import web, WSMsgType
from model import Store, VERSION
from session import Session
from mqtt_bridge import MQTTBridge
from youtube import launch as youtube_launch
from hdhomerun import ChannelCatalog, ChannelError


async def discover_tvs():
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    found = {}
    tasks = set()
    loop = asyncio.get_running_loop()
    zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)

    async def resolve(service_type, name):
        info = await zeroconf.async_get_service_info(service_type, name, timeout=2500)
        if info:
            model = info.properties.get(b"model", b"").decode("utf-8", "replace")
            addresses = info.parsed_addresses()
            if model.startswith("AppleTV") and addresses:
                found[name] = {"name": name.removesuffix("._airplay._tcp.local."), "host": addresses[0], "port": info.port, "model": model}

    def changed(zeroconf, service_type, name, state_change):
        if state_change in {ServiceStateChange.Added, ServiceStateChange.Updated}:
            def schedule():
                task = asyncio.create_task(resolve(service_type, name))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            loop.call_soon_threadsafe(schedule)

    browser = AsyncServiceBrowser(zeroconf.zeroconf, "_airplay._tcp.local.", handlers=[changed])
    try:
        await asyncio.sleep(4)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return sorted(found.values(), key=lambda item: item["name"].casefold())
    finally:
        await browser.async_cancel()
        await zeroconf.async_close()


def create_app(directory, settings, *, development=False, session_factory=Session):
    store = Store(directory)
    channels = ChannelCatalog(directory)
    csrf = secrets.token_urlsafe(32)
    session = session_factory(directory, settings.get("quality", {"width": 1920, "height": 1080, "fps": 30, "bitrate": 8000, "hwaccel": "none"}))
    bridge = None
    novnc = Path(settings.get("novnc", "/usr/share/novnc"))
    static = Path(__file__).parent / "static"
    index_html = (static / "index.html").read_text()
    assets = {}
    # Some Ingress proxies retain static URLs even when no-store is sent and
    # ignore query strings. Change the path whenever an asset's bytes change.
    for name in ("app.js", "style.css"):
        path = static / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        filename = f"{path.stem}.{digest}{path.suffix}"
        assets[filename] = path
        index_html = index_html.replace(f'"static/{name}"', f'"assets/{filename}"')

    @web.middleware
    async def boundary(request, handler):
        if request.path == "/healthz" and request.method == "GET":
            return web.json_response({"ok": True})
        try:
            peer = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden(text="Ingress connection required")
        allowed = peer.is_loopback if development else str(peer) == "172.30.32.2"
        if not allowed:
            raise web.HTTPForbidden(text="Use the Home Assistant app interface")
        if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(request.headers.get("X-Doubletake-CSRF", ""), csrf):
            raise web.HTTPForbidden(text="Refresh the app interface and try again")
        try:
            response = await handler(request)
        except ChannelError as error:
            return web.json_response({"error": error.safe_message}, status=409)
        except (ValueError, KeyError, TypeError):
            # Return only our own validation messages; JSON parsing and
            # missing-key errors can contain private user input.
            return web.json_response({"error": "Check the selected items and entered values, then try again"}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response({"error": "The app could not complete that action"}, status=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=65536)
    app["store"], app["session"], app["csrf"] = store, session, csrf
    app["channels"] = channels

    async def index(_request):
        return web.Response(text=index_html, content_type="text/html")

    async def asset(request):
        path = assets.get(request.match_info["filename"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def state(_request):
        return web.json_response({**store.public(), "channels": channels.public(), "runtime": session.state(), "mqtt_connected": bool(bridge and bridge.connected), "csrf": csrf, "version": VERSION})

    async def put_item(request):
        kind = request.match_info["kind"]
        if kind not in {"tvs", "pages"}:
            raise web.HTTPNotFound()
        value = await request.json()
        item_id = request.match_info.get("item_id")
        # Changing an active receiver address requires a fresh connection.
        if kind == "tvs" and item_id is not None and item_id in session.runtime["receivers"]:
            old = store.get(kind, item_id)
            normalized = store.validate(kind, value)
            if old["host"] != normalized["host"] or old["port"] != normalized["port"]:
                await session.stop(item_id)
        item = store.put(kind, value, item_id)
        if bridge:
            bridge.refresh()
        return web.json_response(item)

    async def delete_item(request):
        kind, item_id = request.match_info["kind"], request.match_info["item_id"]
        if kind not in {"tvs", "pages"}:
            raise web.HTTPNotFound()
        store.get(kind, item_id)
        if kind == "tvs":
            await session.stop(item_id)
        elif item_id == session.runtime["page_id"]:
            await session.close()
        store.delete(kind, item_id)
        if bridge:
            bridge.refresh()
        return web.json_response({"ok": True})

    async def action(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError()
        operation = request.match_info["operation"]
        if operation in {"open", "cast"}:
            page = store.get("pages", body.get("page_id"))
            if operation == 'open':
                await session.open(page)
            else:
                if 'tv_ids' in body:
                    tv_ids = body['tv_ids']
                    if 'tv_id' in body or not isinstance(tv_ids, list) or not tv_ids or any(not isinstance(tv_id, str) for tv_id in tv_ids) or len(tv_ids) != len(set(tv_ids)):
                        raise ValueError()
                else:
                    tv_ids = [body.get('tv_id')]
                # Resolve the entire selection before opening a page or
                # disconnecting a TV. A stale ID must not partially apply.
                receivers = [store.get('tvs', tv_id) for tv_id in tv_ids]
                await session.cast(page, receivers)
        elif operation == 'youtube':
            if set(body) - {'mode', 'url', 'resume', 'tv_ids'}:
                raise ValueError()
            intent = youtube_launch(body.get('mode'), url=body.get('url'), resume=body.get('resume', True))
            receivers = None
            if 'tv_ids' in body:
                tv_ids = body['tv_ids']
                if not isinstance(tv_ids, list) or not tv_ids or any(not isinstance(tv_id, str) for tv_id in tv_ids) or len(tv_ids) != len(set(tv_ids)):
                    raise ValueError()
                receivers = [store.get('tvs', tv_id) for tv_id in tv_ids]
            await session.youtube(**intent, receivers=receivers)
        elif operation == 'channel':
            if set(body) != {'device_id', 'channel', 'tv_ids'}:
                raise ValueError()
            tv_ids = body['tv_ids']
            if not isinstance(tv_ids, list) or not tv_ids or any(not isinstance(v, str) for v in tv_ids) or len(tv_ids) != len(set(tv_ids)):
                raise ValueError()
            receivers = [store.get('tvs', tv_id) for tv_id in tv_ids]
            source = await channels.source(body['device_id'], body['channel'])
            await session.channel(source, receivers)
        elif operation == "stop":
            tv_id = body.get('tv_id')
            if tv_id is not None:
                store.get('tvs', tv_id)
            await session.stop(tv_id)
        elif operation == "close":
            await session.close()
        elif operation == "browser":
            if body.get("action") not in {"back", "forward", "reload"}:
                raise ValueError()
            await session.browser_action(body["action"])
        elif operation == "pin":
            tv_id = body.get('tv_id')
            if tv_id is not None:
                store.get('tvs', tv_id)
            try:
                await session.pin(body.get("value"), tv_id)
            finally:
                body.clear()
        elif operation == "insert_text":
            try:
                await session.insert_text(body.get("value"))
            finally:
                body.clear()
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True, "runtime": session.state()})

    async def discover(_request):
        return web.json_response({"tvs": await discover_tvs()})

    async def channel_discover(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {'host'}:
            raise ValueError()
        result = await channels.refresh(body.get('host'))
        if bridge:
            bridge.refresh()
        return web.json_response(result)

    async def channel_favorite(request):
        body = await request.json()
        if set(body) != {'device_id', 'channel', 'enabled'}:
            raise ValueError()
        channels.favorite(body['device_id'], body['channel'], body['enabled'])
        return web.json_response(channels.public())

    async def diagnostics(_request):
        async with session.lock:
            return web.json_response(await session.request("diagnostics"))

    async def preview_info(_request):
        if not session.preview:
            raise web.HTTPConflict(text="Open a page first")
        return web.json_response({"password": session.preview["password"]})

    async def preview_socket(request):
        # The browser sends this token as a WebSocket subprotocol, so it does
        # not enter URLs, access logs, or referrers.
        protocols = [part.strip() for part in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        if not any(hmac.compare_digest(value, "doubletake." + csrf) for value in protocols):
            raise web.HTTPForbidden()
        if not session.preview:
            raise web.HTTPConflict()
        reader, writer = await asyncio.open_connection("127.0.0.1", session.preview["port"])
        ws = web.WebSocketResponse(protocols=["binary"], heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)

        async def to_browser():
            while data := await reader.read(65536):
                await ws.send_bytes(data)
            await ws.close()

        transfer = asyncio.create_task(to_browser())
        try:
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    writer.write(message.data)
                    await writer.drain()
                elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                    break
        finally:
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return ws

    async def mqtt_command(tv_id, command):
        try:
            tv = store.get("tvs", tv_id)
            if command["action"] == "cast":
                # A second TV joins the currently viewed page without
                # resetting playback or a user's navigation on that page.
                await session.open(store.get("pages", command.get("page_id")), tv, preserve_view=True)
            elif command['action'] in {'youtube', 'watch_later'}:
                mode = 'video' if command['action'] == 'youtube' else 'watch_later'
                intent = youtube_launch(mode, url=command.get('url'), resume=command.get('resume', True))
                if set(command) - {'action', 'url', 'resume'}:
                    raise ValueError()
                await session.youtube(**intent, receivers=[tv], replace_receivers=False)
            elif command['action'] == 'select_channel':
                if set(command) != {'action', 'selection'}:
                    raise ValueError()
                channels.select(tv_id, command['selection'])
                if bridge:
                    bridge.publish_state()
            elif command['action'] == 'channel':
                if set(command) == {'action'}:
                    selection = channels.selected(tv_id)
                    if not selection:
                        raise ValueError()
                    device_id, number = selection
                elif set(command) == {'action', 'device_id', 'channel'}:
                    device_id, number = command['device_id'], command['channel']
                else:
                    raise ValueError()
                source = await channels.source(device_id, number)
                await session.channel(source, [tv], replace_receivers=False)
            elif command['action'] == 'stop':
                await session.stop(tv_id)
            else:
                raise ValueError()
        except (ValueError, OSError):
            # Connection failures already belong to the affected TV. Keep a
            # healthy peer's browser-level status clear.
            if session.runtime['receivers'].get(tv_id, {}).get('state') != 'error':
                session.update(error="The Home Assistant command could not complete; open the app to check the browser")

    async def lifecycle(_app):
        nonlocal bridge
        if settings.get("mqtt"):
            bridge = MQTTBridge(store, settings["mqtt"], directory, mqtt_command, session.state, channels=channels)
            session.notify = bridge.publish_state
            bridge.start()
        yield
        await session.close()
        if bridge:
            await bridge.stop()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/assets/{filename}", asset)
    app.router.add_get("/healthz", state)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/settings/{kind}", put_item)
    app.router.add_put("/api/settings/{kind}/{item_id}", put_item)
    app.router.add_delete("/api/settings/{kind}/{item_id}", delete_item)
    app.router.add_post("/api/action/{operation}", action)
    app.router.add_post("/api/discover", discover)
    app.router.add_post("/api/channels/discover", channel_discover)
    app.router.add_post("/api/channels/favorite", channel_favorite)
    app.router.add_post("/api/diagnostics", diagnostics)
    app.router.add_get("/api/preview", preview_info)
    app.router.add_get("/ws/preview", preview_socket)
    app.router.add_static("/static", Path(__file__).parent / "static")
    if novnc.is_dir():
        app.router.add_static("/novnc", novnc)
    return app


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--data-dir", default="/data/doubletake")
    parser.add_argument("--bootstrap", default="/run/doubletake/bootstrap.json")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    if args.development:
        settings = {"novnc": os.environ.get("NOVNC_DIR", "/usr/share/novnc")}
    else:
        settings = json.loads(Path(args.bootstrap).read_text())
        Path(args.bootstrap).unlink()
    app = create_app(args.data_dir, settings, development=args.development)
    web.run_app(app, host="127.0.0.1" if args.development else "0.0.0.0", port=settings.get("port", args.port), access_log=None, print=lambda _message: print("Doubletake Browser web interface ready", flush=True), shutdown_timeout=55)


if __name__ == "__main__":
    main()
