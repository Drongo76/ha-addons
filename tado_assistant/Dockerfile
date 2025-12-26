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

# Deine Struktur ist rootfs/ -> wird nach / kopiert:
# - /app.py
# - /worker.py
# - /static/...
# - /etc/services.d/web/run ...
COPY rootfs /

# s6 run-scripts ausführbar machen
RUN chmod +x /etc/services.d/web/run || true

# WICHTIG: kein CMD ["/init"] setzen.
# Base Image hat ENTRYPOINT=/init, CMD bleibt leer.
