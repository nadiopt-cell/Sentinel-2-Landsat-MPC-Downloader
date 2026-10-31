"""Shared HTTP helpers (retrying sessions, connection reuse)."""

import threading

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

USER_AGENT = "qgis-sentinel-mpc-plugin/0.5.1"

_RETRY_KWARGS = dict(
    total=4,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
)


def _make_retry():
    try:
        return Retry(allowed_methods=frozenset(["GET", "POST"]), **_RETRY_KWARGS)
    except TypeError:  # urllib3 < 1.26
        return Retry(method_whitelist=frozenset(["GET", "POST"]), **_RETRY_KWARGS)


def make_session(pool_maxsize=10):
    """Return a requests.Session that retries transient errors with backoff.

    A session owns a connection pool. Create one per *batch* of requests
    (not per request): reusing it saves a TCP+TLS handshake per file and
    enables HTTP keep-alive.
    """
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_make_retry(),
                          pool_connections=4, pool_maxsize=max(4, pool_maxsize))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session


# Process-wide session for low-volume API calls (STAC search, SAS tokens).
# Concurrent use is fine: urllib3 pools are thread-safe and these endpoints
# do not rely on cookies.
_shared_lock = threading.Lock()
_shared_session = None


def shared_session():
    """Return a lazily created, process-wide session for API requests."""
    global _shared_session
    if _shared_session is None:
        with _shared_lock:
            if _shared_session is None:
                _shared_session = make_session()
    return _shared_session
