"""
ASTER L1T on Microsoft Planetary Computer: swath-grouped bands, standard
syntheses and a relative NDVI.

Facts verified against the MPC STAC API (2026-10):
  - collection id: "aster-l1t", platform "terra"; the Planetary Computer
    copy of the archive spans 2000-03-04 .. 2006-12-31 (searches with
    newer dates return nothing)
  - every granule carries up to three multi-band GeoTIFF (COG) assets:
      VNIR: B01, B02, B3N  (15 m, uint8)
      SWIR: B04..B09       (30 m, uint8)
      TIR:  B10..B14       (90 m, uint16)
    The band order inside each asset follows the ASTER band numbers: the
    COG band descriptions read "ImageData1 VNIR_Swath" .. "ImageData14
    TIR_Swath", so list index + 1 is the GDAL band index.
  - roughly a quarter of the granules (night or fully overcast scenes)
    carry only the TIR asset; searches support the eo:cloud_cover query
  - DN values are at-sensor radiance counts with fill value 0; the STAC
    metadata exposes no scale/offset (and the COGs carry no nodata tag,
    so the extractor must set it), therefore indices are computed on
    (DN - 1) and stay relative - ASTER L1T is not atmospherically
    corrected and the per-band unit conversion coefficients are not
    available via STAC.

Every standard product draws its bands from a single swath, so composites
never mix the 15 / 30 / 90 m grids and no resampling is needed.
"""

COLLECTION = "aster-l1t"

# radiance = (DN - 1) * unit_conversion_coefficient; the coefficients are
# not exposed via STAC, so (1.0, -1.0) keeps ratio indices relative
# instead of pretending absolute radiance. The -1 matters: DN 1 is zero
# radiance in the ASTER L1T product.
INDEX_SCALE = 1.0
INDEX_OFFSET = -1.0

# Swath groups: asset key -> (native resolution in m, ordered band keys).
# The ordered band lists define the GDAL band indices (1-based) inside
# the downloaded asset.
SWATHS = {
    "VNIR": {"resolution": 15, "bands": ["B01", "B02", "B3N"]},
    "SWIR": {"resolution": 30, "bands": ["B04", "B05", "B06", "B07", "B08", "B09"]},
    "TIR": {"resolution": 90, "bands": ["B10", "B11", "B12", "B13", "B14"]},
}

# Standard RGB syntheses + NDVI. R/G/B values are ASTER band keys.
PRODUCTS = {
    "False Color": {
        "type": "rgb",
        "R": "B3N", "G": "B02", "B": "B01",
        "description": "Vegetation appears bright red (NIR/Red/Green, 15 m).",
    },
    "Geology": {
        "type": "rgb",
        "R": "B04", "G": "B06", "B": "B08",
        "description": "SWIR false color 4/6/8: clays, carbonates, "
                       "iron oxides (30 m).",
    },
    "NDVI": {
        "type": "index",
        "kind": "normalized_difference",  # (bands[0] - bands[1]) / (bands[0] + bands[1])
        "expr": "(B3N - B02) / (B3N + B02)",
        "bands": ["B3N", "B02"],
        "palette": "RdYlGn",
        "description": "Relative vegetation index from radiance DNs "
                       "(15 m, not atmospherically corrected).",
    },
}

# All ASTER bands in spectral order (VNIR 1-3N, SWIR 4-9, TIR 10-14).
INDIVIDUAL_BANDS = [b for s in SWATHS.values() for b in s["bands"]]
BAND_GROUPS = {b: g for g, s in SWATHS.items() for b in s["bands"]}
BAND_RESOLUTION = {b: s["resolution"] for s in SWATHS.values() for b in s["bands"]}

BAND_LABELS = {
    "B01": "VNIR 1 (green, 0.56 um)",
    "B02": "VNIR 2 (red, 0.66 um)",
    "B3N": "VNIR 3N (NIR, 0.82 um)",
    "B04": "SWIR 4 (1.65 um)",
    "B05": "SWIR 5 (2.17 um)",
    "B06": "SWIR 6 (2.21 um)",
    "B07": "SWIR 7 (2.26 um)",
    "B08": "SWIR 8 (2.33 um)",
    "B09": "SWIR 9 (2.40 um)",
    "B10": "TIR 10 (8.3 um)",
    "B11": "TIR 11 (8.65 um)",
    "B12": "TIR 12 (9.1 um)",
    "B13": "TIR 13 (10.6 um)",
    "B14": "TIR 14 (11.3 um)",
}

# Archive extent on Planetary Computer (STAC temporal extent, 2026-10).
ARCHIVE_START = "2000-03-04"
ARCHIVE_END = "2006-12-31"


def asset_href(feature, asset):
    """Download href of a swath asset ('VNIR'/'SWIR'/'TIR') or None."""
    return ((feature.get("assets") or {}).get(asset) or {}).get("href")


def available_groups(feature):
    """Swath assets actually present on the granule."""
    assets = feature.get("assets") or {}
    return {g for g in SWATHS if g in assets}


def swaths_for(bands):
    """{asset: [ordered band keys]} covering `bands` (unknown keys ignored).

    Band lists keep the swath order, so list index + 1 is the GDAL band
    number inside the downloaded asset.
    """
    wanted = {b for b in bands if b in BAND_GROUPS}
    groups = {}
    for asset, spec in SWATHS.items():
        sel = [b for b in spec["bands"] if b in wanted]
        if sel:
            groups[asset] = sel
    return groups


def required_bands(product_names, band_names=()):
    """All band keys needed for the selected products + individual bands."""
    bands = {b for b in band_names if b in INDIVIDUAL_BANDS}
    for name in product_names:
        spec = PRODUCTS.get(name, {})
        if spec.get("type") == "rgb":
            bands.update([spec["R"], spec["G"], spec["B"]])
        elif spec.get("type") == "index":
            bands.update(spec["bands"])
    return bands
