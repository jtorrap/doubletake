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
