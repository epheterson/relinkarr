# relinkarr

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://ghcr.io/epheterson/relinkarr)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**Seed after importing. Any app, any system. Zero wasted disk.**

## The problem

You download a file. Something imports it to your media library — Sonarr, Radarr, Soulseek, a script, a manual copy. Now you have two copies of the same file. You can't delete the download because you're still seeding. Your disk usage doubles for no reason.

This happens every time an import copies instead of hardlinks. And [hardlinks have limitations](https://trash-guides.info/Hardlinks/Hardlinks-and-Instant-Moves/) — they can't cross filesystems, partitions, or subvolumes, and not every app creates them.

## The fix

relinkarr watches your downloads, finds duplicates in your media library, and replaces them with zero-cost links automatically. It doesn't matter what app downloaded the file or what imported it.

On Btrfs (Synology, etc.), it uses **reflinks** — copy-on-write clones that cross subvolume boundaries at zero disk cost. On the same filesystem, it uses **hardlinks**. Either way, the duplicate disappears and seeding continues.

## How it works

relinkarr watches your download client and compares against your media directories. It handles both ways files end up duplicated:

**Copied files** — download and media copy both exist (2x disk):

```
/downloads/movie.mkv           ← seeding this
/movies/Movie (2026)/movie.mkv ← imported copy, 2x disk usage

relinkarr replaces the download copy with a reflink → 0x extra disk
Delete the torrent later → reflink removed, media file untouched
```

**Moved files** — download is gone, can't seed:

```
/downloads/episode.mkv          ← was here, moved away
/tv/Show/S01E01.mkv             ← imported file lives here now

relinkarr restores a link at the original path → seeding works again
```

Either way: one real file, one zero-cost link.

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

When using a single `/volume1` mount, you'll need `PATH_MAP` if your download client's paths don't match:

```yaml
environment:
  - PATH_MAP=/downloads:/volume1/downloads
  - MEDIA_DIRS=/volume1/media/tv,/volume1/media/movies
volumes:
  - /volume1:/volume1
```

See [`docker-compose.nas.yml`](docker-compose.nas.yml) for a complete Synology example.

## Features

- **Crosses Btrfs subvolumes** — reflinks work where hardlinks can't
- **App-agnostic** — doesn't matter what downloaded or imported the files
- **Set and forget** — runs on a poll loop, deduplicates automatically as files appear
- **Safe** — renames to `.bak` before linking; restored automatically on failure
- **Persistent state** — tracks deduped files across restarts via `/config` volume
- **NAS-friendly** — cached media index avoids hammering spinning disks
- **Health check and status API** — `/health`, `/api/status`, `/api/history`
- **PUID/PGID support** — matches your media stack permissions

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
