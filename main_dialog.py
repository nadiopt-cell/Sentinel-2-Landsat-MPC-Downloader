"""
Main dialog: AOI, dates, cloud cover, products -> search -> pick scene ->
download + build. Two source tabs: Sentinel-2 L2A and Landsat C2 L2
(missions selected inside the Landsat tab). All network/disk/GDAL work
runs in a background thread.
"""

from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from qgis.PyQt.QtCore import QDate, QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import landsat, pipeline, stac_client, syntheses
from .loader import add_to_project
from .scene_dialog import SceneDialog
from .token_cache import get_token
from .worker import Worker

SETTINGS = "sentinel_mpc/"
DEFAULT_BBOX = (-10.0, 50.0, 10.0, 60.0)
ALL_BANDS_ITEM = "All bands"   # meta-item: select/deselect every band at once

TAB_S2 = 0
TAB_LANDSAT = 1


def _default_out_dir():
    # Neutral name (both Sentinel-2 and Landsat land here). Existing
    # installations keep their old value via QSettings.
    return Path.home() / "MPC_Downloader"


class MainDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Sentinel-2 / Landsat from Microsoft Planetary Computer")
        self.resize(560, 720)
        self._scene_feature = None
        self._worker = None
        self._scene_dialog = None
        # Source and product selection are frozen at search time so that
        # switching tabs or missions afterwards cannot mix S2 and Landsat.
        self._pending = None
        self._settings = QSettings()
        self._build_ui()

    # ---- UI ----

    def _spin(self, lo, hi, value):
        s = QDoubleSpinBox()
        s.setDecimals(6)
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    def _make_select_list(self):
        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        return lst

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bbox_box = QGroupBox("Area of interest (EPSG:4326)")
        form = QFormLayout(bbox_box)
        self.min_x = self._spin(-180, 180, DEFAULT_BBOX[0])
        self.min_y = self._spin(-90, 90, DEFAULT_BBOX[1])
        self.max_x = self._spin(-180, 180, DEFAULT_BBOX[2])
        self.max_y = self._spin(-90, 90, DEFAULT_BBOX[3])
        form.addRow("Min X (lon):", self.min_x)
        form.addRow("Min Y (lat):", self.min_y)
        form.addRow("Max X (lon):", self.max_x)
        form.addRow("Max Y (lat):", self.max_y)
        row = QHBoxLayout()
        canvas_btn = QPushButton("From canvas")
        canvas_btn.setToolTip("Use the current canvas extent (reprojected to EPSG:4326)")
        canvas_btn.clicked.connect(self._take_from_canvas)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._reset_bbox)
        row.addWidget(canvas_btn)
        row.addWidget(reset_btn)
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        form.addRow("", w)
        layout.addWidget(bbox_box)

        date_box = QGroupBox("Time window and cloud cover")
        dform = QFormLayout(date_box)
        self.date_start = QDateEdit(calendarPopup=True)
        self.date_start.setDate(QDate.currentDate().addMonths(-1))
        self.date_end = QDateEdit(calendarPopup=True)
        self.date_end.setDate(QDate.currentDate())
        dform.addRow("From:", self.date_start)
        dform.addRow("To (inclusive):", self.date_end)
        self.cloud = QSpinBox()
        self.cloud.setRange(0, 100)
        self.cloud.setValue(int(self._settings.value(SETTINGS + "cloud", 30)))
        self.cloud.setSuffix(" %")
        dform.addRow("Max cloud cover:", self.cloud)
        layout.addWidget(date_box)

        # ---- source tabs ----
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_s2_tab(), "Sentinel-2")
        self.tabs.addTab(self._build_landsat_tab(), "Landsat")
        layout.addWidget(self.tabs, 1)

        out_box = QGroupBox("Output")
        ol = QFormLayout(out_box)
        self.output_dir = Path(self._settings.value(SETTINGS + "out_dir", str(_default_out_dir())))
        self.output_dir_label = QLabel(str(self.output_dir))
        self.output_dir_label.setWordWrap(True)
        ol.addRow("Folder:", self.output_dir_label)
        out_btn = QPushButton("Change...")
        out_btn.clicked.connect(self._choose_output_dir)
        ol.addRow(out_btn)
        self.keep_bands = QCheckBox("Keep downloaded band files (reused for next runs)")
        self.keep_bands.setChecked(True)
        ol.addRow(self.keep_bands)
        layout.addWidget(out_box)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox()
        self.search_btn = QPushButton("Search scenes")
        self.search_btn.clicked.connect(self._on_search)
        self.download_btn = QPushButton("Download and build")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._on_download)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        role = QDialogButtonBox.ButtonRole
        self.buttons.addButton(self.search_btn, role.ActionRole)
        self.buttons.addButton(self.download_btn, role.ActionRole)
        self.buttons.addButton(self.cancel_btn, role.ActionRole)
        self.buttons.addButton(self.close_btn, role.RejectRole)
        layout.addWidget(self.buttons)

    def _build_s2_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        synth_box = QGroupBox("Syntheses (multi-select)")
        sl = QVBoxLayout(synth_box)
        self.synth_list = self._make_select_list()
        for name, spec in syntheses.PRESETS.items():
            item = QListWidgetItem(name)
            item.setToolTip(spec.get("description", ""))
            self.synth_list.addItem(item)
            if name == "True Color":
                item.setSelected(True)
        sl.addWidget(self.synth_list)
        v.addWidget(synth_box)

        bands_box = QGroupBox("Individual bands (multi-select)")
        bl = QVBoxLayout(bands_box)
        self.s2_band_list = self._make_select_list()
        self._add_all_bands_item(self.s2_band_list)
        self.s2_band_list.itemClicked.connect(
            lambda it, l=self.s2_band_list: self._on_band_item_clicked(l, it))
        for band in syntheses.INDIVIDUAL_BANDS:
            item = QListWidgetItem(syntheses.BAND_LABELS.get(band, band))
            item.setData(Qt.ItemDataRole.UserRole, band)
            item.setToolTip(band)
            self.s2_band_list.addItem(item)
        bl.addWidget(self.s2_band_list)
        v.addWidget(bands_box, 1)
        return tab

    def _build_landsat_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)

        mission_form = QFormLayout()
        self.mission_combo = QComboBox()
        for platform in landsat.MISSION_KEYS:
            self.mission_combo.addItem(landsat.mission_label(platform), platform)
        saved = str(self._settings.value(SETTINGS + "landsat_mission", "landsat-9"))
        idx = self.mission_combo.findData(saved)
        self.mission_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mission_combo.currentIndexChanged.connect(self._on_mission_changed)
        mission_form.addRow("Mission:", self.mission_combo)
        mission_w = QWidget()
        mission_w.setLayout(mission_form)
        v.addWidget(mission_w)

        prod_box = QGroupBox("Standard syntheses and indices (multi-select)")
        pl = QVBoxLayout(prod_box)
        self.ls_product_list = self._make_select_list()
        pl.addWidget(self.ls_product_list)
        v.addWidget(prod_box)

        bands_box = QGroupBox("Individual bands (multi-select)")
        bl = QVBoxLayout(bands_box)
        self.ls_band_list = self._make_select_list()
        self._add_all_bands_item(self.ls_band_list)
        self.ls_band_list.itemClicked.connect(
            lambda it, l=self.ls_band_list: self._on_band_item_clicked(l, it))
        bl.addWidget(self.ls_band_list)
        v.addWidget(bands_box, 1)

        note = QLabel("Note: Landsat 1-3 (MSS) are not hosted on Planetary "
                      "Computer; Landsat 4 (1982) is the earliest available "
                      "mission. All bands are delivered at 30 m.")
        note.setWordWrap(True)
        v.addWidget(note)

        self._on_mission_changed()
        return tab

    def _add_all_bands_item(self, lst):
        """Meta-item on top of a band list: no UserRole -> never counted as a band."""
        item = QListWidgetItem(ALL_BANDS_ITEM)
        item.setToolTip("Select (or deselect) every band at once")
        lst.addItem(item)

    def _on_band_item_clicked(self, lst, item):
        if item.text() != ALL_BANDS_ITEM:
            return
        state = item.isSelected()      # MultiSelection: click toggles
        for i in range(lst.count()):
            other = lst.item(i)
            if other is not item:
                other.setSelected(state)

    def _on_mission_changed(self):
        platform = self._landsat_mission()
        self._settings.setValue(SETTINGS + "landsat_mission", platform)

        self.ls_product_list.clear()
        products = landsat.products_for(platform)
        for name, spec in products.items():
            item = QListWidgetItem(name)
            item.setToolTip(spec.get("description", ""))
            self.ls_product_list.addItem(item)
            if name == "True Color":
                item.setSelected(True)

        self.ls_band_list.clear()
        self._add_all_bands_item(self.ls_band_list)
        for band in landsat.bands_for(platform):
            item = QListWidgetItem(landsat.BAND_LABELS.get(band, band))
            item.setData(Qt.ItemDataRole.UserRole, band)
            self.ls_band_list.addItem(item)

    # ---- helpers ----

    def _take_from_canvas(self):
        canvas = self.iface.mapCanvas()
        try:
            extent = canvas.extent()
            src = canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            if src != dst:
                extent = QgsCoordinateTransform(
                    src, dst, QgsProject.instance()).transformBoundingBox(extent)
        except Exception as e:  # noqa: BLE001
            # Degenerate or dateline-crossing extents abort the transform:
            # show it instead of failing silently inside the slot.
            QMessageBox.warning(
                self, "Extent error",
                f"Could not convert the canvas extent to EPSG:4326:\n{e}")
            return
        self.min_x.setValue(extent.xMinimum())
        self.min_y.setValue(extent.yMinimum())
        self.max_x.setValue(extent.xMaximum())
        self.max_y.setValue(extent.yMaximum())

    def _reset_bbox(self):
        for spin, v in zip((self.min_x, self.min_y, self.max_x, self.max_y), DEFAULT_BBOX):
            spin.setValue(v)

    def _choose_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", str(self.output_dir))
        if d:
            self.output_dir = Path(d)
            self.output_dir_label.setText(d)
            self._settings.setValue(SETTINGS + "out_dir", d)

    def _selected_syntheses(self):
        return [i.text() for i in self.synth_list.selectedItems()]

    def _bands_from_list(self, lst):
        # Items without UserRole data (the 'All bands' meta-item) are skipped.
        return [d for d in (i.data(Qt.ItemDataRole.UserRole)
                            for i in lst.selectedItems()) if d]

    def _selected_s2_bands(self):
        return self._bands_from_list(self.s2_band_list)

    def _landsat_mission(self):
        return self.mission_combo.currentData() or landsat.MISSION_KEYS[0]

    def _selected_landsat_products(self):
        return [i.text() for i in self.ls_product_list.selectedItems()]

    def _selected_landsat_bands(self):
        return self._bands_from_list(self.ls_band_list)

    def _current_source(self):
        return "Landsat" if self.tabs.currentIndex() == TAB_LANDSAT else "S2"

    def _bbox(self):
        return [self.min_x.value(), self.min_y.value(), self.max_x.value(), self.max_y.value()]

    def _set_busy(self, busy):
        self.search_btn.setEnabled(not busy)
        self.download_btn.setEnabled(not busy and self._scene_feature is not None)
        self.cancel_btn.setEnabled(busy)

    def _start(self, fn, on_success, busy_text):
        self._set_busy(True)
        self.status_label.setText(busy_text)
        self.progress.setRange(0, 0)  # indeterminate until real progress arrives
        w = self._worker = Worker(fn, self)
        w.progress.connect(self._on_progress)
        w.succeeded.connect(lambda r: self._finish(on_success, r))
        w.failed.connect(self._on_failed)
        w.start()

    def _on_progress(self, pct, msg):
        self.progress.setRange(0, 100)
        self.progress.setValue(pct)
        if msg:
            self.status_label.setText(msg)

    def _finish(self, on_success, result):
        self._set_busy(False)
        self.progress.setRange(0, 100)
        on_success(result)

    def _on_failed(self, err):
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        if not err:
            self.status_label.setText("Cancelled.")
            return
        first = err.split("\n", 1)[0]
        self.status_label.setText(f"Failed: {first}")
        box = QMessageBox(QMessageBox.Icon.Critical, "Failed", first, parent=self)
        box.setDetailedText(err)
        box.exec()

    def _on_cancel(self):
        if self._worker:
            self._worker.cancel()
            self.status_label.setText("Cancelling...")

    def closeEvent(self, event):  # noqa: N802
        self._close_scene_dialog()
        if self._worker is not None:
            # Cancel and give short-running jobs a chance to finish. If the
            # job cannot stop within 5 s (a long GDAL step), park the worker
            # instead of letting the dialog destroy a running QThread
            # (that used to abort the whole QGIS process).
            if not self._worker.stop(5000):
                self._worker.detach()
        self._settings.setValue(SETTINGS + "cloud", self.cloud.value())
        super().closeEvent(event)

    # ---- search ----

    def _on_search(self):
        if self.min_x.value() >= self.max_x.value() or self.min_y.value() >= self.max_y.value():
            QMessageBox.warning(self, "Invalid bbox", "Min coordinates must be less than max.")
            return
        if self.date_start.date() > self.date_end.date():
            QMessageBox.warning(self, "Invalid dates", "'From' must not be after 'To'.")
            return

        source = self._current_source()
        if source == "S2":
            products = self._selected_syntheses()
            bands, mission = self._selected_s2_bands(), None
            if not products and not bands:
                QMessageBox.warning(self, "No syntheses",
                                    "Pick at least one synthesis or individual band.")
                return
        else:
            mission = self._landsat_mission()
            products = self._selected_landsat_products()
            bands = self._selected_landsat_bands()
            if not products and not bands:
                QMessageBox.warning(self, "Nothing selected",
                                    "Pick at least one synthesis or individual band.")
                return

        self._scene_feature = None
        self._pending = {
            "source": source, "mission": mission,
            "products": products, "bands": bands,
        }
        self._close_scene_dialog()      # a new search invalidates the old picker
        bbox = self._bbox()
        d0 = self.date_start.date().toString("yyyy-MM-dd")
        d1 = self.date_end.date().toString("yyyy-MM-dd")
        cloud = self.cloud.value()
        collection = landsat.COLLECTION if source == "Landsat" else stac_client.S2L2A_COLLECTION

        def job(w):
            platforms = [mission] if source == "Landsat" else None
            features = stac_client.search(bbox, d0, d1, max_cloud=cloud,
                                          collection=collection,
                                          platforms=platforms)
            token = get_token(collection) if features else ""
            return features, token

        self._start(job, self._on_search_done, "Searching STAC catalog...")

    def _on_search_done(self, result):
        features, token = result
        self.progress.setValue(0)
        if not features:
            self.status_label.setText("No scenes found.")
            QMessageBox.information(self, "No scenes", "No scenes matched the criteria.")
            return
        self._close_scene_dialog()      # a previous picker may still be open
        dlg = SceneDialog(features, token, self.iface.mapCanvas(), parent=self)
        self._scene_dialog = dlg
        dlg.accepted.connect(self._on_scene_accepted)
        dlg.rejected.connect(self._on_scene_rejected)
        # Non-modal: the map stays usable, so the footprint outline of the
        # highlighted scene can be compared with the search area (pan/zoom
        # while the picker is open).
        dlg.show()
        self.status_label.setText(
            "Pick a scene — its footprint is outlined on the map "
            "(pan/zoom to inspect). Press OK in the picker to confirm.")

    def _on_scene_accepted(self):
        dlg = self._scene_dialog
        if dlg is not None and dlg.selected_feature is not None:
            self._scene_feature = dlg.selected_feature
            self.download_btn.setEnabled(True)
            self.status_label.setText(
                f"Scene selected: {self._scene_feature.get('id', '')}. "
                "Click 'Download and build'.")
        self._close_scene_dialog()

    def _on_scene_rejected(self):
        if self._scene_feature is None:
            self.status_label.setText("Selection cancelled.")
        self._close_scene_dialog()

    def _close_scene_dialog(self):
        dlg, self._scene_dialog = self._scene_dialog, None
        if dlg is not None:
            # close() triggers done(): the thumbnail worker is stopped and
            # the footprint rubber band cleared; deleteLater is safe here.
            dlg.close()
            dlg.deleteLater()

    # ---- download + build ----

    def _on_download(self):
        feat = self._scene_feature
        pending = self._pending
        if not feat or not pending:
            QMessageBox.warning(self, "Nothing to do",
                                "Search for scenes and select one first.")
            return
        products, bands = pending["products"], pending["bands"]
        if not products and not bands:
            QMessageBox.warning(self, "Nothing to do",
                                "Select at least one synthesis or individual band.")
            return
        out_dir, keep = self.output_dir, self.keep_bands.isChecked()
        source, mission = pending["source"], pending["mission"]

        def job(w):
            return pipeline.run(feat, products, out_dir, w.report, w.is_cancelled,
                                keep, band_names=bands, source=source,
                                mission=mission)

        self._start(job, self._on_build_done, "Starting...")

    def _on_build_done(self, outputs):
        # Layers are created in the GUI thread.
        layers = []
        try:
            for o in outputs:
                layers.append(add_to_project(o["path"], o["name"], o["palette"]))
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to add layer", str(e))
            return
        if layers:
            self._zoom_to(layers[0])
        self.status_label.setText(f"Done: {len(layers)} layer(s) added.")

    def _zoom_to(self, layer):
        canvas = self.iface.mapCanvas()
        extent = layer.extent()
        dst = canvas.mapSettings().destinationCrs()
        if layer.crs() != dst:
            extent = QgsCoordinateTransform(layer.crs(), dst, QgsProject.instance()).transformBoundingBox(extent)
        canvas.setExtent(extent)
        canvas.refresh()
