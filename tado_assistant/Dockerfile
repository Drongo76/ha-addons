ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install runtime deps
RUN apk add --no-cache \
    python3 \
    py3-requests \
    py3-flask \
    py3-gunicorn \
    tzdata \
    curl

# Optional MQTT (nur wenn du MQTT wirklich nutzt)
# RUN apk add --no-cache py3-paho-mqtt

# Rootfs files (run script, static, etc.)
COPY rootfs /

# App sources (liegen bei dir neben Dockerfile)
COPY app.py /app.py
COPY worker.py /worker.py

RUN chmod a+x /run.sh

CMD ["/run.sh"]
