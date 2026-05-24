# relinkarr

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://ghcr.io/epheterson/relinkarr)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**Btrfs reflink dedup — keep seeding across subvolumes with zero duplicate disk usage.**

## Do you need this?

**Yes, if** your downloads and media are on separate Btrfs subvolumes (e.g., different Synology shared folders). Hardlinks can't cross subvolume boundaries, so any import — Sonarr, Radarr, manual copy, scripts, anything — falls back to a full copy. You end up with two copies of every file. relinkarr replaces the duplicate with a Btrfs reflink (zero disk cost), so your download client keeps seeding without the wasted space.

**No, if** your downloads and media are on the same filesystem or subvolume. Use [hardlink imports](https://trash-guides.info/Hardlinks/Hardlinks-and-Instant-Moves/) — they handle this already and you don't need an extra tool.

## How it works

relinkarr watches your download client for seeding files and detects when they've been imported — by Sonarr, Radarr, a script, a manual copy, or anything else that moves or copies files to a media library.

**Copy mode** (the default when hardlinks aren't possible):

```
1. Download client:      /downloads/movie.mkv
2. Import (any app):     /movies/Movie (2026)/Movie (2026).mkv  ← 2x disk usage
3. relinkarr detects:    same file in download dir and media library
4. relinkarr replaces:   download copy → reflink to media copy   ← 0x extra disk
5. Client keeps seeding ✓  Media library has the real file ✓
6. Delete torrent:       reflink removed, media library file untouched ✓
```

**Move mode** (when files are moved rather than copied):

```
1. Download client:      /data/torrents/episode.mkv
2. Import (any app):     /data/media/tv/Show/S01E01.mkv  (original gone)
3. relinkarr detects:    download file is missing
4. relinkarr restores:   hardlink at original path → media file
5. Client keeps seeding ✓  Media library has the real file ✓
```

Both modes end at the same place: one real file in the media library, one zero-cost link in the download directory.

## Synology NAS / Btrfs

This is the primary use case. Synology creates each shared folder as a separate Btrfs subvolume. If your downloads and media are in different shared folders (e.g., `/volume1/downloads` and `/volume1/media`), hardlinks won't work across them — but reflinks will, since they share the same Btrfs pool.

For reflinks to work inside Docker, mount at a level that encompasses both paths:

```yaml
# WON'T work — Docker sees two separate devices
volumes:
  - /volume1/downloads:/downloads
  - /volume1/media:/media

# WILL work — single mount, reflinks possible between subvolumes
volumes:
  - /volume1:/volume1
```

When using a single `/volume1` mount, you'll need `PATH_MAP` if qBit's paths don't match:

```yaml
environment:
  - PATH_MAP=/downloads:/volume1/downloads
  - MEDIA_DIRS=/volume1/media/tv,/volume1/media/movies
volumes:
  - /volume1:/volume1
```

See [`docker-compose.nas.yml`](docker-compose.nas.yml) for a complete Synology example.

The ideal long-term fix is to follow the [TRaSH Guides folder structure](https://trash-guides.info/Hardlinks/Hardlinks-and-Instant-Moves/) and put downloads + media in the same shared folder so hardlinks work natively. But if you can't restructure your shares, relinkarr bridges the gap.

## Features

- **Btrfs reflinks across subvolumes** — the thing hardlink imports can't do
- **Works with any download client and any import flow** — qBittorrent, Deluge, Transmission, manual copies, scripts, anything
- **Hardlink fallback** — tries hardlink first, falls back to `cp --reflink=always` for cross-subvolume
- **Safe rollback** — renames to `.bak` before linking; restored automatically on failure
- **Persistent state** — tracks deduped files across restarts via `/config` volume
- **Cached media index** — scans media directories once, reuses for 5 minutes; safe for large libraries on spinning disks
- **Health check and status API** — `/health`, `/api/status`, `/api/history`
- **PUID/PGID support** — matches your *arr stack permissions

## Quick start

### Docker Compose (recommended)

```yaml
services:
  relinkarr:
    image: ghcr.io/epheterson/relinkarr:latest
    container_name: relinkarr
    restart: unless-stopped
    ports:
      - 7585:7585
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - QBIT_URL=http://qbittorrent:8080
      - QBIT_USER=admin
      - QBIT_PASS=adminadmin
      - MEDIA_DIRS=/data/media/tv,/data/media/movies
      - POLL_INTERVAL=30
      # - LOG_LEVEL=DEBUG
    volumes:
      - relinkarr-config:/config
      - /data:/data

volumes:
  relinkarr-config:
```

### Standalone

```bash
pip install requests
QBIT_URL=http://localhost:8080 \
MEDIA_DIRS=/data/media/tv,/data/media/movies \
python relinkarr.py
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `0` | User ID for file operations (match your *arr stack) |
| `PGID` | `0` | Group ID for file operations |
| `TZ` | — | Timezone (e.g., `America/New_York`) |
| `QBIT_URL` | `http://localhost:8080` | qBittorrent Web UI URL (more clients coming soon) |
| `QBIT_USER` | `admin` | qBittorrent username |
| `QBIT_PASS` | `adminadmin` | qBittorrent password |
| `MEDIA_DIRS` | *(required)* | Comma-separated paths to search for imported files |
| `POLL_INTERVAL` | `30` | Seconds between checks |
| `PATH_MAP` | — | Path translation (see below) |
| `DEDUP` | `true` | Replace download copies with reflinks when media copy exists |
| `CONFIG_DIR` | `/config` | Directory for persistent state (dedup tracking survives restarts) |
| `PORT` | `7585` | Status server port (`0` to disable) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### PATH_MAP

If your download client sees paths differently than relinkarr (common when containers have different volume mounts), use `PATH_MAP` to translate:

```
PATH_MAP=/downloads:/volume1/downloads
```

This tells relinkarr: when the download client reports a file at `/downloads/pack/file.mkv`, look for it at `/volume1/downloads/pack/file.mkv` instead. Multiple mappings can be comma-separated.

### DEDUP

When enabled (the default), relinkarr detects files that exist in both the download directory and media library — the result of a copy during import. It replaces the download copy with a reflink to the media copy, freeing the duplicate disk usage while your client keeps seeding.

This is safe: the download file is renamed to `.bak` before the reflink is created. If the reflink fails, the `.bak` is restored — no data loss.

Set `DEDUP=false` to disable this and only handle missing files (move mode).

## Link types

| Method | Requirement | Disk cost | How it works |
|--------|-------------|:-:|---|
| **Hardlink** | Same filesystem + subvolume | Zero | Same inode, two directory entries |
| **Reflink** | Same Btrfs/XFS pool (can cross subvolumes) | Zero | Copy-on-write clone, separate inode |

relinkarr tries hardlink first. If that fails (cross-subvolume), it falls back to `cp --reflink=always`. If neither works, it logs an error and skips the file.

## File matching

relinkarr uses three strategies to find media files:

1. **Inode match** (preferred) — same-subvolume moves preserve the inode number
2. **Size + filename match** — cross-subvolume copies create a new inode, so relinkarr falls back to matching by exact file size and filename
3. **Size-only match** — if there's exactly one file with the same size (and different name), it's used as a last resort

## Status server

relinkarr runs a lightweight HTTP server for health checks and monitoring.

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check — returns `200` if the download client is connected and media dirs are accessible, `503` if degraded |
| `GET /api/status` | Stats — tracked files, relinked/deduped counts, cycles, uptime |
| `GET /api/history` | Recent activity log — last 500 relink/dedup/rollback events |

The Docker image includes a `HEALTHCHECK` that hits `/health` every 30 seconds.

```bash
# Quick check
curl http://localhost:7585/health

# Full stats
curl http://localhost:7585/api/status

# What has relinkarr done?
curl http://localhost:7585/api/history
```

Set `PORT=0` to disable the status server entirely.

## Building

```bash
docker build -t relinkarr .
```

The image is based on `python:3.12-alpine` with `requests`, `su-exec`, and `coreutils` (for `cp --reflink`).

## License

MIT

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.ai/code).
