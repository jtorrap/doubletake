# Changelog

## 0.1.6

- Use regular Chrome with native desktop controls by default, without a browser debugging channel.
- Preserve Paste, saved-page navigation, history and clean profile reopening.
- Hold the captured frame during address-bar navigation and guard input from concurrent preview clients.
- Retain the private-pipe backend as the explicit diagnostic configuration option.
- Keep GPU activity checks available; identify when detailed browser inspection is off.

## 0.1.5

- Always report a dark color preference to websites.
- Set the default browser zoom to 120% while retaining per-site overrides.
- Include output resolution, page zoom and color preference in Check video.

## 0.1.4

- Use ANGLE's Vulkan renderer for GPU decoding with the virtual display.
- Select the accessible render node explicitly for Chromium's media pipeline.

## 0.1.3

- Enable GPU video decoding on compatible Intel hosts, with a configuration toggle.
- Include Intel VA-API drivers and grant the non-root browser mapped GPU access.
- Add Check video to inspect codec support, playback and actual GPU video activity.
- Keep the browser sandbox enabled and retain software H.264 AirPlay encoding.

## 0.1.2

- Fix an unresponsive Paste button when a proxy serves an older app script.
- Give scripts and styles content-based filenames so updates load fresh controls.

## 0.1.1

- Add a masked Paste dialog for passwords and other text.
- Insert text into the focused browser field without submitting the form.
- Preserve Unicode and punctuation without using the remote clipboard.

## 0.1.0

- One interactive browser with one active Apple TV receiver.
- Authenticated Ingress preview, saved TVs/pages, private pairing flow.
- MQTT-discovered per-TV launch buttons, Stop, and status sensors.
- Persistent browser/receiver state and graceful browser shutdown.
