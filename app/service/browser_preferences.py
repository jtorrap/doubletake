"""Set browser defaults before launch, preserving all other profile settings."""
import json
from pathlib import Path
from model import atomic_json


def prepare_profile(profile_directory):
    # The session has stopped its previous worker before this is called. Never
    # edit a running Chrome profile: Chrome would overwrite the preferences.
    path = Path(profile_directory) / 'Default' / 'Preferences'
    preferences = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(preferences, dict):
        raise ValueError('Browser preferences are invalid')
    partition = preferences.setdefault('partition', {})
    if not isinstance(partition, dict):
        raise ValueError('Browser preferences are invalid')
    zoom = partition.setdefault('default_zoom_level', {})
    if not isinstance(zoom, dict):
        raise ValueError('Browser preferences are invalid')
    # Chrome stores logarithmic levels: factor = 1.2 ** level. The default
    # storage partition has key "x" (empty relative path), so 0.0 means 100%.
    # Preserve per-site zoom overrides and unrelated profile/authentication data.
    zoom['x'] = 0.0
    atomic_json(path, preferences)
