ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
  python3 \
  py3-requests \
  py3-flask \
  py3-gunicorn \
  py3-paho-mqtt \
  tzdata \
  curl

COPY rootfs /

# s6 service scripts executable machen
RUN chmod +x /etc/services.d/web/run

# s6 als PID1 (wichtig!)
CMD ["/init"]
