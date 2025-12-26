ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Python + benötigte Libraries (Flask UI, Requests API, optional MQTT)
RUN apk add --no-cache \
    python3 \
    py3-requests \
    py3-flask \
    py3-paho-mqtt \
    tzdata \
    curl

# Add-on rootfs (run.sh, app.py/worker.py)
COPY rootfs /

# run.sh ausführbar machen
RUN chmod a+x /run.sh

CMD ["/run.sh"]
