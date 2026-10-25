"""
Qt-free download + build pipeline (runs in a worker thread).

Handles both data sources:
  - "S2":      Sentinel-2 L2A (syntheses.PRESETS, resampling to 10 m)
  - "Landsat": Landsat C2 L2, missions 4-9 (landsat.PRODUCTS + individual
               band downloads, all bands already on a 30 m grid)

Everything touching QgsProject/QgsRasterLayer stays in the GUI thread;
this module only returns the list of produced files.
"""

import re
from pathlib import Path

from . import downloader, landsat, processor, stac_client, syntheses
from .downloader import Cancelled
from .token_cache import get_token


def _safe_name(name, fallback="scene"):
    """Filesystem-safe folder/file name (feature ids are trusted in
    practice, but defensive sanitisation costs nothing)."""
    cleaned = re.sub(r"[^\w\-.]+", "_", str(name)).strip("._")
    return cleaned or fallback


def run(feature, product_names, out_dir, report, cancelled, keep_bands=True,
        band_names=(), source="S2", mission=None):
    """Download required bands and build the selected products.

    product_names: syntheses/indices to build (per source registry).
    band_names:    Landsat individual bands to also download as rasters.
    mission:       STAC platform key (Landsat only, e.g. "landsat-9").
    report(percent, message); cancelled() -> bool.
    Returns [{"path", "name", "palette"}].
    """
    scene_id = feature.get("id", "scene")
    scene_dir = Path(out_dir) / _safe_name(scene_id)
    scene_dir.mkdir(parents=True, exist_ok=True)

    if source == "Landsat":
        specs = landsat.products_for(mission or "")
        unknown = [n for n in product_names if n not in specs]
        if unknown:
            raise RuntimeError(
                f"{', '.join(unknown)} is not available for this mission")
        collection = landsat.COLLECTION
        target_res = landsat.TARGET_RESOLUTION_M
        band_res = {}                       # C2 L2: everything already 30 m
        band_defaults = ("red", (landsat.DEFAULT_SCALE, landsat.DEFAULT_OFFSET))
        bands = landsat.required_bands(mission or "", product_names, band_names)
    else:
        specs = {n: syntheses.PRESETS[n] for n in product_names}
        collection = stac_client.S2L2A_COLLECTION
        target_res = syntheses.TARGET_RESOLUTION_M
        band_res = syntheses.BAND_RESOLUTION
        band_defaults = ("B04", None)
        bands = syntheses.product_bands(product_names, band_names)

    token = get_token(collection)
    pairs, url_to_band = [], {}
    for band in sorted(bands):
        href = stac_client.get_asset_href(feature, band)
        if not href:
            raise RuntimeError(f"Scene has no asset for band {band}")
        url = stac_client.signed_url(href, token)
        url_to_band[url] = band
        pairs.append((url, str(scene_dir / f"{band}.tif")))

    report(1, f"Downloading {len(bands)} band(s): {', '.join(sorted(bands))}")

    def on_dl(done, total, files_done, files_total):
        # `total` grows as Content-Length headers arrive and can briefly be
        # smaller than `done` (chunked transfers): cap the phase at 40%.
        pct = min(40, int(40 * done / total)) if total else 1
        mb = done / 1e6
        report(max(1, pct), f"Downloading... {mb:.0f} MB ({files_done}/{files_total} files)")

    results = downloader.download(pairs, progress_cb=on_dl, cancelled=cancelled)
    band_paths = {url_to_band[u]: p for u, p in results.items()}

    report(45, f"Resampling to {target_res} m where needed...")
    bands_at_target = processor.resample_bands(
        band_paths, scene_dir / f"{target_res}m", cancelled=cancelled,
        band_res=band_res, target_res=target_res)
    if cancelled():
        raise Cancelled()

    scale_band, scale_default = band_defaults
    # The red-band (scale, offset) pair is applied to both channels of an
    # index. This holds for every supported product: all Sentinel-2 L2A
    # bands share the same baseline offset grid, and Landsat C2 L2 scales
    # every reflectance asset identically (2.75e-05 / -0.2). An index that
    # mixes differently scaled bands (e.g. a future NDSI) must read the
    # scale/offset per band instead of reusing this pair.
    scale, offset = stac_client.dn_scale_offset(feature, scale_band,
                                                default=scale_default)

    outputs = []
    for i, name in enumerate(product_names):
        if cancelled():
            raise Cancelled()
        spec = specs[name]
        out_path = scene_dir / f"{_safe_name(name, 'synthesis')}.tif"
        report(50 + int(50 * i / max(1, len(product_names))),
               f"Building {name}...")
        if spec["type"] == "rgb":
            processor.build_rgb(name, spec, bands_at_target, out_path)
        else:
            processor.build_index(name, spec, bands_at_target, out_path,
                                  scale, offset, cancelled)
        outputs.append({
            "path": str(out_path),
            "name": f"{name} - {scene_id}",
            "palette": spec.get("palette") if spec["type"] == "index" else None,
        })

    # Individual band downloads: the downloaded (native-resolution) file
    # itself is the product. It must survive keep_bands=False cleanup.
    for band in band_names:
        p = bands_at_target.get(band) or band_paths.get(band)
        if not p:
            continue
        label_map = landsat.BAND_LABELS if source == "Landsat" else syntheses.BAND_LABELS
        label = label_map.get(band, band)
        outputs.append({
            "path": str(p),
            "name": f"{label} band - {scene_id}",
            "palette": None,
        })

    if not keep_bands:
        # dict.fromkeys dedupes: resampled bands appear in both dicts under
        # the same path, and deleting the same file twice used to raise/hide
        # FileNotFoundError. Files that ARE products (individual band
        # downloads) are never deleted.
        product_files = {o["path"] for o in outputs}
        for p in dict.fromkeys(list(band_paths.values())
                               + list(bands_at_target.values())):
            if str(p) in product_files:
                continue
            try:
                Path(p).unlink()
            except OSError:
                pass
    report(100, "Done.")
    return outputs
