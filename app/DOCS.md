# Using Doubletake Browser

In **Settings → Apps → App store → Repositories**, add
`https://github.com/jtorrap/doubletake`. Install **Doubletake Browser**.
The initial source installation compiles the pinned sender and downloads Chrome,
so allow several minutes for the Supervisor build job to finish.

Install and start the app, then choose **Open web UI** or use its sidebar entry.
The app uses your existing Home Assistant authentication through Ingress.
It obtains the MQTT broker connection from Supervisor automatically.

## Pages, TVs, and login

Add a named page such as **Home Assistant** with its normal dashboard URL.
Add a TV with its IP/hostname and AirPlay port, or choose **Find Apple TVs**.
One browser can send the same page and its audio to several TVs at once.
Every connected TV sees the same browser interactions. Independent pages per TV
and synchronized multiroom audio are not supported.

Choose a page and **Open browser** to sign in. Click inside the preview to type;
mouse, keyboard, and scrolling operate the remote browser. Use Back, Forward,
Reload, or Expand in the preview toolbar. The persistent browser profile retains
cookies and local storage when the browser closes cleanly.

To paste a password, click its field inside the preview, then click **Paste** in
the preview toolbar. Paste into the masked **Text to paste** field with ⌘V or
Ctrl+V and choose **Send text**. This types into the selected browser field without
submitting the form. The dialog clears when sent or closed. Text is sent through
authenticated Ingress and the private browser connection; it is not written to
app settings, logs, MQTT, or the remote clipboard.

Check the TVs you want and choose **Show on TVs**. This applies exactly the
checked set: unchecked TVs disconnect. Changing checkboxes alone has no effect
on playback, and refreshing connection status preserves your pending selection.
When the app first opens, the checkboxes reflect the current connections.

Each TV has its own video and audio status, error, and **Stop** button. Audio
problems are shown even when video keeps sending. **Audio active** means the
capture path has started; confirm audible playback on the TV. If a receiver
requests pairing, choose **Enter code** beside that TV; the dialog names the
receiver. Enter its displayed code or configured AirPlay password. Codes are
passed privately and are not saved in settings or logs. The input clears on
submission or closing. Pairing credentials are stored separately for each TV.

Show preserves your current interaction when the same saved page is selected.
**Open browser** navigates to the saved URL. A Home Assistant launch button adds
its TV to the connected set and selects its saved page. Choosing a different
page changes the shared browser on every connected TV.

**Stop all** disconnects every TV and keeps the browser open. **Close browser**
also closes the browser. Stopping one TV leaves the others connected. A receiver
error requires another explicit Show action; the app does not repeatedly reclaim
a TV or stop the other receivers when one connection fails.

Use the page's normal sign-in flow and trusted certificate configuration. The
app does not bypass certificate errors or disable Chromium's sandbox.

Version 0.1.6 defaults to **browser_control: native** in the app Configuration
tab. This runs regular Chrome on the virtual display without a debugging channel.
Paste uses native keyboard input. Saved-page navigation briefly holds the current
frame while Chrome opens its address bar and returns to full screen; preview
input is temporarily disabled during native control operations.

The **diagnostic** setting restores the previous private debugging pipe for
troubleshooting. Changing modes requires an app restart and retains the same
browser profile, saved pages and receiver pairings. Google may reject automated
browsers; native mode removes Chrome's automation flag but does not guarantee
that a website accepts sign-in. Complete sign-in yourself through the preview.

## Home Assistant devices and automations

Each saved TV appears through MQTT discovery as **TV name Browser**. It has:

- **Show page name** buttons for each saved page.
- **Stop**, scoped to that TV. Other TVs keep playing.
- **Status** and **Page** sensors.

Select the matching button in an automation's **Perform action → Button: Press**
action. This binds a saved URL and a saved TV without placing URLs or credentials
in an automation. For example, pressing Basement Browser's **Show Home Assistant**
button adds that receiver and selects the shared page. Launching the same page
on another TV retains the browser's current interaction. The actual entity ID is assigned by HA;
renaming a page or TV preserves its stable discovery identity.

