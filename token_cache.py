"""
Cache for Microsoft Planetary Computer SAS tokens.

Tokens are short-lived. They are cached in the QGIS profile directory
(sentinel_mpc_cache.json) so repeated requests don't hit the endpoint.

Locking strategy (v0.4.2):
  - reading the cache never blocks: the cache file is replaced atomically
    (os.replace), so a reader sees either the old or the new content;
  - refreshing is single-flight per collection: one thread performs the
    network request OUTSIDE any lock while concurrent callers wait on a
    per-collection event, then reuse the refreshed token. A slow or
    failing token endpoint can therefore delay only callers that actually
    need a refreshed token for the same collection — never the fast
    cache-hit path and never unrelated collections.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .net import shared_session

SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"

# Refresh the token a bit before its reported expiry.
EXPIRY_SAFETY_MARGIN_S = 300
DEFAULT_TTL_S = 1800

_lock = threading.Lock()          # guards _refreshing only (never held on I/O)
_refreshing = {}                   # collection -> Event, touched only under _lock


def profile_dir():
    """QGIS profile directory (falls back to ~/.qgis2 outside QGIS)."""
    try:
        from qgis.core import QgsApplication
        return Path(QgsApplication.qgisSettingsDirPath())
    except Exception:
        return Path.home() / ".qgis2"


def _cache_path():
    return profile_dir() / "sentinel_mpc_cache.json"


def _read_cache():
    try:
        with _cache_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(data):
    """Atomic write; cache problems must never break the plugin."""
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def _iso_to_epoch(iso):
    """Parse ISO-8601 (incl. trailing 'Z', which Python < 3.11 rejects)."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _fresh_entry(entry):
    """True if the cached entry is still usable (expiry margin not crossed)."""
    return bool(entry) and \
        _iso_to_epoch(entry.get("expires_at", "")) - time.time() > EXPIRY_SAFETY_MARGIN_S


def get_token(collection, network_timeout=30, force_refresh=False):
    """Return a valid SAS token for the STAC collection (cached).

    The common case (fresh cached token) takes no lock at all. On a cache
    miss the first thread per collection fetches a new token outside the
    lock while other threads wait on its completion event (bounded by the
    network timeout) and then re-read the cache. If the refresher fails,
    the next waiter takes over — callers keep retrying until one of them
    succeeds or raises, so no thread can be silently stuck behind a slow
    endpoint for longer than its own request would take.
    """
    if not force_refresh:
        entry = _read_cache().get(collection)      # lock-free fast path
        if _fresh_entry(entry):
            return entry["token"]

    deadline = time.time() + max(int(network_timeout), 0) + 5
    while True:
        with _lock:
            if not force_refresh:
                entry = _read_cache().get(collection)
                if _fresh_entry(entry):            # someone just refreshed it
                    return entry["token"]
            event = _refreshing.get(collection)
            if event is None:
                _refreshing[collection] = threading.Event()
                break                              # we are the refresher
        # Another thread is already fetching: wait, then re-check the cache.
        remaining = deadline - time.time()
        event.wait(remaining if remaining > 0 else 0.05)

    try:
        resp = shared_session().get(SAS_TOKEN_URL.format(collection=collection),
                                    timeout=network_timeout)
        resp.raise_for_status()
        payload = resp.json()

        token = payload.get("token", "")
        # MPC reports the expiry as "msft:expiry" (older docs: "expiresAt").
        expiry = payload.get("msft:expiry") or payload.get("expiresAt")
        expires_epoch = _iso_to_epoch(expiry) if expiry else 0.0
        if expires_epoch <= time.time():
            expires_epoch = time.time() + DEFAULT_TTL_S

        # Re-read right before writing: another collection may have been
        # refreshed meanwhile, and its entry must survive.
        cache = _read_cache()
        cache[collection] = {
            "token": token,
            "expires_at": datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat(),
        }
        _write_cache(cache)
        return token
    finally:
        with _lock:
            ev = _refreshing.pop(collection, None)
        if ev is not None:
            ev.set()                               # wake the waiters


def clear_cache():
    """Drop all cached tokens (supported reset hook, also used by tests)."""
    with _lock:
        _write_cache({})
