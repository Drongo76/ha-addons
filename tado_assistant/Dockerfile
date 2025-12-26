ARG BUILD_FROM
FROM ${BUILD_FROM}

# Pakete
RUN apk add --no-cache \
    python3 \
    py3-requests \
    py3-flask \
    py3-gunicorn \
    py3-paho-mqtt \
    tzdata \
    curl

# Rootfs (enthält /app.py, /worker.py und /etc/services.d/*)
COPY rootfs /

# s6-service scripts ausführbar machen
RUN chmod a+x \
    /etc/services.d/web/run \
    /etc/services.d/worker/run

# WICHTIG:
# KEIN CMD setzen. Base Image startet /init als ENTRYPOINT und startet s6-services automatisch.
