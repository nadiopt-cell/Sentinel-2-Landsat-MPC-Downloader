"""
Scene picker: sortable table of STAC features with preview thumbnails
(loaded in the background). Select a row and press OK (or double-click).
"""

import hashlib
import re
from datetime import datetime

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import downloader, stac_client
from .token_cache import profile_dir
from .worker import Worker

THUMB_COL, DATE_COL, CLOUD_COL, TILE_COL, ID_COL = range(5)
THUMB_SIZE = 96
THUMB_MAX = 150          # cap preview downloads on very large result sets
THUMB_CACHE_KEEP = 400   # max files kept in the thumbnail cache dir


def _thumb_cache_dir():
    return profile_dir() / "sentinel_mpc_thumbs"


def _thumb_filename(key):
    """Collision-safe, filesystem-safe cache name for a scene key."""
    safe = re.sub(r"[^\w\-.]+", "_", key)[:80] or "thumb"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}.png"


def _safe_mtime(p):
    """mtime that tolerates a file vanishing mid-listing (antivirus, a
    second QGIS instance pruning the same cache): treat as oldest."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _prune_thumb_cache(max_files=THUMB_CACHE_KEEP):
    """Drop the oldest cached previews when the cache grows unbounded."""
    d = _thumb_cache_dir()
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except OSError:
        return
    if len(files) <= max_files:
        return
    files.sort(key=_safe_mtime)
    for p in files[:len(files) - max_files]:
        try:
            p.unlink()
        except OSError:
            pass


class SceneDialog(QDialog):
    def __init__(self, features, token, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select a scene")
        self.resize(900, 520)
        # Collision-safe keys: STAC ids are unique in practice, but a
        # duplicated id used to silently overwrite a scene in the dict.
        self.features = {}
        for i, f in enumerate(features):
            fid = f.get("id") or f"scene-{i}"
            key = fid if fid not in self.features else f"{fid}#{i}"
            self.features[key] = f
        self.token = token
        self.selected_feature = None
        self._worker = None
        self._build_ui(len(features))
        self._populate()
        self._load_thumbnails()

    def _build_ui(self, count):
        layout = QVBoxLayout(self)
        self.info_label = QLabel(
            f"Found {count} scenes (newest first; click a header to sort). "
            "Loading thumbnails...")
        layout.addWidget(self.info_label)

        t = self.table = QTableWidget(0, 5, self)
        t.setHorizontalHeaderLabels(
            ["Preview", "Date (UTC)", "Cloud %", "Tile", "Feature id"])
        t.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        t.verticalHeader().setDefaultSectionSize(THUMB_SIZE + 6)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setColumnWidth(THUMB_COL, THUMB_SIZE + 16)
        t.setColumnWidth(DATE_COL, 160)
        t.setColumnWidth(CLOUD_COL, 80)
        t.setColumnWidth(TILE_COL, 100)
        t.setColumnHidden(ID_COL, True)
        t.horizontalHeader().setStretchLastSection(True)
        t.cellDoubleClicked.connect(lambda row, _c: self._accept_row(row))
        layout.addWidget(t)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._on_ok)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _populate(self):
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self.features))
        center = Qt.AlignmentFlag.AlignCenter
        role = Qt.ItemDataRole.UserRole
        for row, (key, feat) in enumerate(self.features.items()):
            ph = QTableWidgetItem("")
            t.setItem(row, THUMB_COL, ph)

            dt = stac_client.feature_datetime(feat)
            try:
                pretty = datetime.fromisoformat(dt.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pretty = dt
            t.setItem(row, DATE_COL, QTableWidgetItem(pretty))

            cc = stac_client.feature_cloud_cover(feat)
            cc_item = QTableWidgetItem()
            if cc is not None:
                try:
                    cc_item.setData(Qt.ItemDataRole.DisplayRole,
                                    round(float(cc), 1))     # numeric sort
                except (TypeError, ValueError):
                    cc_item.setText("-")   # keep the row usable on bad data
            else:
                cc_item.setText("-")
            cc_item.setTextAlignment(center)
            t.setItem(row, CLOUD_COL, cc_item)

            t.setItem(row, TILE_COL, QTableWidgetItem(stac_client.feature_tile_id(feat)))
            id_item = QTableWidgetItem(key.split("#")[0])
            id_item.setData(role, key)
            t.setItem(row, ID_COL, id_item)
        t.setSortingEnabled(True)
        if t.rowCount():
            t.selectRow(0)

    def _row_key(self, row):
        item = self.table.item(row, ID_COL)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole) or item.text()

    # ---- thumbnails (background) ----

    def _load_thumbnails(self):
        cache = _thumb_cache_dir()
        items = []
        for key, feat in self.features.items():   # insertion order = newest first
            if len(items) >= THUMB_MAX:
                break
            href = stac_client.feature_thumbnail_href(feat)
            if href:
                items.append((key, stac_client.signed_url(href, self.token),
                              str(cache / _thumb_filename(key))))
        total = len(items)
        if not total:
            self.info_label.setText(f"Found {len(self.features)} scenes (no previews).")
            return
        _prune_thumb_cache()
        self._done = 0
        self._total = total

        def job(w):
            return downloader.download_thumbnails(
                items, on_item=lambda k, p: w.emit_item((k, p)),
                cancelled=w.is_cancelled)

        self._worker = Worker(job, self)
        self._worker.item.connect(self._on_thumb)
        self._worker.succeeded.connect(lambda _r: self.info_label.setText(
            f"Found {len(self.features)} scenes. Select one and press OK."))
        self._worker.failed.connect(lambda _e: None)
        self._worker.start()

    def _on_thumb(self, payload):
        key, path = payload
        self._done += 1
        self.progress.setValue(int(100 * self._done / self._total))
        for row in range(self.table.rowCount()):
            if self._row_key(row) == key:
                pix = QPixmap(str(path))
                if not pix.isNull():
                    pix = pix.scaled(THUMB_SIZE, THUMB_SIZE,
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                    self.table.item(row, THUMB_COL).setIcon(QIcon(pix))
                break

    # ---- selection ----

    def _on_ok(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._accept_row(rows[0].row())

    def _accept_row(self, row):
        key = self._row_key(row)
        if key is None:
            return
        self.selected_feature = self.features.get(key)
        if self.selected_feature is not None:
            self.accept()

    def done(self, result):
        if self._worker is not None and not self._worker.stop(5000):
            self._worker.detach()  # never destroy a running QThread
        super().done(result)
