# Headless browser over AirPlay

This fork adds `doubletake-browser`: Chromium runs on its own authenticated
Xvfb display, and upstream doubletake captures only that browser's X11 window.
GStreamer encodes H.264 and doubletake sends AirPlay mirroring directly to the
selected receiver. There is no HLS server, attached monitor, desktop environment,
Wayland portal prompt, or custom Apple TV app in this path.

The initial implementation is **video only**. Playing video within the page and
JavaScript/WebSocket charts are rendered by Chromium. It does not send audio or
map the Siri Remote to browser controls. Resolution and smoothness depend on
the browser workload, encoder, network, and Apple TV; they must be measured on
the intended hardware. The launcher forces H.264 so a receiver's high-resolution
HEVC mode does not unexpectedly expand a VM's encoding workload.

## Linux VM preparation

Use Debian 12/13 or a comparable Linux VM with an ordinary unprivileged user,
Python 3, Go 1.25 or newer, and these packages:

```sh
sudo apt-get update
sudo apt-get install -y chromium python3 xvfb xauth xdotool x11vnc \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
make all test-browser
sudo make install
```

Ubuntu's `chromium-browser` may be a Snap launcher; use an installed native
Chromium-compatible browser and `--browser /path/to/browser` if necessary.
Keep Chromium's sandbox enabled. The launcher refuses to run as root by default.
`--no-browser-sandbox` is an explicit exception for isolated test fixtures, not
a remedy for failures loading a real authenticated dashboard.

For a first resource allocation, try 2–4 vCPUs and 4 GB RAM at 1080p/30, then
measure browser/encoder load; this is a starting estimate, not validated sizing.
GPU encoding is optional (`--hwaccel vaapi` or `nvenc` needs the corresponding
VM device access/drivers). `--hwaccel none` uses software x264. The VM needs
connectivity to the chosen Apple TV's AirPlay port and negotiated timing/video
ports, and to the dashboard. See upstream networking documentation.

## Configure and sign in privately

Choose a state directory outside the source checkout. It contains the Chromium
profile, pairing credentials, a process lock, and private status file. Keep it
on persistent storage with restricted permissions; never commit or upload it.

Put the full HTTP(S) dashboard address in a private file, for example
`~/.config/doubletake-browser/url` (mode 0600). The URL may contain a query or
fragment, but credentials in the URL's user/password fields are rejected.

```sh
doubletake-browser --url-file ~/.config/doubletake-browser/url --setup
```

Setup starts a VNC server bound **only to localhost**, does not contact a TV,
and uses the same persistent browser profile as streaming. From your Mac:

```sh
ssh -N -L 15900:127.0.0.1:5900 your-linux-vm
open vnc://localhost:15900
```

Sign in through Home Assistant's normal browser flow and check that the intended
dashboard, charts, and cameras are visible. Then stop the setup command with
Ctrl-C before streaming. No VNC service or remote debugging port is started in
normal streaming mode. The loopback VNC endpoint has no additional password;
use a dedicated VM/user and the SSH tunnel, and leave it running only for setup.
TLS errors are never bypassed; install the appropriate CA trust if required.

## Start and stop

```sh
doubletake-browser --url-file ~/.config/doubletake-browser/url \
  --target APPLE_TV_ADDRESS --fps 30 --bitrate 6000
```

The target must be explicit. Existing pairing credentials are reused. If the
receiver requires initial pairing, complete its normal PIN flow while running
interactively. Use `--pair` only when deliberately requesting new pairing, never
in a recurring service command. A configured receiver password can be supplied
through the existing `DOUBLETAKE_CODE` environment variable rather than process
arguments. Do not save PINs/passwords in shell history or application logs.

Ctrl-C stops the sender, Chromium, and the private display while retaining the
profile and pairing. Losing any managed process stops the session with a failure
status, so the service manager can retry. Two launchers cannot share one state
directory. No process outside this session is stopped or captured.

`--check` verifies arguments and executable dependencies without starting a TV
connection. `status.json` says `sender_started` when the process was launched;
it is **not proof that an Apple TV is displaying frames**. The status file
contains local display/window IDs and process IDs, but no dashboard URL, session
cookies, or pairing secrets. Chromium stderr is suppressed to avoid disclosing
authenticated URLs. Upstream debug logging is not enabled.

## Persistent service

After successful manual sign-in, pairing, and a real-TV trial, use
`deploy/doubletake-browser.service` as the system service template. Create a
dedicated `doubletake-browser` user, a private `/etc/doubletake-browser/url` file,
and `/etc/doubletake-browser/environment` containing `DOUBLETAKE_TARGET=...`.
Both files should be root-owned and readable only by the service group. Perform
setup/pairing as that same user with `--state-dir /var/lib/doubletake-browser` so
the service reuses the correct profile. The systemd `StateDirectory` is private
and persistent. Stop the service before running setup with that state directory.

The template retries process failures at most three times in five minutes;
a retry makes another playback attempt on the selected TV. Recovery after TV
sleep, switching apps, or a silent stale stream needs device testing before
enabling this service for unattended use. Disable/stop the service to
roll back; keep its state directory to preserve sign-in and pairing.

## Verification

```sh
make test test-browser all
python3 -B scripts/browser-smoke.py
```

The Linux smoke check serves a synthetic local page with a WebSocket-updated
chart and a looping H.264 video. It verifies both are advancing, captures the
exact browser window to a short video/PNG, and sends sustained video through
doubletake to upstream's test receiver. It also exercises VNC setup, verifies
cookies and local storage survive into streaming, and tests shutdown when
Chromium exits. The receiver validates protocol
traffic; it is not a substitute for Apple TV decode/display verification.

CI explicitly disables the browser sandbox for this credential-free synthetic
fixture only. Generated images, logs, and result metadata are available as the
`headless-browser-evidence` artifact. Do not run this artifact-upload workflow
against a real dashboard or authenticated browser profile.

### 2026-09-13 development record

- Upstream base: `ae06722` (full commit recorded in Git history).
- Added browser-only launcher, VM/service instructions, and Linux smoke check.
- The initial `xvfb-run` subprocess wrapper failed the setup shutdown check.
  The launcher now owns Xvfb directly, uses `-displayfd` for allocation, and
  supervises each process group explicitly with a private Xauthority cookie.
- Upstream uses Linux-specific process-death handling. Native macOS builds do
  not compile; Linux cross-compilation works, and execution tests run on Linux.
- GitHub workflow changes require an appropriate credential scope; an existing
  SSH credential for the fork's owner can be used instead of an OAuth token
  without workflow access.
- No Home Assistant configuration or Apple TV playback changed during development.
- Live HA sign-in, Apple TV playback, latency, and long-run recovery are pending.
