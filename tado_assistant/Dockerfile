ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache \
    python3 \
    py3-requests \
    py3-flask \
    py3-paho-mqtt \
    py3-gunicorn \
    tzdata \
    curl

COPY rootfs /

RUN chmod a+x /run.sh

CMD ["/run.sh"]
