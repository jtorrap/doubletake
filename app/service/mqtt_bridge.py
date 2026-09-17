"""MQTT discovery and commands, scoped to this installation's topics."""
import asyncio
import json
from pathlib import Path
import paho.mqtt.client as mqtt
from model import atomic_json, discovery


class MQTTBridge:
    def __init__(self, store, credentials, directory, on_command, state, channels=None):
        self.store, self.credentials, self.on_command, self.state = store, credentials, on_command, state
        self.channels = channels
        self.loop = asyncio.get_running_loop()
        self.connected = False
        self.base = "doubletake_browser/" + store.data["installation_id"]
        self.topics_path = Path(directory) / "discovery-topics.json"
        self.previous = set(json.loads(self.topics_path.read_text())) if self.topics_path.exists() else set()
        self.client = mqtt.Client(client_id="doubletake_" + store.data["installation_id"], clean_session=True)
        self.client.username_pw_set(credentials["username"], credentials["password"])
        if credentials.get("ssl"):
            self.client.tls_set()
        self.client.will_set(self.base + "/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 30)
        self.client.on_connect = self._connect
        self.client.on_disconnect = self._disconnect
        self.client.on_message = self._message

    def start(self):
        self.client.connect_async(self.credentials["host"], int(self.credentials.get("port", 1883)), 30)
        self.client.loop_start()

    def _connect(self, client, _userdata, _flags, rc):
        if rc == 0:
            client.subscribe(self.base + "/+/command", qos=0)
            client.subscribe("homeassistant/status", qos=0)
            self.loop.call_soon_threadsafe(self._online)

    def _online(self):
        self.connected = True
        self.refresh()
        self.client.publish(self.base + "/availability", "online", qos=1, retain=True)

    def _disconnect(self, *_args):
        self.loop.call_soon_threadsafe(setattr, self, "connected", False)

    def _message(self, _client, _userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self.loop.call_soon_threadsafe(self.refresh)
            return
        # Retained commands must never take over a TV after restart.
        if message.retain or len(message.payload) > 8192:
            return
        parts = message.topic.split("/")
        if len(parts) != 4 or "/".join(parts[:2]) != self.base or parts[3] != "command":
            return
        try:
            command = json.loads(message.payload)
            if not isinstance(command, dict) or command.get("action") not in {"cast", "stop", "youtube", "watch_later", "channel", "select_channel"}:
                return
        except (ValueError, TypeError):
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.on_command(parts[2], command)))

    def refresh(self):
        if not self.connected:
            return
        messages = discovery(self.store, channels=self.channels)
        for topic in self.previous - messages.keys():
            if topic.startswith("homeassistant/") and ("doubletake_" + self.store.data["installation_id"] + "_") in topic:
                self.client.publish(topic, b"", qos=1, retain=True)
        for topic, value in messages.items():
            self.client.publish(topic, json.dumps(value), qos=1, retain=True)
        self.previous = set(messages)
        atomic_json(self.topics_path, sorted(self.previous))
        self.publish_state()

    def publish_state(self):
        if not self.connected:
            return
        runtime = self.state()
        pages = {page["id"]: page["name"] for page in self.store.data["pages"]}
        for tv in self.store.data["tvs"]:
            receiver = runtime.get('receivers', {}).get(tv['id'])
            active = receiver is not None
            state = receiver['state'] if active else 'idle'
            page = (runtime.get('source_label') or pages.get(runtime.get("page_id"), "None")) if active else "None"
            self.client.publish(f"{self.base}/{tv['id']}/state", json.dumps({"state": state, "page": page, **({"source": runtime.get("source_kind", "browser") if active else "None", "selected_channel": self.channels.data["selected"].get(tv["id"], "")} if self.channels else {})}), qos=1, retain=True)

    async def stop(self):
        if self.connected:
            info = self.client.publish(self.base + "/availability", "offline", qos=1, retain=True)
            await asyncio.to_thread(info.wait_for_publish, 3)
        self.client.disconnect()
        await asyncio.to_thread(self.client.loop_stop)
