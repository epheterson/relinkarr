#!/usr/bin/env python3
"""
relinkarr — Keep seeding after Sonarr/Radarr imports your files.

Watches qBittorrent for seeded files, detects when Sonarr/Radarr copies or
moves them, and replaces the download directory copy with a zero-cost
hardlink or reflink. qBit keeps seeding, zero duplicate disk usage.
"""

__version__ = "0.1.0"

import errno
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

import requests

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("relinkarr")

SHUTDOWN = False

API_TIMEOUT = 15
START_TIME = time.time()
STATS_LOCK = Lock()


def handle_signal(_sig, _frame):
    global SHUTDOWN
    log.info("Shutdown requested")
    SHUTDOWN = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


class ActivityLog:
    def __init__(self, maxlen=500):
        self.entries = deque(maxlen=maxlen)

    def record(self, action, source, target, link_type=None, error=None):
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "action": action,
            "source": os.path.basename(source),
            "source_path": source,
            "target_path": target,
        }
        if link_type:
            entry["link_type"] = link_type
        if error:
            entry["error"] = str(error)
        self.entries.append(entry)

    def recent(self, n=50):
        return list(self.entries)[-n:]


activity = ActivityLog()


class QbitClient:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.username = username
        self.password = password
        self.connected = False
        self._login()

    def _login(self):
        r = self.session.post(
            f"{self.url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=API_TIMEOUT,
        )
        if r.status_code >= 400:
            self.connected = False
            raise RuntimeError(f"qBit login failed: HTTP {r.status_code}")
        if not any("SID" in c.name for c in self.session.cookies):
            self.connected = False
            raise RuntimeError("qBit login failed: no session cookie returned")
        self.connected = True
        log.info("Connected to qBittorrent at %s", self.url)

    def get_seeding_torrents(self):
        torrents = []
        for status in ("seeding", "stalledUP"):
            r = self.session.get(
                f"{self.url}/api/v2/torrents/info",
                params={"filter": status},
                timeout=API_TIMEOUT,
            )
            r.raise_for_status()
            torrents.extend(r.json())
        seen = set()
        return [
            t for t in torrents if t["hash"] not in seen and not seen.add(t["hash"])
        ]

    def get_torrent_files(self, torrent_hash):
        r = self.session.get(
            f"{self.url}/api/v2/torrents/files",
            params={"hash": torrent_hash},
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()


class FileTracker:
    """Tracks inodes and sizes for all files in seeded torrents."""

    def __init__(self, state_file=None):
        self.tracked = {}  # absolute_path -> (inode, size)
        self.failed = set()  # (inode, size) tuples we already searched for
        self.deduped = set()  # paths confirmed deduped (persistent)
        self.no_match = (
            set()
        )  # paths with no media match (ephemeral, retried periodically)
        self._state_file = state_file
        if state_file:
            self._load_state()

    def update(self, path):
        try:
            st = os.stat(path)
            key = (st.st_ino, st.st_size)
            self.tracked[path] = key
            self.failed.discard(key)
            return key
        except FileNotFoundError:
            return None

    def classify(self, media_dirs):
        """Single pass: find missing files and dedup candidates."""
        missing = {}
        dupes = {}
        for path, key in list(self.tracked.items()):
            if path in self.deduped or path in self.no_match:
                continue
            if any(path.startswith(d) for d in media_dirs):
                continue
            try:
                st = os.stat(path)
            except FileNotFoundError:
                if key not in self.failed:
                    missing[path] = key
                continue
            if st.st_nlink == 1:
                dupes[path] = key
        return missing, dupes

    def mark_unfindable(self, key):
        self.failed.add(key)

    def remove(self, path):
        self.tracked.pop(path, None)

    def mark_deduped(self, path):
        self.deduped.add(path)

    def mark_no_match(self, path):
        self.no_match.add(path)

    def clear_stale(self):
        self.failed.clear()
        self.no_match.clear()

    def _load_state(self):
        if not self._state_file or not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            self.deduped = set(data.get("deduped", []))
            log.info("Loaded state: %d deduped paths", len(self.deduped))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not load state from %s: %s", self._state_file, e)

    def save_state(self):
        if not self._state_file:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"deduped": sorted(self.deduped)}, f)
            os.replace(tmp, self._state_file)
        except OSError as e:
            log.warning("Could not save state to %s: %s", self._state_file, e)


