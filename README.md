# relinkarr

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://ghcr.io/epheterson/relinkarr)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**Keep seeding after Sonarr/Radarr imports your files. Zero duplicate disk usage.**

A companion tool for the *arr stack that watches qBittorrent and replaces duplicate download files with zero-cost hardlinks or Btrfs reflinks to the media library copy. qBit keeps seeding, you reclaim the disk space.

## Why

Every *arr user hits this: Sonarr or Radarr imports a file, and now you have two copies — one in your media library, one in your download directory where qBit is still seeding it. You're choosing between wasting disk space (Copy mode) or breaking your seeds (Move mode).

relinkarr eliminates the trade-off. It detects the duplicate and replaces the download copy with a link to the media library file. One real file, one zero-cost link, qBit never notices.

| Without relinkarr | With relinkarr |
|---|---|
| **Copy**: 2x disk until you clean up | Link replaces the duplicate — 0x extra |
| **Move**: qBit errors, seed lost | Link restores the download path — seed continues |
| **Hardlink**: works, but download dir copy can't be cleaned up | Media library owns the file, download link is ephemeral |

## How it works

**Copy mode** (most common — Sonarr/Radarr's default for cross-filesystem setups):

```
1. qBit downloads:       /downloads/movie.mkv
2. Radarr copies to:     /movies/Movie (2026)/Movie (2026).mkv  ← 2x disk usage
3. relinkarr detects:    duplicate in download dir and media library
4. relinkarr replaces:   download copy → reflink to media copy   ← 0x extra disk
5. qBit keeps seeding ✓  Media library has the real file ✓
6. Delete torrent:       reflink removed, media library copy persists ✓
```

**Move mode** (same filesystem, or when explicitly configured):

```
1. qBit downloads:       /data/torrents/episode.mkv
2. Sonarr moves to:      /data/media/tv/Show/S01E01.mkv  (original gone)
3. relinkarr detects:    download file is missing
4. relinkarr restores:   hardlink at original path → media file
5. qBit keeps seeding ✓  Media library has the real file ✓
```

Both modes end at the same place: one real file in the media library, one zero-cost link in the download directory.

## Features

- **Works with any import mode** — Copy, Move, or Hardlink
- **Hardlinks and reflinks** — tries hardlink first, falls back to Btrfs/XFS reflink for cross-subvolume setups
- **Safe rollback** — dedup renames to `.bak` before linking; restored automatically on failure
- **Persistent state** — tracks deduped files across restarts via `/config` volume
- **Cached media index** — scans media directories once, reuses for 5 minutes; safe for large libraries on spinning disks
- **Health check and status API** — `/health`, `/api/status`, `/api/history` endpoints
- **Docker HEALTHCHECK** built in
- **PUID/PGID support** — matches your *arr stack permissions
- **Synology / Btrfs native** — reflinks work across subvolumes with a single volume mount

## Quick start

### Docker Compose (recommended)

Add relinkarr alongside your existing *arr stack. It needs to see the same filesystem paths as qBittorrent.

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
      # Mount the same /data volume as your *arr stack and qBittorrent.
      # All containers must see the same filesystem for hardlinks to work.
      - /data:/data

volumes:
  relinkarr-config:
```

This follows the [TRaSH Guides](https://trash-guides.info/Hardlinks/Hardlinks-and-Instant-Moves/) recommended folder structure where downloads and media live under a single `/data` root.

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
| `QBIT_URL` | `http://localhost:8080` | qBittorrent Web UI URL |
| `QBIT_USER` | `admin` | qBit username |
| `QBIT_PASS` | `adminadmin` | qBit password |
| `MEDIA_DIRS` | *(required)* | Comma-separated paths to search for moved files |
| `POLL_INTERVAL` | `30` | Seconds between checks |
| `PATH_MAP` | — | Path translation (see below) |
| `DEDUP` | `true` | Replace download copies with reflinks when media copy exists (see below) |
| `CONFIG_DIR` | `/config` | Directory for persistent state (dedup tracking survives restarts) |
| `PORT` | `7585` | Status server port (`0` to disable) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### PATH_MAP

If qBittorrent sees paths differently than relinkarr (common when containers have different volume mounts), use `PATH_MAP` to translate:

```
PATH_MAP=/downloads:/volume1/downloads
```

This tells relinkarr: when qBit reports a file at `/downloads/pack/file.mkv`, look for it at `/volume1/downloads/pack/file.mkv` instead. Multiple mappings can be comma-separated.

### DEDUP

When enabled (the default), relinkarr detects files that exist in both the download directory and media library — the result of Sonarr/Radarr copying during import. It replaces the download copy with a reflink to the media copy, freeing the duplicate disk usage while keeping qBit seeding.

This is safe: the download file is renamed to `.bak` before the reflink is created. If the reflink fails, the `.bak` is restored — no data loss.

Set `DEDUP=false` to disable this and only handle missing files (move mode).

## *arr app configuration

1. **Any import mode works.** Copy, Move, or Hardlink — relinkarr handles all of them. With Copy (the default for cross-filesystem setups), dedup mode eliminates the duplicate. With Move, the missing-file detection kicks in.
2. **Completed Download Handling → Remove: OFF** — if Sonarr/Radarr removes the torrent from qBit after import, there's nothing left to relink
3. **Same filesystem**: downloads and media should share a filesystem (or at least a Btrfs/XFS pool) so hardlinks or reflinks work

## Link types

| Method | Requirement | Disk cost | How it works |
|--------|-------------|:-:|---|
| **Hardlink** | Same filesystem / subvolume | Zero | Same inode, two directory entries |
| **Reflink** | Same Btrfs/XFS pool | Zero | Copy-on-write clone, separate inode |

relinkarr tries hardlink first. If that fails (cross-subvolume), it falls back to `cp --reflink=always`. If neither works, it logs an error and skips the file.

## File matching

relinkarr uses three strategies to find media files:

1. **Inode match** (preferred) — same filesystem moves preserve the inode number
2. **Size + filename match** — cross-subvolume moves create a new inode, so relinkarr falls back to matching by exact file size and filename
3. **Size-only match** — if there's exactly one file with the same size (and different name), it's used as a last resort

## Synology NAS / Btrfs

Synology creates each shared folder as a separate Btrfs subvolume. If your downloads and media are in different shared folders, hardlinks won't work across them — but reflinks will, since they share the same Btrfs pool.

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

The ideal long-term fix is to follow the [TRaSH Guides folder structure](https://trash-guides.info/Hardlinks/Hardlinks-and-Instant-Moves/) and put downloads + media in the same shared folder so hardlinks work natively.

## How is this different from Sonarr's hardlink support?

Sonarr can create hardlinks during import, but the hardlink is at the *destination* (media library) pointing back to the download directory. The download dir copy is the "real" one and can't be cleaned up while seeding.

relinkarr flips this: the media library has the real file, and relinkarr creates a zero-cost link back at the download path. With Copy mode (the most common setup), it replaces the duplicate download copy with a reflink. With Move mode, it creates a hardlink at the original path. Either way, the download path has an ephemeral link that disappears when you stop seeding.

## Status server

relinkarr runs a lightweight HTTP server for health checks and monitoring.

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check — returns `200` if qBit is connected and media dirs are accessible, `503` if degraded |
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
