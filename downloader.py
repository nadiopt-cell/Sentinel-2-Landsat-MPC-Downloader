"""
Download manager: parallel, resumable (HTTP byte-range), cancellable.

Files are written to '<name>.part' and renamed only when complete, so a
half-downloaded file is never mistaken for a finished one, and files that
already exist are reused (re-running a scene costs nothing).

A '.part' file is the resume point: when a transfer is interrupted
(cancel, network drop, incomplete body) it is *kept*, and the next run
continues it with a 'Range' request instead of starting over.  Servers
that ignore Range are handled too (the part is rewritten from scratch).
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .net import make_session


class Cancelled(Exception):
    """Raised when the user cancels a running operation."""


class DownloadError(Exception):
    pass


CHUNK_SIZE = 1024 * 1024


def _download_one(url, dest_path, timeout, cancelled, on_bytes, session=None):
    dest = Path(dest_path)
    if dest.exists():
        st = dest.stat()
        if st.st_size > 0:
            on_bytes(st.st_size, st.st_size)
            return str(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    own_session = session is None
    session = session if session is not None else make_session()
    try:
        offset = 0
        resume = part.exists() and part.stat().st_size > 0
        if resume:
            offset = part.stat().st_size
        headers = {"Range": f"bytes={offset}-"} if resume else None

        with session.get(url, stream=True, timeout=timeout, headers=headers) as r:
            if resume and r.status_code == 416:
                # Range not satisfiable: the part is stale or oversized.
                # Drop it and restart the transfer from scratch (once).
                try:
                    part.unlink()
                except OSError:
                    pass
                return _download_one(url, dest_path, timeout, cancelled,
                                     on_bytes, session)
            r.raise_for_status()
            if resume and r.status_code != 206:
                offset = 0          # server ignored Range: rewrite from zero
                resume = False
            length = int(r.headers.get("Content-Length") or 0)
            expected_total = (offset + length) if (resume and length) else length
            on_bytes(offset, expected_total)
            written = 0
            with open(part, "ab" if resume else "wb") as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if cancelled():
                        raise Cancelled()
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        on_bytes(len(chunk), 0)
        if length and written != length:
            raise DownloadError(f"Incomplete download ({written} of {length} bytes)")
        os.replace(part, dest)
    except BaseException:
        # Keep the .part file whenever it holds useful data: it is where
        # the next attempt will resume.  Only drop empty files.
        try:
            if part.exists() and part.stat().st_size == 0:
                part.unlink()
        except OSError:
            pass
        raise
    finally:
        if own_session:
            session.close()
    return str(dest)


def download(urls_to_paths, max_workers=4, progress_cb=None, timeout=120,
             cancelled=lambda: False):
    """Download (url, dest_path) pairs in parallel.

    progress_cb(done_bytes, total_bytes, done_files, total_files) is called
    from worker threads (total_bytes grows as sizes become known).
    Returns {url: path}. Raises Cancelled or DownloadError (listing all
    failures).

    The whole batch shares one session/connection pool: with N band files
    per scene this saves N TCP+TLS handshakes and enables keep-alive.
    """
    pairs = list(urls_to_paths)
    if not pairs:
        return {}

    lock = threading.Lock()
    state = {"done": 0, "total": 0, "files": 0}

    def on_bytes(delta_done, delta_total):
        with lock:
            state["done"] += delta_done
            state["total"] += delta_total
            snap = (state["done"], state["total"], state["files"])
        if progress_cb:
            progress_cb(snap[0], snap[1], snap[2], len(pairs))

    results, errors = {}, {}
    session = make_session(pool_maxsize=2 * max(1, max_workers))
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pairs)))) as pool:
            futures = {
                pool.submit(_download_one, url, dst, timeout, cancelled,
                            on_bytes, session): url
                for url, dst in pairs
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    results[url] = fut.result()
                    with lock:
                        state["files"] += 1
                    on_bytes(0, 0)
                except Cancelled:
                    errors[url] = "cancelled"
                except Exception as e:  # noqa: BLE001
                    errors[url] = str(e)
    finally:
        session.close()
    if cancelled():
        raise Cancelled()
    if errors:
        raise DownloadError("; ".join(f"{u.split('?')[0]}: {m}" for u, m in errors.items()))
    return results


def download_thumbnails(items, on_item=None, max_workers=6, timeout=30,
                        cancelled=lambda: False):
    """Best-effort parallel download of preview images.

    items: list of (key, url, dest_path). Failures are ignored.
    on_item(key, path) is called (from a worker thread) for each success.

    The batch shares one session (one TLS handshake instead of N).
    """
    items = list(items)
    if not items:
        return {}

    def one(session, key, url, dest):
        if cancelled():
            return key, None
        try:
            return key, _download_one(url, dest, timeout, cancelled,
                                      lambda *_: None, session)
        except Exception:  # noqa: BLE001
            return key, None

    out = {}
    session = make_session()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(one, session, *it) for it in items]
            for fut in as_completed(futs):
                key, path = fut.result()
                out[key] = path
                if path and on_item:
                    on_item(key, path)
    finally:
        session.close()
    return out