class MediaIndex:
    """Cached index of media directory files. Avoids re-walking on every cycle."""

    def __init__(self, media_dirs, max_age=300):
        self.media_dirs = media_dirs
        self.max_age = max_age
        self.by_inode = {}
        self.by_size = {}
        self._built_at = 0

    def get(self, force=False):
        age = time.time() - self._built_at
        if not force and self._built_at > 0 and age < self.max_age:
            return self.by_inode, self.by_size
        self._build()
        return self.by_inode, self.by_size

    def _build(self):
        by_inode = {}
        by_size = {}
        for search_dir in self.media_dirs:
            search_path = Path(search_dir)
            if not search_path.exists():
                continue
            for dirpath, _, filenames in os.walk(search_path):
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full)
                        by_inode[st.st_ino] = full
                        by_size.setdefault(st.st_size, []).append(full)
                    except (FileNotFoundError, PermissionError):
                        continue
        self.by_inode = by_inode
        self.by_size = by_size
        self._built_at = time.time()
        log.debug("Media index: %d files", len(by_inode))


FIDEDUPERANGE = 0xC0189436
FILE_DEDUPE_RANGE_SAME = 0
FILE_DEDUPE_RANGE_DIFFERS = 1

# struct file_dedupe_range header:  src_offset(u64) src_length(u64) dest_count(u16) reserved1(u16) reserved2(u32)
_DEDUP_HDR_FMT = "QQHHI"
# struct file_dedupe_range_info:    dest_fd(s64) dest_offset(u64) bytes_deduped(u64) status(s32) reserved(u32)
_DEDUP_INFO_FMT = "qQQiI"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def btrfs_dedupe(src_path, dst_path):
    """Deduplicate dst against src using the kernel FIDEDUPERANGE ioctl.

    Returns bytes deduped (0 if already shared). Raises OSError on failure.
    Raises ValueError if file contents differ.
    """
    import fcntl
    import struct

    src_fd = os.open(src_path, os.O_RDONLY)
    try:
        dst_fd = os.open(dst_path, os.O_RDWR)
        try:
            size = os.fstat(src_fd).st_size
            offset = 0
            total_deduped = 0
            while offset < size:
                chunk = size - offset
                header = struct.pack(_DEDUP_HDR_FMT, offset, chunk, 1, 0, 0)
                dest_info = struct.pack(_DEDUP_INFO_FMT, dst_fd, offset, 0, 0, 0)
                buf = bytearray(header + dest_info)
                fcntl.ioctl(src_fd, FIDEDUPERANGE, buf)
                info_offset = struct.calcsize(_DEDUP_HDR_FMT)
                _, _, bytes_deduped, status, _ = struct.unpack_from(
                    _DEDUP_INFO_FMT, buf, info_offset
                )
                if status == FILE_DEDUPE_RANGE_DIFFERS:
                    raise ValueError(
                        f"Content differs at offset {offset}: {src_path} vs {dst_path}"
                    )
                if status < 0:
                    raise OSError(-status, os.strerror(-status))
                if bytes_deduped == 0:
                    break  # already shared from this offset onward
                total_deduped += bytes_deduped
                offset += bytes_deduped
            return total_deduped
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def restore_link(original_path, found_path):
    """Restore a file at original_path linked to found_path.

    Tries hardlink first (zero cost, same inode). Falls back to reflink
    (cp --reflink=always) for cross-filesystem cases on Btrfs/XFS — also
    zero cost via copy-on-write, but a separate inode.
    """
    parent = os.path.dirname(original_path)
    os.makedirs(parent, exist_ok=True)

    try:
        os.link(found_path, original_path)
        return "hardlink"
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM):
            raise

    log.debug("Cross-filesystem — trying reflink: %s → %s", original_path, found_path)
    try:
        subprocess.run(
            ["cp", "--reflink=always", found_path, original_path],
            check=True,
            capture_output=True,
        )
        return "reflink"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.error(
            "Cannot link %s → %s: cross-filesystem and reflink unsupported (%s)",
            original_path,
            found_path,
            e,
        )
        raise OSError(f"Neither hardlink nor reflink possible for {original_path}")


# --- HTTP Status Server ---


def make_handler(stats, qbit, media_dirs):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._health()
            elif self.path == "/api/status":
                self._status()
            elif self.path.startswith("/api/history"):
                self._history()
            else:
                self._respond(404, {"error": "not found"})

        def _health(self):
            checks = {"qbit": qbit.connected}
            for d in media_dirs:
                checks[f"media:{d}"] = os.path.isdir(d)
            healthy = checks["qbit"] and all(
                v for k, v in checks.items() if k.startswith("media:")
            )
            status_code = 200 if healthy else 503
            self._respond(
                status_code,
                {
                    "status": "ok" if healthy else "degraded",
                    "version": __version__,
                    "uptime": int(time.time() - START_TIME),
                    "checks": checks,
                },
            )

        def _status(self):
            with STATS_LOCK:
                snapshot = dict(stats)
            self._respond(
                200,
                {
                    "version": __version__,
                    "uptime": int(time.time() - START_TIME),
                    **snapshot,
                },
            )

        def _history(self):
            self._respond(200, {"history": activity.recent()})

        def _respond(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body, indent=2).encode())

        def log_message(self, format, *args):
            pass

    return Handler


