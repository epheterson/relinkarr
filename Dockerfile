FROM python:3.12-alpine

RUN pip install --no-cache-dir requests \
    && apk add --no-cache su-exec coreutils

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app
COPY relinkarr.py .

EXPOSE 7585

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://localhost:7585/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
