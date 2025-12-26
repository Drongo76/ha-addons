ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Basis-Tools + Python (für app/worker)
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-setuptools \
    py3-wheel \
    tzdata \
    curl

# Add-on rootfs (run.sh, app.py/worker.py, s6/cont-init.d falls vorhanden)
COPY rootfs /

# run.sh ausführbar machen
RUN chmod a+x /run.sh

CMD ["/run.sh"]