def start_server(port, stats, qbit, media_dirs):
    handler = make_handler(stats, qbit, media_dirs)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Status server listening on port %d", port)
    return server


# --- Main Loop ---


def run(
    qbit,
    tracker,
    media_index,
    poll_interval,
    path_map=None,
    dedup=True,
    stats=None,
):
    path_map = path_map or {}
    media_dirs = media_index.media_dirs
    if stats is None:
        stats = {}
    stats.update(
        {"relinked": 0, "deduped": 0, "tracked": 0, "cycles": 0, "last_cycle": None}
    )

    while not SHUTDOWN:
        try:
            torrents = qbit.get_seeding_torrents()
            qbit.connected = True
        except requests.RequestException as e:
            qbit.connected = False
            log.warning("qBit API error: %s — retrying in %ds", e, poll_interval)
            try:
                qbit._login()
            except Exception:
                log.debug("Re-auth failed, will retry after sleep")
            time.sleep(poll_interval)
            continue

        active_paths = set()
        for torrent in torrents:
            save_path = torrent["save_path"]
            try:
                files = qbit.get_torrent_files(torrent["hash"])
            except requests.RequestException:
                continue
            for f in files:
                qbit_path = os.path.join(save_path, f["name"])
                local_path = apply_path_map(qbit_path, path_map)
                active_paths.add(local_path)
                tracker.update(local_path)

        stale = [p for p in tracker.tracked if p not in active_paths]
        for p in stale:
            tracker.remove(p)
            tracker.deduped.discard(p)

        stats["tracked"] = len(tracker.tracked)

        missing, dupes = tracker.classify(media_dirs)
        if not dedup:
            dupes = {}

        if not missing and not dupes:
            with STATS_LOCK:
                stats["cycles"] += 1
                stats["last_cycle"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            time.sleep(poll_interval)
            continue

        by_inode, by_size = media_index.get()
        log.info(
            "Processing %d missing, %d dedup candidates",
            len(missing),
            len(dupes),
        )

        for original_path, (inode, size) in missing.items():
            found = by_inode.get(inode)
            match_type = "inode"

            if not found:
                candidates = by_size.get(size, [])
                original_name = os.path.basename(original_path)
                name_matches = [
                    c for c in candidates if os.path.basename(c) == original_name
                ]
                if len(name_matches) == 1:
                    found = name_matches[0]
                    match_type = "size+name"
                elif len(candidates) == 1:
                    found = candidates[0]
                    match_type = "size"
                elif len(candidates) > 1:
                    log.warning(
                        "Multiple size matches for %s (%d bytes, %d candidates) — skipping",
                        original_name,
                        size,
                        len(candidates),
                    )

            if found:
                try:
                    link_type = restore_link(original_path, found)
                    stats["relinked"] += 1
                    log.info(
                        "Restored %s via %s match (%s)",
                        os.path.basename(original_path),
                        match_type,
                        link_type,
                    )
                    activity.record("relink", original_path, found, link_type=link_type)
                    tracker.update(original_path)
                except OSError as e:
                    log.error("Link failed for %s: %s", original_path, e)
                    activity.record("error", original_path, "", error=e)
                    tracker.mark_unfindable((inode, size))
            else:
                log.warning(
                    "Inode %d / %d bytes (%s) not found in media dirs",
                    inode,
                    size,
                    os.path.basename(original_path),
                )
                tracker.mark_unfindable((inode, size))

        for dl_path, (inode, size) in dupes.items():
            dl_name = os.path.basename(dl_path)
            candidates = by_size.get(size, [])
            others = []
            for c in candidates:
                try:
                    if os.stat(c).st_ino != inode:
                        others.append(c)
                except FileNotFoundError:
                    continue
            media_match = None
            name_hits = [c for c in others if os.path.basename(c) == dl_name]
            if len(name_hits) == 1:
                media_match = name_hits[0]
            elif len(others) == 1:
                media_match = others[0]
            if not media_match:
                tracker.mark_no_match(dl_path)
                continue
            try:
                if os.stat(dl_path).st_ino == os.stat(media_match).st_ino:
                    log.debug("Already hardlinked: %s", dl_name)
                    tracker.mark_deduped(dl_path)
                    continue
            except FileNotFoundError:
                continue
            try:
                bytes_deduped = btrfs_dedupe(media_match, dl_path)
                if bytes_deduped == 0:
                    log.debug("Already sharing extents: %s", dl_name)
                else:
                    stats["deduped"] += 1
                    log.info(
                        "Deduped: %s → %s (%s freed)",
                        dl_name,
                        media_match,
                        _fmt_size(bytes_deduped),
                    )
                    activity.record("dedup", dl_path, media_match, link_type="dedupe")
                tracker.mark_deduped(dl_path)
            except ValueError as e:
                log.warning(
                    "Content mismatch for %s — not a true duplicate: %s", dl_name, e
                )
                tracker.mark_no_match(dl_path)
            except OSError as e:
                log.warning(
                    "Dedupe failed for %s, falling back to reflink: %s", dl_name, e
                )
                bak_path = dl_path + ".bak"
                try:
                    os.rename(dl_path, bak_path)
                    link_type = restore_link(dl_path, media_match)
                    os.remove(bak_path)
                    stats["deduped"] += 1
                    tracker.mark_deduped(dl_path)
                    log.info("Deduped: %s → %s (%s)", dl_name, media_match, link_type)
                    activity.record("dedup", dl_path, media_match, link_type=link_type)
                except OSError as e2:
                    if os.path.exists(bak_path):
                        if os.path.exists(dl_path):
                            os.remove(dl_path)
                        os.rename(bak_path, dl_path)
                        log.warning("Rolled back %s: %s", dl_name, e2)
                        activity.record("rollback", dl_path, media_match, error=e2)
                    else:
                        log.error("Dedup failed for %s: %s", dl_path, e2)
                        activity.record("error", dl_path, media_match, error=e2)

        tracker.save_state()

        with STATS_LOCK:
            stats["cycles"] += 1
            stats["last_cycle"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if stats["cycles"] % 10 == 0:
            log.info(
                "Status: tracking %d files, %d relinked, %d deduped",
                stats["tracked"],
                stats["relinked"],
                stats["deduped"],
            )
            tracker.clear_stale()

        time.sleep(poll_interval)

    log.info(
        "Shutting down. Relinked %d files (%d deduped) across %d cycles.",
        stats["relinked"],
        stats["deduped"],
        stats["cycles"],
    )


def parse_path_map(raw):
    """Parse PATH_MAP env var: '/downloads:/volume1/downloads,/tv:/volume1/tv'."""
    mapping = {}
    if not raw:
        return mapping
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            log.warning("Ignoring malformed PATH_MAP entry (no colon): %s", entry)
            continue
        src, dst = entry.split(":", 1)
        mapping[src.rstrip("/")] = dst.rstrip("/")
    return mapping


def apply_path_map(path, path_map):
    """Translate a path using the path map (longest prefix match)."""
    for src, dst in sorted(path_map.items(), key=lambda x: -len(x[0])):
        if path == src or path.startswith(src + "/"):
            return dst + path[len(src) :]
    return path


def main():
    qbit_url = os.environ.get("QBIT_URL", "http://localhost:8080")
    qbit_user = os.environ.get("QBIT_USER", "admin")
    qbit_pass = os.environ.get("QBIT_PASS", "adminadmin")
    media_dirs = os.environ.get("MEDIA_DIRS", "").split(",")
    media_dirs = [d.strip() for d in media_dirs if d.strip()]
    poll_interval = int(os.environ.get("POLL_INTERVAL", "30"))
    path_map = parse_path_map(os.environ.get("PATH_MAP", ""))
    dedup = os.environ.get("DEDUP", "true").lower() in ("true", "1", "yes")
    config_dir = os.environ.get("CONFIG_DIR", "/config")
    port = int(os.environ.get("PORT", "7585"))

    if not media_dirs:
        log.error("MEDIA_DIRS is required (comma-separated paths to search)")
        sys.exit(1)

    log.info("relinkarr v%s starting", __version__)
    log.info("Media directories: %s", media_dirs)
    log.info("Poll interval: %ds", poll_interval)
    if path_map:
        log.info("Path mappings: %s", path_map)
    if dedup:
        log.info("Dedup mode: enabled")

    state_file = os.path.join(config_dir, "state.json") if config_dir else None
    qbit = QbitClient(qbit_url, qbit_user, qbit_pass)
    tracker = FileTracker(state_file)
    media_index = MediaIndex(media_dirs)
    stats = {}

    if port > 0:
        start_server(port, stats, qbit, media_dirs)

    run(qbit, tracker, media_index, poll_interval, path_map, dedup, stats)


if __name__ == "__main__":
    main()
