# Browser control experiment

This experiment compares the deployed Chrome debugging pipe with a nonzero
loopback debugging port and two browser modes without a debugging channel.
It changes no production service code, launcher pin, or app version.

Run the **Browser control experiment** GitHub Actions workflow on
`codex/browser-control-experiment`. It builds the app image and runs the fixture
as uid 1000 with Chrome's sandbox enabled, an isolated Xvfb display, disposable
profiles, and Docker `--network none`. Only the container's loopback fixture is
reachable. No Home Assistant profile, TV, Google account, or password is used.
The `browser-control-evidence` artifact contains a JSON comparison and synthetic
X11 screenshots. The app's existing Linux CI is still required before deployment
of any resulting runtime changes.

Chrome 153 explicitly enables `AutomationControlled` for `--remote-debugging-pipe`,
`--headless`, `--enable-automation`, and debugging port zero. Its source explicitly
leaves that feature unset for a specified nonzero debugging port:
[Chromium runtime features](https://raw.githubusercontent.com/chromium/chromium/153.0.8010.36/content/child/runtime_features.cc).
This experiment does not override `navigator.webdriver`, patch browser features,
or use stealth scripts. The test page reports its own browser properties over
HTTP; no debugger is attached to either manual mode.

Google lists software-controlled browsers among possible reasons for refusing
sign-in: [Google Account Help](https://support.google.com/accounts/answer/7675428).
A false `navigator.webdriver` result does not establish Google acceptance or
make a software-controlled browser exempt from that policy. Real sign-in needs
a separate user-operated test in the intended environment.

## Candidates and decision gates

1. **Current pipe:** baseline. Retains CDP navigation, text insertion and video
   diagnostics, but Chrome explicitly enables automation mode.
2. **Specified debugging port:** retains CDP features without that startup flag.
   This is still a remotely controlled browser. The deployed app uses host
   networking; a loopback debugger would be reachable by other host-network
   processes/apps. Do not deploy this prototype listener there without an
   isolation design. The current private pipe has no such network listener.
3. **Manual app window:** remove only the pipe; preserve app/kiosk presentation.
   Test whether native controls suffice before assuming Ctrl+L is available.
4. **Manual full-screen Chrome:** no debugging endpoint, ordinary browser window,
   native remote-desktop input. Test navigation, ASCII/Unicode input, close/reopen,
   profile retention, dark mode, 120% zoom and X11 capture. Detailed DOM/CDP video
   diagnostics would need an explicitly separate diagnostic session.

Record measured results below after the workflow completes. Any implementation
must preserve the persistent profile, avoid concurrent access to it, keep the
masked Paste dialog and its immediate clearing, prevent control races, and leave
the existing GPU/capture configuration intact. Synthetic cookie retention tests
profile persistence; it does not prove an actual Google login will persist.

## Measured results — 2026-09-13

Completed [Linux run 34780911773](https://github.com/jtorrap/doubletake/actions/runs/34780911773)
at source `fbc802c26da49dfcafb38c6fca7a7aeb2a613bb0`, using Google Chrome
153.0.8010.36. Download `browser-control-evidence` for the full JSON and images.
The workflow completes successfully when every mode can be measured; individual
false results in the matrix are expected experimental findings, not hidden passes.

| Mode | `navigator.webdriver` | Text and mouse | Navigation | Clean close and profile retention |
| --- | --- | --- | --- | --- |
| Current pipe + app window | true, including after CDP commands | Native and CDP pass | CDP passes; Ctrl+L fails | Pass |
| Specified port + app window | false, including after CDP commands | Native and CDP pass | CDP passes; Ctrl+L fails | Pass |
| No debugger + app window | false | Native passes | Ctrl+L fails | Pass |
| No debugger + normal full-screen window | false | Native passes | Exit full screen, Ctrl+L, enter URL, restore full screen passes | Pass |

All four modes reported dark preference, 120% zoom, and a 1600 by 900 CSS viewport
inside 1920 by 1080 output both before and after reopening. Synthetic text entry
preserved ASCII punctuation, accents, Greek, Japanese, and emoji. Native reload,
X11 capture and Ctrl+Shift+W close passed, with Chrome recording a Normal exit.
Persistent synthetic cookies and localStorage survived each reopen. The final
normal-window test also passed native Back and Forward.

Direct Ctrl+L failed while full screen. The normal-window test measured F11 exit,
navigation with the toolbar visible, and F11 restoration separately. Chromium
disables location-bar focus when that UI is hidden:
[browser command controller](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/ui/browser_command_controller.cc).
Full-screen transitions can briefly display Chrome UI; the test image captured
immediately afterward also contains a top overlay. Capture stabilization still
needs verification. This is not yet a polished streaming navigation implementation.

The first run exposed two fixture issues: `xdotool mousemove --sync` can wait
indefinitely when the pointer is already at its destination, and private files
created by container uid 1000 were unreadable by the artifact uploader. The
fixture now uses ordered move/click without that wait, and the workflow returns
only its synthetic evidence directory to the runner's ownership. A second run
completed the initial matrix; the final run added the full-screen sequence.

## Recommended implementation plan

Prefer normal Chrome on the existing virtual X11 display with **no debugging
endpoint**. The VM can remain headless; Chrome itself is a regular windowed
browser. Keep the existing GPU wrapper, capture engine and persistent profile.
Implement this behind a reversible browser-control setting first:

1. Add a native-control backend beside the current private-pipe backend. Launch
   without `--remote-debugging-pipe`, `--headless`, or `--enable-automation`; use
   a normal full-screen window instead of app/kiosk mode. Keep the sandbox on.
2. Route Paste through bounded UTF-8 stdin to native input, preserving current
   single-line validation, no automatic Enter, masked dialog, and immediate
   clearing. The experiment validates this transport; the existing Paste UI
   has not yet been connected to it. Serialize commands and prevent concurrent
   VNC input from redirecting keystrokes midway through an operation.
3. Implement native Back/Forward/Reload and verified full-screen/toolbar
   navigation. Do not type a URL until browser focus and the toolbar state are
   confirmed. If confirmation fails, stop that action. Suppress or wait out the
   transient browser UI in the outgoing capture without unnecessarily ending
   the AirPlay connection. That capture behavior still needs implementation.
4. Close Chrome gracefully before changing modes, then reopen the same profile.
   Never copy live profiles, edit a running profile, clear cookies, or allow two
   processes to open it. Preserve saved pages, receiver credentials, and exact
   current page/receiver state across an explicitly requested switch.
5. Keep OS/driver GPU checks available. Detailed DOM and CDP media diagnostics
   need a separate explicit diagnostic mode; attaching the current debugging
   pipe would re-enable automation mode. Do not silently reintroduce it.
6. Run the full app integration suite for the new backend: actual noVNC Paste,
   navigation/input races, video and real-time updates, one sender, cleanup and
   profile retention. Then deploy an opt-in trial and let the user test Google
   sign-in on the HA host. Verify actual HEVC decoding and TV behavior there.

The port backend is a smaller code change but has both the network-listener
tradeoff and Google's software-control uncertainty. Merely removing the pipe
from app/kiosk mode is usable for a limited manual sign-in trial, but lacks a
working native address bar for the app's named-page controls. Neither alternative
is preferable to the tested normal-window path without another concrete need.

No production app code, live browser, profile, account setting, or TV connection
was changed during this experiment. The deployed app remains version 0.1.5.
