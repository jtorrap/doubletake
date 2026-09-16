# Doubletake Browser

Open a real browser from Home Assistant and send its view and audio to saved Apple TVs.
The interactive preview supports signing in, clicking, typing, and navigating.
Named pages appear as launch buttons under each TV's Home Assistant device.
YouTube links can launch directly into playback, and Watch Later can play
unfinished videos in order. Both actions are available to HA automations.

One shared browser page can play on several TVs at once, with separate connection,
pairing, and Stop controls. The default output is 1080p at 30 fps. The interactive
preview is silent; audio plays on the TVs. The app runs on amd64 Home Assistant
hosts and requires a working MQTT service.

See [usage and deployment](DOCS.md) and the [implementation plan](PLAN.md).
