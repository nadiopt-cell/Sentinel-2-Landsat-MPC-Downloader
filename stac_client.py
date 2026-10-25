"""
Lightweight STAC client for Microsoft Planetary Computer.

 - POST /api/stac/v1/search for Sentinel-2 L2A and Landsat C2 L2 (with
   pagination: STAC 'next' links are followed until `limit` features are
   collected, so the result is no longer silently truncated to the first
   page); Landsat missions are filtered server-side via the 'platform'
   query property
 - helpers to read feature properties and sign asset URLs
"""

import re
from urllib.parse import urlparse

from .net import shared_session

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
S2L2A_COLLECTION = "sentinel-2-l2a"
LANDSAT_COLLECTION = "landsat-c2-l2"   # Landsat 4-9 (TM / ETM+ / OLI)

# Default L2A scaling: reflectance = DN * 0.0001 (+ offset, see dn_scale_offset)
DEFAULT_SCALE = 0.0001

# Most STAC servers cap the page size; request at most this many per page.
PAGE_SIZE = 100


def _clean_bbox(bbox):
    """Accept [minX, minY, maxX, maxY] or [(minX, minY), (maxX, maxY)]."""
    if isinstance(bbox[0], (list, tuple)):
        flat = [c for pt in bbox for c in pt]
    else:
        flat = list(bbox)
    if len(flat) != 4:
        raise ValueError("bbox must contain 4 numbers")
    return [float(c) for c in flat]


def _next_page_request(page_body, current_payload):
    """Decode a STAC 'next' link -> (method, url, payload) or None.

    Handles both styles seen in the wild:
      - stac-server (Planetary Computer): method POST, link 'body' with
        optional 'merge': true (link body is merged into the current one)
      - plain GET hrefs with the paging token already in the URL
    """
    for link in (page_body.get("links") or []):
        if link.get("rel") != "next" or not link.get("href"):
            continue
        href = link["href"]
        method = (link.get("method") or "GET").upper()
        if method == "POST":
            link_body = link.get("body") or {}
            if link.get("merge"):
                # current_payload is None when the previous page was a GET
                # link (which carries no body); merging then means "over {}".
                merged = dict(current_payload or {})
                merged.update(link_body)
                return "POST", href, merged
            return "POST", href, link_body
        return "GET", href, None
    return None


def _search_payload(bbox, dt_range, max_cloud, limit, collection,
                    platforms=None):
    """Build the POST /search body (kept pure for unit tests)."""
    query = {"eo:cloud_cover": {"lte": float(max_cloud)}}
    if platforms:
        query["platform"] = {"in": [str(p) for p in platforms]}
    return {
        "collections": [collection],
        "bbox": _clean_bbox(bbox),
        "datetime": dt_range,
        "query": query,
        "limit": max(1, min(int(limit), PAGE_SIZE)),
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }


def search(bbox, date_start, date_end, max_cloud=30, limit=100, timeout=60,
           max_pages=10, collection=S2L2A_COLLECTION, platforms=None):
    """Search scenes of `collection` intersecting bbox (EPSG:4326).

    date_start/date_end: "YYYY-MM-DD" (end date is inclusive) or full ISO.
    `platforms` (e.g. ["landsat-9"]) filters Landsat missions server-side.
    Newest scenes come first so the limit never hides recent acquisitions.
    Follows STAC 'next' links (up to `max_pages` requests) until `limit`
    unique features are collected; duplicate ids are dropped.
    Returns a list of STAC features (dicts).
    """
    def norm(d, end_of_day=False):
        if "T" in d:
            return d
        return f"{d}T23:59:59Z" if end_of_day else f"{d}T00:00:00Z"

    payload = _search_payload(bbox, f"{norm(date_start)}/{norm(date_end, True)}",
                              max_cloud, limit, collection, platforms)

    session = shared_session()
    method, url = "POST", STAC_SEARCH_URL
    features, seen = [], set()
    for _page in range(max(1, max_pages)):
        if method == "GET":
            resp = session.get(url, timeout=timeout)
        else:
            resp = session.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        for feat in body.get("features", []):
            fid = feat.get("id")
            if fid is None:
                features.append(feat)
            elif fid not in seen:
                seen.add(fid)
                features.append(feat)
        if len(features) >= limit:
            break
        nxt = _next_page_request(body, payload)
        if nxt is None:
            break
        method, url, payload = nxt

    return features[:limit]


def _is_blob_url(url):
    """Only Azure Blob Storage URLs need (and accept) a SAS token."""
    try:
        return urlparse(url).netloc.lower().endswith(".blob.core.windows.net")
    except Exception:
        return False


def signed_url(asset_href, token):
    """Append the SAS token to an Azure Blob asset URL (never urlencoded)."""
    if not token or not asset_href or not _is_blob_url(asset_href):
        return asset_href
    token = token.lstrip("?")
    sep = "&" if "?" in asset_href else "?"
    return f"{asset_href}{sep}{token}"


