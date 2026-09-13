# Using Doubletake Browser

Install and start the app, then choose **Open web UI** or use its sidebar entry.
The app uses your existing Home Assistant authentication through Ingress.
It obtains the MQTT broker connection from Supervisor automatically.

## Pages, TVs, and login

Add a named page such as **Home Assistant** with its normal dashboard URL.
Add a TV with its IP/hostname and AirPlay port, or choose **Find Apple TVs**.
The first version runs one browser and sends to one TV at a time.

Choose a page and **Open browser** to sign in. Click inside the preview to type;
mouse, keyboard, and scrolling operate the remote browser. Use Back, Forward,
Reload, or Expand in the preview toolbar. The persistent browser profile retains
cookies and local storage when the browser closes cleanly.

Choose a TV and **Show on TV** to send the same browser window. If the receiver
requests a pairing code or configured AirPlay password, enter it in the app.
Codes are passed privately and are not saved in settings or logs. Receiver
pairing credentials are stored separately for each saved TV.

Switching TVs stops the prior sender and connects the selected receiver.
**Stop** keeps the browser open. **Close browser** stops both. A receiver error
requires another explicit Show action; the app does not repeatedly reclaim a TV.

Use the page's normal sign-in flow and trusted certificate configuration. The
app does not bypass certificate errors or disable Chromium's sandbox.

## Home Assistant devices and automations

Each saved TV appears through MQTT discovery as **TV name Browser**. It has:

- **Show page name** buttons for each saved page.
- **Stop**, scoped to that TV. Stopping an inactive TV leaves the active TV alone.
- **Status** and **Page** sensors.

Select the matching button in an automation's **Perform action → Button: Press**
action. This binds a saved URL and a saved TV without placing URLs or credentials
in an automation. For example, pressing Basement Browser's **Show Home Assistant**
button selects that page and receiver. The actual entity ID is assigned by HA;
renaming a page or TV preserves its stable discovery identity.

The sending status indicates a negotiated sender connection. It does not prove
the physical TV is displaying a healthy page. Use the preview plus a real-TV
check when setting up a dashboard.

MQTT commands are not retained, and retained commands received after reconnect
are ignored. A Home Assistant restart republishes discovery and current state
without starting playback.

## Quality and resource usage

The HA app Configuration tab offers 1080p at 15 or 30 fps, or 720p at 30 fps.
Changing this option requires an app restart. Start with the default 1080p/15
and measure CPU load while displaying your actual cameras and charts.

The initial implementation sends video only. It uses software H.264 encoding,
an amd64 Google Chrome build with video codecs, Xvfb, and the existing doubletake
AirPlay implementation. Host networking supports receiver discovery and the
negotiated AirPlay ports. Ingress accepts only Supervisor's gateway; VNC is
password-protected and loopback-only. No debugging port is exposed.

## Storage, backups, and rollback

All runtime data is in the app's private `/data/doubletake` directory: named
settings, browser profile, receiver credentials, and owned MQTT discovery topic
inventory. Never upload that directory or real-browser test artifacts.

Backups use a cold snapshot so the browser profile can close before copying.
Restart/backup stops the active view; the app returns without automatic TV
playback. Sign-in and pairing are retained. Removing a saved item removes its
HA controls but retains browser/pairing files for recovery.

To stop a trial, use Stop and Close browser, then stop the app through HA.
For rollback after an update, retain app data and reinstall the previous app
version or restore its exact native backup. Do not delete the data directory or
re-pair receivers merely to troubleshoot.

## Build and verification

The Dockerfile builds the AirPlay engine and launcher from an exact, checksum-
verified source commit. The app's own controller and UI are built from this
directory. Linux CI validates synthetic content only; device pairing and HA
sign-in are separate deployment acceptance checks. See [PLAN.md](PLAN.md).
