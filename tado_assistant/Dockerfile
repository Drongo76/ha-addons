ARG BUILD_FROM=ghcr.io/hassio-addons/base-python:3.11
FROM ${BUILD_FROM}

RUN pip install --no-cache-dir flask requests waitress paho-mqtt

COPY rootfs /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
