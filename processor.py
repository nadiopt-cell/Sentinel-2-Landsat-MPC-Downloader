"""
Raster processing with plain GDAL (osgeo) + numpy:
  - resample coarse bands to the mission's target resolution (parallel
    across bands) - Sentinel-2: 20/60 m -> 10 m; Landsat C2 L2 needs no
    resampling (all bands arrive on the same 30 m grid),
  - RGB composites (VRT -> compressed GeoTIFF),
  - normalized-difference indices computed in row strips (low memory).

Synthesis definitions are passed in as spec dicts (from syntheses.PRESETS
or landsat.PRODUCTS), so this module stays collection-agnostic.

No dependency on the QGIS Processing framework (works even if the
Processing plugin is disabled) and gdal.UseExceptions() is not called
globally, so other plugins' behaviour is not changed.  GDAL error text is
picked up via GetLastErrorMsg() and included in raised exceptions instead
of being lost to stderr.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from .downloader import Cancelled
from .syntheses import BAND_RESOLUTION, TARGET_RESOLUTION_M

NODATA_DN = 0           # Sentinel-2 L2A and Landsat C2 L2: DN 0 = fill/no data
NODATA_INDEX = -9999.0
STRIP_ROWS = 512
GTIFF_OPTS = ["COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"]


def _gdal():
    from osgeo import gdal
    return gdal


def _p(path):
    return Path(path).as_posix()


def _err(action):
    """Return `action` plus the last GDAL error message (if any).

    The plugin deliberately does not call gdal.UseExceptions() globally
    (that would change behaviour for other plugins), so GDAL reports
    errors by returning None and printing to stderr.  This helper keeps
    the real cause visible to the user.
    """
    try:
        msg = _gdal().GetLastErrorMsg()
    except Exception:
        msg = b""
    text = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else (msg or "")
    return f"{action}: {text}" if text else action


# ---------------------------------------------------------------- resample --

def resample_band(src_path, dst_path, target_res=None):
    """Resample a band to `target_res` m (bilinear); reuse if present."""
    if target_res is None:
        target_res = TARGET_RESOLUTION_M
    dst = Path(dst_path)
    if dst.exists() and dst.stat().st_size > 0:
        return str(dst)
    gdal = _gdal()
    ds = gdal.Open(_p(src_path))
    if ds is None:
        raise RuntimeError(_err(f"Cannot open {src_path}"))
    tmp = dst.with_name(dst.name + ".part")
    try:
        gt = ds.GetGeoTransform()
        factor = abs(gt[1]) / target_res
        width, height = round(ds.RasterXSize * factor), round(ds.RasterYSize * factor)
        minx, maxy = gt[0], gt[3]
        maxx, miny = minx + gt[1] * ds.RasterXSize, maxy + gt[5] * ds.RasterYSize
        dst.parent.mkdir(parents=True, exist_ok=True)
        opts = gdal.WarpOptions(
            format="GTiff", width=width, height=height,
            outputBounds=(minx, miny, maxx, maxy),
            resampleAlg="bilinear", srcNodata=NODATA_DN, dstNodata=NODATA_DN,
            multithread=True, creationOptions=GTIFF_OPTS + ["TILED=YES"],
        )
        out = gdal.Warp(_p(tmp), ds, options=opts)
        if out is None:
            raise RuntimeError(_err(f"gdal.Warp failed for {src_path}"))
        out = None  # close the warp result before renaming (Windows locks)
        tmp.replace(dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        ds = None  # close the source even on failure
    return str(dst)


def resample_bands(band_paths, work_dir, cancelled=None, max_workers=3,
                   band_res=None, target_res=None):
    """Return {band: path} with every band at `target_res` m.

    band_res maps band -> native resolution; bands whose native resolution
    exceeds the target are warped. `band_res=None` means the Sentinel-2
    table; pass {} (all bands already at target) for Landsat C2 L2.
    Bands needing resampling are warped in a small thread pool: GDAL
    releases the GIL inside Warp, so per-band jobs overlap I/O and CPU
    (a 10-band scene previously resampled strictly one after another).
    """
    if target_res is None:
        target_res = TARGET_RESOLUTION_M
    if band_res is None:
        band_res = BAND_RESOLUTION
    work_dir = Path(work_dir)
    out = {}
    todo = {}
    for band, p in band_paths.items():
        if not p:
            continue
        if band_res.get(band, target_res) > target_res:
            todo[band] = p
        else:
            out[band] = p
    if not todo:
        return out
    if cancelled is not None and cancelled():
        raise Cancelled()

    workers = max(1, min(max_workers, len(todo)))
    if workers == 1:
        for band, p in todo.items():
            if cancelled is not None and cancelled():
                raise Cancelled()
            out[band] = resample_band(p, work_dir / f"{band}_{target_res}m.tif",
                                      target_res)
        return out

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(resample_band, p,
                        work_dir / f"{band}_{target_res}m.tif", target_res): band
            for band, p in todo.items()
        }
        for fut in as_completed(futures):
            out[futures[fut]] = fut.result()  # propagates the first failure
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python < 3.9
            pool.shutdown(wait=False)
    if cancelled is not None and cancelled():
        raise Cancelled()
    return out


# --------------------------------------------------------------------- RGB --

def build_rgb(name, spec, band_paths, out_path):
    """Build an RGB composite from a spec dict {type: 'rgb', R/G/B: band}."""
    if spec.get("type") != "rgb":
        raise ValueError(f"{name} is not an RGB synthesis")
    rgb = [band_paths.get(spec[c]) for c in "RGB"]
    if not all(rgb):
        missing = [spec[c] for c in "RGB" if not band_paths.get(spec[c])]
        raise RuntimeError(f"Missing bands for {name}: {', '.join(missing)}")

    gdal = _gdal()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_path = out_path.with_suffix(".vrt")
    vrt = gdal.BuildVRT(_p(vrt_path), [_p(p) for p in rgb], separate=True,
                        srcNodata=NODATA_DN, VRTNodata=NODATA_DN)
    if vrt is None:
        raise RuntimeError(_err("gdal.BuildVRT failed"))
    vrt = None
    try:
        res = gdal.Translate(
            _p(out_path), _p(vrt_path),
            options=gdal.TranslateOptions(
                format="GTiff", noData=NODATA_DN,
                creationOptions=GTIFF_OPTS + ["TILED=YES", "PHOTOMETRIC=RGB"]),
        )
        if res is None:
            raise RuntimeError(_err("gdal.Translate failed"))
        res = None
    finally:
        try:
            vrt_path.unlink()
        except OSError:
            pass
    return str(out_path)


# ------------------------------------------------------------------- index --

def normalized_difference(a, b, scale=0.0001, offset=0.0):
    """(A - B) / (A + B) on DN arrays -> float32, nodata -9999.

    Converts DN to reflectance in float32 first (avoids uint16 wrap-around)
    and masks nodata (DN == 0) and zero denominators.
    """
    valid = (a != NODATA_DN) & (b != NODATA_DN)
    fa = a.astype(np.float32) * np.float32(scale) + np.float32(offset)
    fb = b.astype(np.float32) * np.float32(scale) + np.float32(offset)
    den = fa + fb
    valid &= np.abs(den) > 1e-6
    out = np.full(a.shape, NODATA_INDEX, dtype=np.float32)
    np.divide(fa - fb, den, out=out, where=valid)
    out[valid] = np.clip(out[valid], -1.0, 1.0)
    return out


def build_index(name, spec, band_paths, out_path, scale=0.0001, offset=0.0,
                cancelled=lambda: False):
    """Build a normalized-difference index from a spec dict."""
    if spec.get("type") != "index" or spec.get("kind") != "normalized_difference":
        raise ValueError(f"{name} is not a normalized-difference index")
    pa, pb = (band_paths.get(b) for b in spec["bands"])
    if not (pa and pb):
        raise RuntimeError(f"Missing bands for {name}: {spec['bands']}")

    gdal = _gdal()
    ds_a, ds_b = gdal.Open(_p(pa)), gdal.Open(_p(pb))
    if ds_a is None or ds_b is None:
        raise RuntimeError(_err("Cannot open input bands"))
    xs, ys = ds_a.RasterXSize, ds_a.RasterYSize
    if (ds_b.RasterXSize, ds_b.RasterYSize) != (xs, ys):
        raise RuntimeError("Band grids differ; resample first")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(_p(out_path), xs, ys, 1, gdal.GDT_Float32,
                     options=["COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER"])
    if dst is None:
        raise RuntimeError(_err(f"Cannot create {out_path}"))
    dst.SetGeoTransform(ds_a.GetGeoTransform())
    dst.SetProjection(ds_a.GetProjection())
    band = dst.GetRasterBand(1)
    band.SetNoDataValue(NODATA_INDEX)
    ba, bb = ds_a.GetRasterBand(1), ds_b.GetRasterBand(1)
    try:
        for y in range(0, ys, STRIP_ROWS):
            if cancelled():
                raise Cancelled()
            h = min(STRIP_ROWS, ys - y)
            a = ba.ReadAsArray(0, y, xs, h)
            b = bb.ReadAsArray(0, y, xs, h)
            band.WriteArray(normalized_difference(a, b, scale, offset), 0, y)
        band.FlushCache()
    except Cancelled:
        # `finally` below closes the datasets first: on Windows removing an
        # open file fails with PermissionError and would swallow Cancelled.
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        # Drop every dataset/band reference so the file handles really close.
        band = dst = ds_a = ds_b = None
    return str(out_path)
