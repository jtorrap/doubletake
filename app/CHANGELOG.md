# Changelog

## 0.2.0

- Select an HDHomeRun channel and play it on the selected TVs from the same UI.
- Discover the local lineup, search channels, and keep app-local favorites.
- Share one tuner/decode path and matching H.264 encoders across TVs, with each receiver's negotiated audio.
- Add a staged Channel selector, Play selected channel button and Source sensor to existing HA TV devices.
- Validate and warm a channel before TV takeover; release the tuner when the final TV stops.
- Retain saved pages, YouTube controls, browser sign-in, receiver IDs and pairings.
- Support unprotected MPEG-2/H.264 broadcasts with AC-3/MPEG/AAC audio. Protected and ATSC 3.0 channels are marked unavailable.

## 0.1.14

- Keep YouTube headers, recommendations, and comments outside the full-window video.
- Reveal consent and playback prompts when interaction is needed, then restore the player after playback resumes.

## 0.1.13

- Fix YouTube native-host discovery with the production startup permissions.
- Verify browser access to its public registration while keeping the signing key private.

## 0.1.12

- Play a pasted YouTube video URL with automatic playback and a full-window player.
- Play Watch Later in playlist order, skip completed videos, and resume partial videos.
- Expose both launch modes through per-TV Home Assistant controls and MQTT commands.
- Keep normal Chrome sign-in with a private, YouTube-only playback companion.

## 0.1.11

- Set the default browser zoom to 100%, retaining per-site overrides and dark mode.

## 0.1.10

- Report audio capture age, sent packets and stale-frame drops in Check video.
- Allow a joint audio/video buffering target for diagnosing receiver timing.

## 0.1.9

- Keep the headless display presentation clock running when the preview is closed.
- Measure the real X11 presentation clock in Linux tests, including disconnected preview behavior.
- Check the private browser audio signal and mute state without recording audio.

## 0.1.8

- Use a GPU-capable headless display on compatible Intel hosts, with an Xvfb fallback.
- Probe Intel H.264 hardware encoding and use it when available, with software fallback.
- Show measured sender frame rate and capture-to-send time for each TV.
- Include frame lateness, process CPU and encoder details in Check video.
- Pause the interactive preview independently while browser and TV playback continue.
- Test distinct full-screen video frames and pacing with two active receivers.
- Allow more time for native Unicode input and avoid unwanted URL autocomplete during navigation.

## 0.1.7

- Default to 1080p at 30 fps, retaining the 15 fps and 720p options.
- Send browser audio through a private audio sink with ALAC and AAC-ELD support.
- Select several TVs to receive the same browser page and audio simultaneously.
- Show each receiver's status, pairing prompt and Stop control independently.
- Keep Home Assistant launch buttons additive and Stop scoped to their own TV.
- Preserve the browser profile, native controls, GPU decoding, dark mode and zoom.

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
