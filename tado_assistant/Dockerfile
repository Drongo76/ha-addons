ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-requests py3-paho-mqtt tzdata curl

COPY rootfs /
RUN chmod a+x /run.sh

# s6/init komplett umgehen -> /run.sh ist PID1
ENTRYPOINT ["/run.sh"]
