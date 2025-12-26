ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache \
    python3 \
    py3-requests \
    py3-flask \
    py3-gunicorn \
    py3-paho-mqtt \
    tzdata \
    curl

# s6 / services / init scripts
COPY rootfs /

# WICHTIG: App-Code liegt neben Dockerfile, also explizit mitkopieren
COPY app.py /app.py
COPY worker.py /worker.py
COPY static /static

# Falls du /run.sh in rootfs nutzt:
RUN chmod a+x /run.sh || true
