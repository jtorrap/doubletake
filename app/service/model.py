"""Persistent names and stable IDs. Browser authentication lives elsewhere."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

VERSION = "0.1.0"
ID = re.compile(r"^[a-f0-9]{16}$")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Unknown saved item")
    return value


def title(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a name between 1 and 80 characters")
    return value.strip()


def page_url(value):
    try:
        parsed = urlsplit(value)
        valid = isinstance(value, str) and len(value) <= 4096 and parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        valid = valid and not any(c.isspace() or ord(c) < 32 for c in value)
        _ = parsed.port
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Enter an HTTP or HTTPS URL without embedded credentials")
    return value


def receiver_host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("Enter the TV's IP address or hostname")
    value = value.strip().rstrip(".")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value):
            raise ValueError("Enter the TV's IP address or hostname") from None
        return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".new")
    with open(tmp, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "settings.json"
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if self.data["version"] != 1 or not ID.fullmatch(self.data["installation_id"]):
                    raise ValueError()
                for kind in ("tvs", "pages"):
                    for item in self.data[kind]:
                        identifier(item["id"])
                        self.validate(kind, item)
            except (ValueError, KeyError, TypeError):
                raise RuntimeError("Saved settings could not be read; the original file has been preserved") from None
        else:
            self.data = {"version": 1, "installation_id": uuid.uuid4().hex[:16], "tvs": [], "pages": []}
            self.save()

    def save(self):
        atomic_json(self.path, self.data)

    def public(self):
        return copy.deepcopy({kind: self.data[kind] for kind in ("tvs", "pages")})

    def get(self, kind, item_id):
        identifier(item_id)
        item = next((item for item in self.data[kind] if item["id"] == item_id), None)
        if item is None:
            raise ValueError("Saved item no longer exists")
        return copy.deepcopy(item)

    def validate(self, kind, value):
        result = {"name": title(value.get("name"))}
        if kind == "pages":
            result["url"] = page_url(value.get("url"))
        elif kind == "tvs":
            result["host"] = receiver_host(value.get("host"))
            try:
                port = int(value.get("port", 7000))
            except (ValueError, TypeError):
                raise ValueError("Port must be between 1 and 65535") from None
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            result["port"] = port
        else:
            raise ValueError("Unknown settings category")
        return result

    def put(self, kind, value, item_id=None):
        result = self.validate(kind, value)
        if any(item["name"].casefold() == result["name"].casefold() and item["id"] != item_id for item in self.data[kind]):
            raise ValueError("Choose a unique name")
        if item_id is not None:
            self.get(kind, item_id)
        elif len(self.data[kind]) >= 32:
            raise ValueError("Up to 32 saved items are supported")
        result["id"] = item_id or uuid.uuid4().hex[:16]
        items = [result if item["id"] == item_id else item for item in self.data[kind]]
        if item_id is None:
            items.append(result)
        previous = self.data[kind]
        self.data[kind] = items
        try:
            self.save()
        except OSError:
            self.data[kind] = previous
            raise
        return result

    def delete(self, kind, item_id):
        self.get(kind, item_id)
        previous = self.data[kind]
        self.data[kind] = [item for item in previous if item["id"] != item_id]
        try:
            self.save()
        except OSError:
            self.data[kind] = previous
            raise


def discovery(store, prefix="homeassistant"):
    """A device per saved TV; a stable launch button per saved page."""
    installation = store.data["installation_id"]
    base = f"doubletake_browser/{installation}"
    messages = {}
    for tv in store.data["tvs"]:
        device_id = f"doubletake_{installation}_{tv['id']}"
        device = {"identifiers": [device_id], "name": tv["name"] + " Browser", "manufacturer": "Doubletake", "model": "Browser sender", "sw_version": VERSION}
        shared = {"device": device, "origin": {"name": "Doubletake Browser", "sw_version": VERSION, "support_url": "https://github.com/jtorrap/doubletake"}, "availability_topic": base + "/availability"}
        for page in store.data["pages"]:
            uid = device_id + "_" + page["id"]
            messages[f"{prefix}/button/{uid}/config"] = {**shared, "unique_id": uid, "name": "Show " + page["name"], "icon": "mdi:cast", "command_topic": f"{base}/{tv['id']}/command", "payload_press": json.dumps({"action": "cast", "page_id": page["id"]}), "retain": False, "qos": 0}
        uid = device_id + "_stop"
        messages[f"{prefix}/button/{uid}/config"] = {**shared, "unique_id": uid, "name": "Stop", "icon": "mdi:stop", "command_topic": f"{base}/{tv['id']}/command", "payload_press": '{"action":"stop"}', "retain": False, "qos": 0}
        for key, label, icon in [("state", "Status", "mdi:cast"), ("page", "Page", "mdi:web")]:
            uid = device_id + "_" + key
            messages[f"{prefix}/sensor/{uid}/config"] = {**shared, "unique_id": uid, "name": label, "icon": icon, "state_topic": f"{base}/{tv['id']}/state", "value_template": "{{ value_json." + key + " }}"}
    return messages
