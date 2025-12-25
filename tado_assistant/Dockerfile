ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
  bash \
  python3 \
  py3-flask \
  py3-requests \
  py3-paho-mqtt

COPY rootfs/ /
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
