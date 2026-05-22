#!/bin/sh
PUID=${PUID:-0}
PGID=${PGID:-0}

if [ "$PUID" -ne 0 ]; then
    GRP=$(getent group "$PGID" | cut -d: -f1)
    if [ -z "$GRP" ]; then
        addgroup -g "$PGID" relinkarr
        GRP=relinkarr
    fi
    EXISTING_UID=$(id -u relinkarr 2>/dev/null)
    if [ -n "$EXISTING_UID" ] && [ "$EXISTING_UID" != "$PUID" ]; then
        deluser relinkarr 2>/dev/null
        EXISTING_UID=""
    fi
    if [ -z "$EXISTING_UID" ]; then
        adduser -D -u "$PUID" -G "$GRP" relinkarr
    fi
    chown relinkarr:$GRP /config 2>/dev/null || true
    exec su-exec relinkarr python3 -u /app/relinkarr.py
else
    exec python3 -u /app/relinkarr.py
fi