def feature_cloud_cover(feature):
    props = feature.get("properties", {}) or {}
    return props.get("eo:cloud_cover", props.get("s2:cloud_cover"))


def feature_datetime(feature):
    return (feature.get("properties", {}) or {}).get("datetime", "")


def feature_tile_id(feature):
    """Tile identifier: MGRS tile ('36UXV') for Sentinel-2,
    WRS-2 'path/row' ('095/022') for Landsat."""
    props = feature.get("properties", {}) or {}
    tile = props.get("s2:mgrs_tile") or props.get("mgrs_tile")
    if tile:
        return str(tile)
    path = props.get("landsat:wrs_path")
    row = props.get("landsat:wrs_row")
    if path and row:
        try:
            return f"{int(path):03d}/{int(row):03d}"
        except (TypeError, ValueError):
            return f"{path}/{row}"
    m = re.search(r"_T(\d{2}[A-Z]{3})_", feature.get("id", ""))
    if m:
        return m.group(1)
    # Landsat id fallback: ..._L2SP_095022_...
    m = re.search(r"_(\d{3})(\d{3})_", feature.get("id", ""))
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def feature_thumbnail_href(feature):
    assets = feature.get("assets", {}) or {}
    for key in ("rendered_preview", "thumbnail", "preview", "overview"):
        if key in assets:
            return assets[key].get("href")
    return None


def footprint_rings(feature):
    """Outer rings of the scene footprint as [[(x, y), ...], ...] (EPSG:4326).

    Reads the GeoJSON 'geometry' (Polygon or MultiPolygon); falls back to
    the 'bbox' property as a rectangle when the geometry is missing or
    unparsable. Returns [] when neither is usable. Pure function (Qt-free)
    so the GUI layer can turn it into a map outline.
    """
    rings = []
    try:
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates")
        polys = []
        if geom.get("type") == "Polygon" and coords:
            polys = [coords]
        elif geom.get("type") == "MultiPolygon" and coords:
            polys = coords
        elif coords is None:
            bbox = feature.get("bbox")
            if bbox and len(bbox) >= 4:
                x0, y0, x1, y1 = (float(c) for c in bbox[:4])
                polys = [[[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]]
        for poly in polys:
            if poly and poly[0] and len(poly[0]) >= 4:
                rings.append([(float(c[0]), float(c[1])) for c in poly[0]])
    except (TypeError, ValueError, AttributeError):
        return []
    return rings


_BAND_FALLBACK = {
    "B01": "coastal", "B02": "blue", "B03": "green", "B04": "red",
    "B05": "rededge1", "B06": "rededge2", "B07": "rededge3", "B08": "nir",
    "B8A": "nir08", "B09": "nir09", "B11": "swir16", "B12": "swir22",
}


def get_asset_href(feature, band):
    """Download href for a band or None.

    `band` is a Sentinel-2 key ('B04', 'B08', ...) or a Landsat asset key
    ('red', 'nir08', ...). Lookups are case-insensitive on the asset key.
    """
    assets = feature.get("assets", {}) or {}
    band = str(band)
    if band in assets:
        return assets[band].get("href")
    band_u = band.upper()
    if band_u in assets:
        return assets[band_u].get("href")
    for alt in (_BAND_FALLBACK.get(band), _BAND_FALLBACK.get(band_u)):
        if alt and alt in assets:
            return assets[alt].get("href")
    band_l = band.lower()
    for key in assets:                      # last resort: case-insensitive
        if key.lower() == band_l:
            return assets[key].get("href")
    return None


def dn_scale_offset(feature, band="B04", default=None):
    """Return (scale, offset) so that reflectance = DN * scale + offset.

    Reads scale/offset from the asset's raster:bands metadata when present
    (Landsat C2 L2: 0.0000275 / -0.2). Otherwise falls back to `default`
    (pass e.g. landsat.DEFAULT_SCALE/OFFSET) or, for Sentinel-2, to the
    processing-baseline logic: scenes with baseline >= 04.00 (after
    2022-01-25) carry a BOA_ADD_OFFSET of -1000 DN. Ignoring it biases
    NDVI/NDMI noticeably.
    """
    assets = feature.get("assets", {}) or {}
    band_u = str(band).upper()
    for key in (band, _BAND_FALLBACK.get(band, ""), _BAND_FALLBACK.get(band_u, "")):
        href_asset = assets.get(key)
        if not href_asset:
            continue
        for rb in (href_asset.get("raster:bands") or []):
            if "offset" in rb or "scale" in rb:
                scale = float(rb.get("scale", DEFAULT_SCALE))
                return scale, float(rb.get("offset", 0.0))
    if default is not None:
        return default

    props = feature.get("properties", {}) or {}
    try:
        baseline = float(props.get("s2:processing_baseline", 0))
    except (TypeError, ValueError):
        baseline = 0.0
    return DEFAULT_SCALE, (-1000 * DEFAULT_SCALE if baseline >= 4.0 else 0.0)
