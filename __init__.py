"""Sentinel-2 MPC Downloader — QGIS plugin package."""


def classFactory(iface):  # noqa: N802  (QGIS API name)
    from .plugin import SentinelPlugin
    return SentinelPlugin(iface)
