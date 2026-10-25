"""
Sentinel-2 L2A band syntheses and indices.

Band naming follows Sentinel-2 naming convention (B01..B12).
Resolutions (native):
  10 m: B02, B03, B04, B08
  20 m: B05, B06, B07, B11, B12, B8A
  60 m: B01, B09, B10

For syntheses involving 20 m bands (B11, B12) we resample to 10 m
to keep alignment and resolution consistent across the final rasters.
"""

# 8 RGB синтезов с gisgeography.com + 2 индекса (NDVI, NDMI)
PRESETS = {
    "True Color": {
        "type": "rgb",
        "R": "B04", "G": "B03", "B": "B02",
        "description": "Natural colors as seen by the eye.",
    },
    "Color Infrared": {
        "type": "rgb",
        "R": "B08", "G": "B04", "B": "B03",
        "description": "Healthy vegetation -> red.",
    },
    "SWIR": {
        "type": "rgb",
        "R": "B12", "G": "B08", "B": "B04",
        "description": "Highlights burned areas and bare soil.",
    },
    "Agriculture": {
        "type": "rgb",
        "R": "B11", "G": "B08", "B": "B02",
        "description": "Used to monitor crop health.",
    },
    "Geology": {
        "type": "rgb",
        "R": "B12", "G": "B11", "B": "B02",
        "description": "Highlights geological features.",
    },
    "Bathymetric": {
        "type": "rgb",
        "R": "B04", "G": "B03", "B": "B01",
        "description": "Shallow water / coastal bathymetry.",
    },
    "Urban": {
        "type": "rgb",
        "R": "B12", "G": "B11", "B": "B04",
        "description": "Urban areas appear in purple/violet.",
    },
    "Snow": {
        "type": "rgb",
        "R": "B11", "G": "B03", "B": "B04",
        "description": "Snow vs clouds discrimination.",
    },
    "NDVI": {
        "type": "index",
        "kind": "normalized_difference",  # (bands[0] - bands[1]) / (bands[0] + bands[1])
        "expr": "(B08 - B04) / (B08 + B04)",
        "bands": ["B08", "B04"],
        "palette": "RdYlGn",
        "description": "Normalized Difference Vegetation Index.",
    },
    "NDMI": {
        "type": "index",
        "kind": "normalized_difference",
        "expr": "(B08 - B11) / (B08 + B11)",
        "bands": ["B08", "B11"],
        "palette": "RdBu",
        "description": "Normalized Difference Moisture Index.",
    },
}

# Native resolutions (meters) per band
BAND_RESOLUTION = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10,
    "B05": 20, "B06": 20, "B07": 20, "B08": 10,
    "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
}

# Target resolution for the final raster: 10 m (matches B02/B03/B04/B08)
TARGET_RESOLUTION_M = 10


def required_bands(preset_names):
    """Return a set of band names needed to fulfil all given presets."""
    bands = set()
    for name in preset_names:
        spec = PRESETS[name]
        if spec["type"] == "rgb":
            bands.update([spec["R"], spec["G"], spec["B"]])
        elif spec["type"] == "index":
            bands.update(spec["bands"])
    return bands


# Individual L2A bands offered as standalone downloads (spectral order).
# B10 (cirrus, L1C-only) is not delivered in Level-2A products.
INDIVIDUAL_BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07",
                    "B08", "B8A", "B09", "B11", "B12"]

BAND_LABELS = {
    "B01": "Coastal / aerosol",
    "B02": "Blue",
    "B03": "Green",
    "B04": "Red",
    "B05": "Red edge 1",
    "B06": "Red edge 2",
    "B07": "Red edge 3",
    "B08": "NIR",
    "B8A": "Narrow NIR",
    "B09": "Water vapour",
    "B11": "SWIR1 (1.6 um)",
    "B12": "SWIR2 (2.2 um)",
}


def product_bands(product_names, extra_bands=()):
    """Union of the bands required by the selected presets and any
    individually requested bands (None entries are ignored)."""
    bands = required_bands(product_names)
    bands.update(b for b in (extra_bands or ()) if b)
    return bands
