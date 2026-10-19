"""Add result rasters to the QGIS project (styled for indices)."""

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

# Evenly spaced stops over [-1, 1].
PALETTES = {
    "RdYlGn": ["#d73027", "#fc8d59", "#ffffbf", "#91cf60", "#1a9850"],
    "RdBu": ["#b2182b", "#ef8a62", "#f7f7f7", "#67a9cf", "#2166ac"],
}


def _linear_interpolation():
    if hasattr(Qgis, "ShaderInterpolationMethod"):  # QGIS >= 3.34 / 4.x
        return Qgis.ShaderInterpolationMethod.Linear
    return QgsColorRampShader.Interpolated


def _pseudocolor_renderer(rlayer, palette_name, min_v=-1.0, max_v=1.0):
    colors = PALETTES.get(palette_name, PALETTES["RdYlGn"])
    n = len(colors) - 1
    items = [
        QgsColorRampShader.ColorRampItem(
            min_v + (max_v - min_v) * i / n, QColor(c), f"{min_v + (max_v - min_v) * i / n:.1f}")
        for i, c in enumerate(colors)
    ]
    ramp = QgsColorRampShader(min_v, max_v)
    ramp.setColorRampType(_linear_interpolation())
    ramp.setColorRampItemList(items)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(rlayer.dataProvider(), 1, shader)
    renderer.setClassificationMin(min_v)
    renderer.setClassificationMax(max_v)
    return renderer


def add_to_project(tif_path, name, palette=None):
    """Load a raster into the current project; returns the QgsRasterLayer."""
    rlayer = QgsRasterLayer(str(tif_path), name)
    if not rlayer.isValid():
        raise RuntimeError(f"Invalid raster: {tif_path}")
    if palette:
        rlayer.setRenderer(_pseudocolor_renderer(rlayer, palette))
    QgsProject.instance().addMapLayer(rlayer)
    return rlayer