The sending status indicates a negotiated sender connection. It does not prove
the physical TV is displaying a healthy page. Use the preview plus a real-TV
check when setting up a dashboard.

MQTT commands are not retained, and retained commands received after reconnect
are ignored. A Home Assistant restart republishes discovery and current state
without starting playback.

## Quality and resource usage

The HA app Configuration tab offers 1080p at 15 or 30 fps, or 720p at 30 fps.
Changing this option requires an app restart. The default is 1080p at 30 fps.
The preview footer shows the configured output size and frame-rate target;
actual playback depends on rendering, encoding, and the network. Measure CPU
load while displaying your actual cameras and charts, especially with several TVs.

Each connected TV shows measured **fps sent** and capture-to-send time. These count
encoded pictures written to the connection, not unique pictures or frames displayed
by the TV. **Check video** includes five-second timing windows, late-frame counts
and CPU usage by role (100% is one CPU core). Pause the preview to stop its extra
capture/network work while the browser and TVs keep playing; Resume restores input.

**hardware_encoding** defaults to true. Before starting a sender, the app exercises
Intel's VA-API H.264 encoder at the configured size. If the probe fails, it uses
software encoding. An Intel sender that exits during startup gets one software
retry. Changing the option requires an app restart and retains sign-ins/pairings.

**display_backend: auto** uses Xvnc with DRI3 on an accessible GPU, allowing
Chrome to render without Xvfb's synchronous GPU-to-CPU presentation path.
If startup or the DRI3 check fails, it returns to Xvfb. **xvfb** selects the
previous backend; **xvnc** requires the new one. Changing this setting restarts
the app. Both use a private authenticated X11 display and the same preview;
Xvnc's TCP listener is disabled; its required unused Unix socket stays inside
the private runtime with a separate random password. Check video reports the backend,
DRI3 support and display renderer. Native sign-in mode remains unchanged.

The browser always reports a dark color preference and defaults to 120% page
zoom. Websites with automatic dark themes use that preference; a website's
explicit theme setting can still take precedence. Per-site zoom overrides are
retained. At 1080p, the output remains 1920 by 1080 pixels and the default page
layout uses approximately 1600 by 900 CSS pixels. **Check video** includes the
actual output resolution. Detailed zoom and color-preference readback is available
in diagnostic mode; standard mode does not attach a debugger to inspect pages.

Hardware decoding is enabled by default when an accessible GPU is present.
The app includes Intel's iHD VA-API driver and uses Supervisor's video device
mapping. The non-root browser receives the device's existing group permissions;
host device permissions are not changed. A VM must have its GPU passed through.
The **hardware_decoding** option disables this path for troubleshooting and
requires an app restart. HEVC needs compatible GPU, driver, and browser support.

While a camera is playing, choose **Check video** below the preview. Available
profiles establish capability; **GPU video engine active** establishes combined
decoding/encoding activity during the sample, not smooth browser presentation.
Technical details include video frame counters, browser
decoder properties in diagnostic mode, and container-local DRM activity. If activity
cannot be observed, the check reports that it is unconfirmed. The check does not
return page URLs, login fields, cookies, or raw browser logs.

Browser audio is enabled by default and plays on the selected TVs. The interactive
preview is silent. Use normal page playback and mute controls to choose what is
heard; the app's **audio** Configuration option switches TV audio off and requires
an app restart. Each receiver has its own AirPlay connection, so audio timing may
differ between TVs.

The app uses Intel or software H.264 encoding, an amd64 Google Chrome build with video
codecs, Xvfb, and the existing doubletake AirPlay implementation. Host networking supports receiver discovery and the
negotiated AirPlay ports. Ingress accepts only Supervisor's gateway; VNC is
password-protected and loopback-only. No debugging port is exposed.

Chrome's sandbox requires the container's `SYS_ADMIN` capability to create its
namespaces under Docker's syscall policy. The controller and browser run as uid
1000. Supervisor protection and AppArmor remain enabled; no host filesystem,
Docker socket, or host PID namespace is mapped.

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
