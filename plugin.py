"""QGIS plugin entry point: toolbar button + menu entry opening the dialog."""

import os

try:
    from qgis.PyQt.QtGui import QAction      # Qt6 / QGIS 4: QAction lives here
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5 / QGIS 3.x
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMessageBox


def resources_path(*parts):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", *parts)


class SentinelPlugin:
    PLUGIN_NAME = "Sentinel-2 & Landsat MPC Downloader"

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):  # noqa: N802  (QGIS API name)
        self.action = QAction(QIcon(resources_path("icon.svg")), self.PLUGIN_NAME,
                              self.iface.mainWindow())
        self.action.setObjectName("SentinelPluginAction")
        self.action.setStatusTip(
            "Search and download Sentinel-2 L2A and Landsat C2 L2 "
            "from Microsoft Planetary Computer")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Sentinel / Landsat", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&Sentinel / Landsat", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        if self.dialog is None:
            try:  # lazy import: a missing dependency must not break plugin loading
                from .main_dialog import MainDialog
            except ImportError as e:
                QMessageBox.critical(self.iface.mainWindow(), self.PLUGIN_NAME,
                                     f"Cannot start the plugin: {e}")
                return
            self.dialog = MainDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
