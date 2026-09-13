FROM golang:1.25-bookworm AS engine
WORKDIR /src
# Supervisor builds only this app directory. Pin and verify the separately
# versioned capture engine instead of depending on files outside that context.
RUN curl -fsSL https://codeload.github.com/jtorrap/doubletake/tar.gz/eb893212c93244dfd47908b4c5025a4652a75b8e -o engine.tar.gz \
    && echo 'a974f137a820d84d745fd77390bf2f01d30359db20ac95ca61b00a92379ac251  engine.tar.gz' | sha256sum -c - \
    && tar -xzf engine.tar.gz --strip-components=1 \
    && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /doubletake ./cmd/doubletake

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       google-chrome-stable python3 python3-aiohttp python3-paho-mqtt python3-zeroconf \
       xvfb xauth xdotool x11vnc novnc fonts-dejavu-core fonts-liberation \
       gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
       gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin browser
COPY --from=engine /doubletake /usr/local/bin/doubletake
COPY --from=engine /src/scripts/doubletake-browser /opt/doubletake/launcher.py
COPY service/ /opt/browser-app/
LABEL io.hass.name="Doubletake Browser" io.hass.description="Interactive browser views over AirPlay" \
      io.hass.type="addon" io.hass.version="0.1.0" io.hass.arch="amd64" \
      org.opencontainers.image.source="https://github.com/jtorrap/doubletake"
WORKDIR /opt/browser-app
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/python3", "-u", "-B", "/opt/browser-app/entrypoint.py"]
