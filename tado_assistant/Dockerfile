ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache bash python3 py3-pip

RUN pip3 install --no-cache-dir flask

COPY rootfs/ /
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
