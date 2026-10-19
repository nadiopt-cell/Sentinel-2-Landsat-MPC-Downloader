"""
Landsat Collection 2 Level-2 on Microsoft Planetary Computer: mission
registry (Landsat 4-9), band asset keys, standard RGB syntheses and NDVI.

Facts verified against the MPC STAC API (2026-10):
  - collection id: "landsat-c2-l2", platforms: landsat-4 .. landsat-9
    (Landsat 1-3 / MSS is NOT hosted on Planetary Computer)
  - every item carries the six reflectance bands on a common 30 m grid:
    blue, green, red, nir08, swir16, swir22
  - OLI missions (8/9) additionally have "coastal"; TM/ETM+ (4/5/7)
    additionally have "lwir" (thermal). Landsat 8/9 "L2SR" scenes lack
    the thermal band, so it is not offered for 8/9.
  - reflectance = DN * 0.0000275 - 0.2 (raster:bands of each asset)

Band keys are shared by all missions, so the synthesis definitions are
mission-independent; only the *available* products and bands differ per
mission (Landsat 4: RGB only, no indices - per user requirements).
"""

COLLECTION = "landsat-c2-l2"

# Landsat C2 L2 surface reflectance scaling (fallback when an asset has
# no raster:bands metadata): reflectance = DN * SCALE + OFFSET.
DEFAULT_SCALE = 2.75e-05
DEFAULT_OFFSET = -0.2

# All C2 L2 bands are delivered on a 30 m grid -> nothing to resample.
TARGET_RESOLUTION_M = 30

# Standard RGB syntheses + NDVI. R/G/B values are band asset keys.
PRODUCTS = {
    "True Color": {
        "type": "rgb",
        "R": "red", "G": "green", "B": "blue",
        "description": "Natural colors (red/green/blue, 30 m).",
    },
    "False Color": {
        "type": "rgb",
        "R": "nir08", "G": "red", "B": "green",
        "description": "Vegetation appears bright red (NIR/Red/Green).",
    },
    "SWIR": {
        "type": "rgb",
        "R": "swir22", "G": "nir08", "B": "red",
        "description": "SWIR2/NIR/Red: burned areas, bare soil, moisture.",
    },
    "Agriculture": {
        "type": "rgb",
        "R": "swir16", "G": "nir08", "B": "blue",
        "description": "SWIR1/NIR/Blue: crop health monitoring.",
    },
    "NDVI": {
        "type": "index",
        "kind": "normalized_difference",  # (bands[0] - bands[1]) / (bands[0] + bands[1])
        "expr": "(NIR - Red) / (NIR + Red)",
        "bands": ["nir08", "red"],
        "palette": "RdYlGn",
        "description": "Normalized Difference Vegetation Index.",
    },
}

_FULL_PRODUCT_SET = ["True Color", "False Color", "SWIR", "Agriculture", "NDVI"]
_RGB_ONLY = ["True Color", "False Color"]  # Landsat 4: only per-band + RGB

# Ordered mission registry; keys are STAC "platform" values.
MISSIONS = {
    "landsat-9": {
        "label": "Landsat 9 (OLI/TIRS, 2021 -)",
        "bands": ["coastal", "blue", "green", "red", "nir08", "swir16", "swir22"],
        "products": _FULL_PRODUCT_SET,
    },
    "landsat-8": {
        "label": "Landsat 8 (OLI/TIRS, 2013 -)",
        "bands": ["coastal", "blue", "green", "red", "nir08", "swir16", "swir22"],
        "products": _FULL_PRODUCT_SET,
    },
    "landsat-7": {
        "label": "Landsat 7 (ETM+, 1999 -)",
        "bands": ["blue", "green", "red", "nir08", "swir16", "swir22", "lwir"],
        "products": _FULL_PRODUCT_SET,
    },
    "landsat-5": {
        "label": "Landsat 5 (TM, 1984 - 2012)",
        "bands": ["blue", "green", "red", "nir08", "swir16", "swir22", "lwir"],
        "products": _FULL_PRODUCT_SET,
    },
    "landsat-4": {
        "label": "Landsat 4 (TM, 1982 - 1993)",
        "bands": ["blue", "green", "red", "nir08", "swir16", "swir22", "lwir"],
        "products": _RGB_ONLY,
    },
}

BAND_LABELS = {
    "coastal": "Coastal / aerosol",
    "blue": "Blue",
    "green": "Green",
    "red": "Red",
    "nir08": "NIR",
    "swir16": "SWIR1 (1.6 um)",
    "swir22": "SWIR2 (2.2 um)",
    "lwir": "Thermal",
}

MISSION_KEYS = list(MISSIONS.keys())


def mission_label(platform):
    """Combo-box label for a mission platform key."""
    return MISSIONS.get(platform, {}).get("label", platform)


def products_for(platform):
    """{name: spec} with the syntheses/indices available for a mission."""
    names = MISSIONS.get(platform, {}).get("products", [])
    return {n: PRODUCTS[n] for n in names if n in PRODUCTS}


def bands_for(platform):
    """Individual band asset keys offered for a mission (in spectral order)."""
    return list(MISSIONS.get(platform, {}).get("bands", []))


def required_bands(platform, product_names, band_names=()):
    """All asset keys needed for the selected products + individual bands."""
    bands = {b for b in band_names if b in bands_for(platform)}
    for name in product_names:
        spec = PRODUCTS.get(name, {})
        if spec.get("type") == "rgb":
            bands.update([spec["R"], spec["G"], spec["B"]])
        elif spec.get("type") == "index":
            bands.update(spec["bands"])
    known = set(bands_for(platform))
    return {b for b in bands if b in known}
