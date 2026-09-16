FROM golang:1.25-bookworm AS engine
WORKDIR /src
RUN echo 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian bookworm non-free' > /etc/apt/sources.list.d/fdk-aac.list \
    && apt-get update && apt-get install -y --no-install-recommends libfdk-aac-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
# Supervisor builds only this app directory. Pin and verify the separately
# versioned capture engine instead of depending on files outside that context.
RUN curl -fsSL https://codeload.github.com/jtorrap/doubletake/tar.gz/5d78d014d3302f368741fb59b6b037370ba6391e -o engine.tar.gz \
    && echo '2c6cffa9f3f9df52c29119df1f68707986a966db82f3e001ca9a488482171066  engine.tar.gz' | sha256sum -c - \
    && tar -xzf engine.tar.gz --strip-components=1 \
    && CGO_ENABLED=1 go test -tags fdk_aac ./internal/airplay -run '^TestFDKAACELDEncodesRawFrame$' \
    && CGO_ENABLED=1 go build -tags fdk_aac -trimpath -ldflags='-s -w' -o /doubletake ./cmd/doubletake

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       google-chrome-stable python3 python3-aiohttp python3-paho-mqtt python3-zeroconf \
       xvfb xauth xdotool x11vnc novnc fonts-dejavu-core fonts-liberation \
       dbus-x11 at-spi2-core python3-pyatspi python3-xlib python3-pycryptodome \
       pulseaudio pulseaudio-utils \
       gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
       gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin browser
RUN echo 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian bookworm non-free' > /etc/apt/sources.list.d/intel-media.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       intel-media-va-driver-non-free libva-drm2 vainfo libgl1-mesa-dri libegl-mesa0 mesa-vulkan-drivers libfdk-aac2 \
    && rm -rf /var/lib/apt/lists/*
# Upstream's distribution-neutral Xvnc supports render-node DRI3 on headless
# hosts. Its TCP listener is disabled; x11vnc keeps the existing preview.
RUN apt-get update && apt-get install -y --no-install-recommends \
       libgbm1 libxcvt0 libxshmfence1 libgnutls30 libnettle8 libhogweed6 \
       libgmp10 libpam0g libsystemd0 libxcb-present0 x11-utils mesa-utils \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL --retry 2 https://downloads.sourceforge.net/project/tigervnc/stable/1.16.2/tigervnc-1.16.2.x86_64.tar.gz -o /tmp/tigervnc.tar.gz \
    && echo '5b70c84baefc09a030cfc78315c34ccb55b2a0dde4092b7da67a1962c5f0dea6  /tmp/tigervnc.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/tigervnc.tar.gz -C /tmp tigervnc-1.16.2.x86_64/usr/bin/Xvnc tigervnc-1.16.2.x86_64/usr/bin/vncpasswd tigervnc-1.16.2.x86_64/usr/share/doc/tigervnc \
    && install -m 755 /tmp/tigervnc-1.16.2.x86_64/usr/bin/Xvnc /usr/local/bin/Xvnc \
    && install -m 755 /tmp/tigervnc-1.16.2.x86_64/usr/bin/vncpasswd /usr/local/bin/tigervncpasswd \
    && cp -R /tmp/tigervnc-1.16.2.x86_64/usr/share/doc/tigervnc /usr/share/doc/ \
    && ldd /usr/local/bin/Xvnc > /tmp/xvnc-libraries \
    && ! grep -q 'not found' /tmp/xvnc-libraries \
    && rm /tmp/tigervnc.tar.gz /tmp/xvnc-libraries \
    && rm -rf /tmp/tigervnc-1.16.2.x86_64 \
    && install -d -m 1777 /tmp/.X11-unix
COPY --from=engine /doubletake /usr/local/bin/doubletake
COPY --from=engine /src/scripts/doubletake-browser /opt/doubletake/launcher.py
COPY service/ /opt/browser-app/
RUN chmod 755 /opt/browser-app/acceleration.py
ENV DOUBLETAKE_BROWSER=/opt/browser-app/acceleration.py
LABEL io.hass.name="Doubletake Browser" io.hass.description="Interactive browser views over AirPlay" \
      io.hass.type="addon" io.hass.version="0.1.9" io.hass.arch="amd64" \
      org.opencontainers.image.source="https://github.com/jtorrap/doubletake"
WORKDIR /opt/browser-app
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/python3", "-u", "-B", "/opt/browser-app/entrypoint.py"]
