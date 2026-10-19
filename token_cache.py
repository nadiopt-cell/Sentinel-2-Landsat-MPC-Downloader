"""
Cache for Microsoft Planetary Computer SAS tokens.

Tokens are short-lived. They are cached in the QGIS profile directory
(sentinel_mpc_cache.json) so repeated requests don't hit the endpoint.

Refreshes are serialised with a process-wide lock: the search worker and
the download pipeline can both ask for a token at the same time, and
without the lock they would each hit the endpoint and overwrite each
other's cache entry.
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

_lock = threading.RLock()


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


def get_token(collection, network_timeout=30, force_refresh=False):
    """Return a valid SAS token for the STAC collection (cached)."""
    with _lock:
        now = time.time()
        cache = _read_cache()
        entry = cache.get(collection)
        if entry and not force_refresh:
            if _iso_to_epoch(entry.get("expires_at", "")) - now > EXPIRY_SAFETY_MARGIN_S:
                return entry["token"]

        resp = shared_session().get(SAS_TOKEN_URL.format(collection=collection),
                                    timeout=network_timeout)
        resp.raise_for_status()
        payload = resp.json()

        token = payload.get("token", "")
        # MPC reports the expiry as "msft:expiry" (older docs: "expiresAt").
        expiry = payload.get("msft:expiry") or payload.get("expiresAt")
        expires_epoch = _iso_to_epoch(expiry) if expiry else 0.0
        if expires_epoch <= now:
            expires_epoch = now + DEFAULT_TTL_S

        cache[collection] = {
            "token": token,
            "expires_at": datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat(),
        }
        _write_cache(cache)
        return token


def clear_cache():
    with _lock:
        _write_cache({})
