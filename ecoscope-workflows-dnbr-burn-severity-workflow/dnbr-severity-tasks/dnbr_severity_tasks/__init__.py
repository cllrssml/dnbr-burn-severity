"""
Custom tasks for the dNBR Burn Severity workflow on the Ecoscope Platform.

Maps burn severity across an EarthRanger area of interest for a chosen fire
window, using the Differenced Normalized Burn Ratio (dNBR) and the standard
USGS severity classes. Sentinel-2 (default, 10-20 m) or Landsat (30 m, archive
back to 1984) can be selected.

Science (all coefficients verified against primary sources, 2026-07-02):
  - Compositing:     Parks et al. 2018 (doi:10.3390/rs10060879) — mean compositing on GEE.
  - dNBR thresholds: Key & Benson 2006 (USGS standard severity classes; 0.1/0.27/0.44/0.66).
  - MIRBI index:     Trigg & Flasse 2001 (Int. J. Remote Sensing 22:13) — MIRBI = 10·SWIR2 − 9.8·SWIR1 + 2.
  - Dual-index tier: Bastarrika et al. 2024 (ISPRS J. Photogramm. Remote Sens. 218).
  - Landsat C2 L2:   USGS scale factor reflectance = DN·0.0000275 − 0.2 (offset must be applied
                     before the normalized difference — it does not cancel).
  - Sentinel-2:      COPERNICUS/S2_SR_HARMONIZED already removes the PB 04.00 −1000 offset, so
                     the DN→reflectance scale is purely multiplicative and cancels in the ratio.
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import geopandas as gpd
import numpy as np
from pydantic import Field
from pydantic.json_schema import WithJsonSchema
from wt_registry import register

# Type shims — no ecoscope imports at module level (compiler task discovery runs before
# ecoscope is on sys.path). Pattern identical to dnbr-tasks / burn-scar-tasks.
_GDF = Annotated[Any, WithJsonSchema({"type": "ecoscope.platform.annotations.DataFrame"})]
_GEE = Annotated[str, WithJsonSchema({"type": "string", "description": "A named Google Earth Engine connection."})]


# ── Severity class table ─────────────────────────────────────────────────────
# Key & Benson 2006 thresholds on the ×1000 dNBR convention.
# Each entry: (label, dNBR_low_inclusive, dNBR_high_exclusive, rgba_uint8, hex_str)
SEVERITY_CLASSES = [
    ("Enhanced Regrowth", -np.inf,  -100, [0,   102,  0,   220], "#006600"),
    ("Unburned",          -100,      100, [200, 200,  200, 200], "#C8C8C8"),
    ("Low",                100,      270, [255, 255,  0,   220], "#FFFF00"),
    ("Moderate-Low",       270,      440, [255, 165,  0,   220], "#FFA500"),
    ("Moderate-High",      440,      660, [220,  50,  0,   220], "#DC3200"),
    ("High",               660, np.inf,  [153,   0,  0,   220], "#990000"),
]
_CLASS_NAMES = [c[0] for c in SEVERITY_CLASSES]
_CLASS_HEX   = [c[4] for c in SEVERITY_CLASSES]

# Burned map legend shows only the burned classes (Low and above); Enhanced Regrowth
# and Unburned are not vectorised so they should not clutter the legend.
_BURNED_NAMES = [c[0] for c in SEVERITY_CLASSES[2:]]
_BURNED_HEX   = [c[4] for c in SEVERITY_CLASSES[2:]]


# ── GEE index helpers (module-level, each with its own lazy `import ee`) ──────
# Defined at module level so they can be passed as ee.ImageCollection.map()
# callbacks without triggering Python's closure-scoping trap.
# Each returns a two-band image (NBR, MIRBI) for one masked scene.

def _s2_pre_indices(img):
    """Sentinel-2 NBR + MIRBI for the PRE-fire composite.

    Aggressive SCL mask (keep only clear vegetation 4 / bare soil 5 / water 6).
    Safe pre-fire: there is no burn signal to protect.
    """
    import ee
    scl = img.select("SCL")
    m = img.updateMask(scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)))
    nbr = m.normalizedDifference(["B8A", "B12"]).rename("NBR")          # 20 m NIR / SWIR2
    b11 = m.select("B11").toFloat().divide(10000)
    b12 = m.select("B12").toFloat().divide(10000)
    mirbi = b12.multiply(10).subtract(b11.multiply(9.8)).add(2).rename("MIRBI")
    return nbr.addBands(mirbi)


def _s2_post_indices(img):
    """Sentinel-2 NBR + MIRBI for the POST-fire composite.

    Cloud-probability mask (MSK_CLDPRB ≤ 40) instead of SCL: Sen2Cor frequently
    misclassifies fresh burn scars as SCL 2 (dark) / 3 (cloud shadow), which would
    erase the burn signal. Threshold follows the DE Africa burn-mapping reference.
    """
    import ee
    m = img.updateMask(img.select("MSK_CLDPRB").lte(40))
    nbr = m.normalizedDifference(["B8A", "B12"]).rename("NBR")
    b11 = m.select("B11").toFloat().divide(10000)
    b12 = m.select("B12").toFloat().divide(10000)
    mirbi = b12.multiply(10).subtract(b11.multiply(9.8)).add(2).rename("MIRBI")
    return nbr.addBands(mirbi)


def _l89_indices(img):
    """Landsat 8/9 OLI/OLI-2 (C2 L2) NBR + MIRBI, with C2 scale factor applied.

    NIR=SR_B5, SWIR1=SR_B6, SWIR2=SR_B7. QA_PIXEL bits masked: 1=dilated cloud,
    3=cloud, 4=cloud shadow. Reflectance = DN·0.0000275 − 0.2 applied BEFORE the
    ratio (the additive offset does not cancel in a normalized difference).
    """
    import ee
    qa = img.select("QA_PIXEL")
    mask = (
        qa.bitwiseAnd(1 << 1).eq(0)
        .And(qa.bitwiseAnd(1 << 3).eq(0))
        .And(qa.bitwiseAnd(1 << 4).eq(0))
    )
    sr = img.updateMask(mask).select(["SR_B5", "SR_B6", "SR_B7"]).multiply(0.0000275).add(-0.2)
    nbr = sr.normalizedDifference(["SR_B5", "SR_B7"]).rename("NBR")
    mirbi = sr.select("SR_B7").multiply(10).subtract(sr.select("SR_B6").multiply(9.8)).add(2).rename("MIRBI")
    return nbr.addBands(mirbi)


def _l57_indices(img):
    """Landsat 5/7 TM/ETM+ (C2 L2) NBR + MIRBI, with C2 scale factor applied.

    NIR=SR_B4, SWIR1=SR_B5, SWIR2=SR_B7. QA_PIXEL bits masked: 3=cloud, 4=cloud shadow.
    """
    import ee
    qa = img.select("QA_PIXEL")
    mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
    sr = img.updateMask(mask).select(["SR_B4", "SR_B5", "SR_B7"]).multiply(0.0000275).add(-0.2)
    nbr = sr.normalizedDifference(["SR_B4", "SR_B7"]).rename("NBR")
    mirbi = sr.select("SR_B7").multiply(10).subtract(sr.select("SR_B5").multiply(9.8)).add(2).rename("MIRBI")
    return nbr.addBands(mirbi)


_LANDSAT_SENSORS = [
    ("LANDSAT/LC09/C02/T1_L2", _l89_indices),
    ("LANDSAT/LC08/C02/T1_L2", _l89_indices),
    ("LANDSAT/LE07/C02/T1_L2", _l57_indices),
    ("LANDSAT/LT05/C02/T1_L2", _l57_indices),
]


def _composites(satellite, roi_geom, pre_start, pre_end, post_start, post_end):
    """Return (pre_img, post_img, pre_count, post_count).

    pre_img / post_img are two-band (NBR, MIRBI) mean composites. Counts are taken
    on the raw (unmasked) collections so they reflect scenes available, not pixels
    surviving the mask.
    """
    import ee

    if satellite == "Sentinel-2":
        def raw(s, e):
            return ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(roi_geom).filterDate(s, e)
        pre_raw, post_raw = raw(pre_start, pre_end), raw(post_start, post_end)
        pre_img  = pre_raw.map(_s2_pre_indices).mean()
        post_img = post_raw.map(_s2_post_indices).mean()
        pre_count  = int(pre_raw.size().getInfo())
        post_count = int(post_raw.size().getInfo())
        return pre_img, post_img, pre_count, post_count

    # Landsat: merge all four sensors so the archive extends back to 1984.
    def merged(s, e):
        colls = [
            ee.ImageCollection(cid).filterBounds(roi_geom).filterDate(s, e).map(fn)
            for cid, fn in _LANDSAT_SENSORS
        ]
        m = colls[0]
        for c in colls[1:]:
            m = m.merge(c)
        return m

    def raw_count(s, e):
        total = 0
        for cid, _ in _LANDSAT_SENSORS:
            total += int(ee.ImageCollection(cid).filterBounds(roi_geom).filterDate(s, e).size().getInfo())
        return total

    pre_img  = merged(pre_start, pre_end).mean()
    post_img = merged(post_start, post_end).mean()
    return pre_img, post_img, raw_count(pre_start, pre_end), raw_count(post_start, post_end)


# ── Input helper tasks ────────────────────────────────────────────────────────

@register()
def set_aoi_group_name(
    group_name: Annotated[
        str,
        Field(
            title="Area of Interest",
            description=(
                "EarthRanger spatial features group that defines the area to map for burn "
                "severity (e.g. 'Reserve Boundary', 'North Block'). Find it in ER under "
                "Admin → Map Layers → Feature Groups. Severity is computed within this boundary."
            ),
        ),
    ],
) -> str:
    """Return the AOI group name; exists to expose a well-labelled form field."""
    return group_name


@register()
def set_overlay_group_name(
    group_name: Annotated[
        str,
        Field(
            title="Overlay Layer (optional)",
            description=(
                "Optional EarthRanger spatial features group to draw on top of the map "
                "(e.g. 'Roads', 'Fencelines', 'Water Sources'). Leave blank for none."
            ),
            default="",
        ),
    ] = "",
) -> str:
    """Return the overlay group name as-is; exposes a labelled form field."""
    return group_name


# ── Main GEE computation ──────────────────────────────────────────────────────

@register(tags=["gee", "fire"])
def compute_dnbr_severity(
    client: _GEE,
    aoi: _GDF,
    fire_start_date: Annotated[
        str,
        Field(
            title="Fire Start Date",
            description="Start of the fire (or scan window), YYYY-MM-DD. Pre-fire imagery is taken before this date.",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            json_schema_extra={"format": "date"},
        ),
    ],
    fire_end_date: Annotated[
        str,
        Field(
            title="Fire End Date",
            description="End of the fire (or scan window), YYYY-MM-DD. Use the same value as the start date for a single-day fire.",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            json_schema_extra={"format": "date"},
        ),
    ],
    satellite: Annotated[
        Literal["Sentinel-2", "Landsat"],
        Field(
            title="Satellite",
            description=(
                "Sentinel-2 — 10-20 m, from 2015, best for recent fires (recommended). "
                "Landsat — 30 m, archive back to 1984, use for fires before 2015."
            ),
        ),
    ] = "Sentinel-2",
    pre_fire_days: Annotated[
        int,
        Field(
            title="Pre-Fire Window (days)",
            description="Days before the fire start used to build the pre-fire composite. 90 days keeps the baseline in stable pre-season vegetation.",
            ge=30,
            le=365,
        ),
    ] = 90,
    post_fire_days: Annotated[
        int,
        Field(
            title="Post-Fire Window (days)",
            description="Days after the fire end used to build the post-fire composite. 30 days captures charred ground before regrowth dilutes the signal; use up to 60 for slow-recovering vegetation.",
            ge=7,
            le=180,
        ),
    ] = 30,
    scale: Annotated[
        int,
        Field(
            title="Analysis Scale (metres)",
            description=(
                "Pixel resolution for the analysis. 100 m (default) works for reserves up to "
                "~500,000 ha. Use 20-50 m for small areas (< 5,000 ha) where detail matters. "
                "Finer scales over large areas exceed Earth Engine memory limits."
            ),
            ge=20,
            le=500,
        ),
    ] = 100,
    dnbr_threshold: Annotated[
        float,
        Field(
            title="dNBR Detection Threshold",
            description=(
                "Minimum dNBR for a pixel to count as burned at all (independent of the "
                "Key & Benson severity colour it's given once detected). Default 0.20. "
                "0.10 (the floor) detects the most area but is highly prone to false positives "
                "from ordinary seasonal vegetation drying — especially over a scan window of "
                "weeks or months rather than a specific known fire date. Raise to 0.27-0.30 if "
                "a run shows a large, diffuse, low-severity 'burn' across most of the AOI: real "
                "fire scars are spatially coherent and rarely uniform across an entire reserve."
            ),
            ge=0.10,
            le=0.5,
        ),
    ] = 0.20,
) -> _GDF:
    """
    Compute a per-class dNBR burn-severity map across the AOI for the fire window.

        NBR    = (NIR − SWIR2) / (NIR + SWIR2)
        dNBR   = (NBR_pre − NBR_post) × 1000            positive ⇒ burn
        MIRBI  = 10·SWIR2 − 9.8·SWIR1 + 2               (Trigg & Flasse 2001)
        ΔMIRBI = MIRBI_post − MIRBI_pre                 positive ⇒ burn (opposite sign to dNBR)

    Detection and classification are deliberately decoupled: `dnbr_threshold` gates
    whether a pixel counts as burned at all (a locally-tunable operational decision —
    Key & Benson themselves note the fixed classes need biome calibration), while the
    colour/class it receives once detected always follows the unmodified Key & Benson
    bins. This keeps severity comparisons standard even when the detection floor is
    raised for a noisy AOI.

    NOTE: ΔMIRBI confirmation reduces false positives from clouds/shadow/water, but does
    NOT reliably separate real fire from seasonal grass senescence — both processes
    reduce vegetation moisture and shift the SWIR bands the same way. A high 'confirmed'
    rate is not on its own proof of a real burn; treat it as a secondary signal only.

    A minimum patch size of 0.5 ha (connected-pixel filter, Roteta et al. 2019) removes
    single-pixel speckle before vectorisation.

    Returns a GeoDataFrame with one row per burned polygon:
        severity_class, severity_index (0..5), dNBR (mean), DELTA_MIRBI (mean),
        confidence, fill_color, fill_color_hex, area_ha, satellite, dnbr_threshold,
        pre_image_count, post_image_count, aoi_area_ha, fire_start_date, fire_end_date.
    """
    import ee
    from shapely.geometry import mapping
    from shapely.geometry.polygon import orient
    from ecoscope.platform.connections import EarthEngineConnection

    if isinstance(client, str):
        EarthEngineConnection.client_from_named_connection(client)

    # Parse the fire window. Pre composite ends the day the fire starts; post
    # composite starts the day the fire ends. filterDate is [start, end).
    fire_start_dt = datetime.strptime(fire_start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    fire_end_dt   = datetime.strptime(fire_end_date,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if fire_end_dt < fire_start_dt:
        raise ValueError("Fire End Date must be on or after Fire Start Date.")
    pre_start  = (fire_start_dt - timedelta(days=pre_fire_days)).strftime("%Y-%m-%d")
    pre_end    = fire_start_date
    post_start = fire_end_date
    post_end   = (fire_end_dt + timedelta(days=post_fire_days)).strftime("%Y-%m-%d")

    # Pre-flight pixel budget check (reduceToVectors / reduceRegions OOM above ~500k px).
    aoi = aoi.set_geometry(aoi.geometry.make_valid())
    aoi_utm = aoi.to_crs(aoi.estimate_utm_crs())
    area_m2 = float(aoi_utm.geometry.union_all().area)
    est_pixels = area_m2 / (scale ** 2)
    if est_pixels > 500_000:
        suggest = int((area_m2 / 250_000) ** 0.5) + 5
        raise ValueError(
            f"AOI ({area_m2 / 10_000:.0f} ha) at {scale} m scale would need "
            f"~{est_pixels:,.0f} pixels — Earth Engine's limit is ~500,000. "
            f"Increase 'Analysis Scale' to at least {suggest} m."
        )

    # AOI → single CCW polygon geometry for GEE.
    aoi_4326 = aoi.to_crs("EPSG:4326")
    union_geom = aoi_4326.geometry.unary_union
    if hasattr(union_geom, "geoms"):
        union_geom = union_geom.convex_hull
    union_geom = orient(union_geom, sign=1.0)      # GEE requires CCW exterior ring
    roi_geom = ee.Geometry(mapping(union_geom))

    pre_img, post_img, pre_count, post_count = _composites(
        satellite, roi_geom, pre_start, pre_end, post_start, post_end
    )
    if pre_count == 0:
        raise ValueError(
            f"No {satellite} imagery for the pre-fire window ({pre_start} – {pre_end}). "
            "Try increasing 'Pre-Fire Window (days)' or check the AOI overlaps a scene footprint."
        )
    if post_count == 0:
        raise ValueError(
            f"No {satellite} imagery for the post-fire window ({post_start} – {post_end}). "
            "Try increasing 'Post-Fire Window (days)' or use a more recent fire date."
        )

    dnbr        = pre_img.select("NBR").subtract(post_img.select("NBR")).multiply(1000).rename("dNBR")
    delta_mirbi = post_img.select("MIRBI").subtract(pre_img.select("MIRBI")).rename("DELTA_MIRBI")

    # Severity index image, 0..5, built by summing threshold crossings:
    #   <-100→0, -100..100→1, 100..270→2, 270..440→3, 440..660→4, ≥660→5
    # This is always the unmodified Key & Benson classification, regardless of
    # dnbr_threshold — only which pixels get THROUGH to the map depends on the threshold.
    sev = (
        dnbr.gte(-100)
        .add(dnbr.gte(100))
        .add(dnbr.gte(270))
        .add(dnbr.gte(440))
        .add(dnbr.gte(660))
        .toInt()
        .rename("severity_index")
    )

    # Detection gate: dNBR ≥ threshold (independent of the Key & Benson colour above).
    # Floored at 0.10 (Field ge=0.10) so a pixel that passes the gate can never fall in
    # the Unburned band (-100..100) — avoids a "burned but labelled Unburned" contradiction.
    threshold_scaled = dnbr_threshold * 1000
    burned_bool = dnbr.gte(threshold_scaled).selfMask()

    # Minimum patch size: 0.5 ha connected-component filter (Roteta et al. 2019) —
    # removes single/speckled pixels so noise doesn't get individually vectorised.
    pixel_area_m2 = scale ** 2
    min_pixels = max(1, int(np.ceil(5000.0 / pixel_area_m2)))  # 0.5 ha = 5000 m²
    connected = burned_bool.connectedPixelCount(maxSize=1000, eightConnected=True)
    patch_mask = connected.gte(min_pixels)

    burned_label = sev.updateMask(patch_mask)
    polys = burned_label.reduceToVectors(
        geometry=roi_geom,
        scale=scale,
        geometryType="polygon",
        labelProperty="severity_index",
        eightConnected=True,
        maxPixels=int(1e8),
        bestEffort=True,
    )

    # Mean dNBR + ΔMIRBI per polygon (tileScale keeps large AOIs within memory).
    data_img = dnbr.addBands(delta_mirbi)
    stats = data_img.reduceRegions(
        collection=polys,
        reducer=ee.Reducer.mean(),
        scale=scale,
        tileScale=4,
    )
    features = stats.getInfo()["features"]

    _cols = [
        "geometry", "severity_class", "severity_index", "dNBR", "DELTA_MIRBI",
        "confidence", "fill_color", "fill_color_hex", "area_ha", "satellite",
        "dnbr_threshold", "pre_image_count", "post_image_count", "aoi_area_ha",
        "fire_start_date", "fire_end_date",
    ]
    if not features:
        return gpd.GeoDataFrame(columns=_cols, crs="EPSG:4326")

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")

    # Clip to AOI — at coarse scales GEE keeps any pixel whose centroid is inside
    # the AOI, so polygons can straddle the boundary.
    aoi_union = aoi_4326.geometry.union_all()
    gdf["geometry"] = gdf.geometry.intersection(aoi_union)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
    if gdf.empty:
        return gpd.GeoDataFrame(columns=_cols, crs="EPSG:4326")

    # Colour / label each polygon from its severity index.
    gdf["severity_index"] = gdf["severity_index"].astype(int).clip(0, len(SEVERITY_CLASSES) - 1)
    gdf["severity_class"] = gdf["severity_index"].map(lambda i: SEVERITY_CLASSES[i][0])
    gdf["fill_color"]     = gdf["severity_index"].map(lambda i: SEVERITY_CLASSES[i][3])
    gdf["fill_color_hex"] = gdf["severity_index"].map(lambda i: SEVERITY_CLASSES[i][4])

    # Confidence: 'confirmed' when ΔMIRBI independently indicates burn.
    if "DELTA_MIRBI" not in gdf.columns:
        gdf["DELTA_MIRBI"] = 0.0
    gdf["DELTA_MIRBI"] = gdf["DELTA_MIRBI"].fillna(0.0)
    gdf["confidence"] = gdf["DELTA_MIRBI"].apply(lambda v: "confirmed" if v > 0 else "probable")

    gdf["area_ha"] = gdf.to_crs(gdf.estimate_utm_crs()).geometry.area / 10_000.0

    # Run metadata (stored per row so stat tasks can read it back).
    gdf["satellite"]        = satellite
    gdf["dnbr_threshold"]   = dnbr_threshold
    gdf["pre_image_count"]  = pre_count
    gdf["post_image_count"] = post_count
    gdf["aoi_area_ha"]      = area_m2 / 10_000.0
    gdf["fire_start_date"]  = fire_start_date
    gdf["fire_end_date"]    = fire_end_date

    return gdf


# ── Layer / visualisation tasks ───────────────────────────────────────────────

@register(tags=["fire"])
def create_dnbr_severity_layer(
    geodataframe: _GDF,
    opacity: Annotated[
        float,
        Field(
            title="Severity Layer Opacity",
            description="Transparency of the severity layer (0 = transparent, 1 = fully opaque).",
            ge=0.0,
            le=1.0,
        ),
    ] = 0.85,
) -> Any:
    """Polygon layer coloured by burn-severity class, with a discrete legend."""
    from ecoscope.platform.tasks.results._ecomap import (
        LayerDefinition,
        LegendDefinition,
        PolygonLayerStyle,
    )

    style = PolygonLayerStyle(
        filled=True,
        stroked=True,
        fill_color_column="fill_color",
        get_line_color="#333333",
        get_line_width=1,
        line_width_units="pixels",
        opacity=opacity,
    )
    legend = LegendDefinition(labels=_BURNED_NAMES, colors=_BURNED_HEX)
    return LayerDefinition(
        geodataframe=geodataframe,
        layer_style=style,
        legend=legend,
        tooltip_columns=["severity_class", "dNBR", "confidence", "area_ha"],
        zoom=False,   # AOI boundary layer is the zoom target (see combine task)
    )


@register(tags=["fire", "overlay"])
def create_styled_overlay_layer(
    geodataframe: _GDF,
    zoom: Annotated[
        bool,
        Field(default=False, description="If True, the map zooms to this layer's extent on load."),
    ] = False,
) -> Any:
    """
    Overlay layer for ER spatial features (AOI boundary, roads, fencelines, etc.).

    Splits by geometry type so lonboard never receives mixed types.
    """
    from ecoscope.platform.tasks.results._ecomap import (
        LayerDefinition,
        PointLayerStyle,
        PolygonLayerStyle,
        PolylineLayerStyle,
    )

    gdf = geodataframe.copy()
    geom_col = gdf.geometry.geom_type
    color, width = "#FF8C00", 2.0
    layers = []

    line_gdf = gdf[geom_col.isin({"LineString", "MultiLineString"})].copy()
    if not line_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=line_gdf,
            layer_style=PolylineLayerStyle(get_color=color, get_width=width, width_units="pixels", cap_rounded=True),
            legend=None,
            tooltip_columns=[],
            zoom=zoom,
        ))

    polygon_gdf = gdf[geom_col.isin({"Polygon", "MultiPolygon"})].copy()
    if not polygon_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=polygon_gdf,
            layer_style=PolygonLayerStyle(
                filled=False, stroked=True,
                get_line_color=color, get_line_width=width, line_width_units="pixels",
            ),
            legend=None,
            tooltip_columns=[],
            zoom=zoom,
        ))

    point_gdf = gdf[geom_col.isin({"Point", "MultiPoint"})].copy()
    if not point_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=point_gdf,
            layer_style=PointLayerStyle(get_fill_color=color, get_radius=5, radius_units="pixels"),
            legend=None,
            tooltip_columns=[],
            zoom=zoom,
        ))

    return layers


@register(tags=["fire"])
def combine_severity_layers(
    severity_layer: Any,
    aoi_layer: Any,
    overlay_layer: Any = None,
) -> Any:
    """Combine severity layer + AOI boundary outline + optional user overlay for draw_ecomap.

    aoi_layer is always shown and is the zoom target (compact, always present).
    overlay_layer is optional — pass SkipSentinel or None to omit. Handles SkipSentinel
    internally so the map renders even when the overlay is blank.
    """
    from wt_task.skip import SkipSentinel

    if isinstance(severity_layer, SkipSentinel):
        return severity_layer
    layers = [severity_layer]
    for extra in (aoi_layer, overlay_layer):
        if isinstance(extra, SkipSentinel) or extra is None:
            continue
        if isinstance(extra, list):
            layers.extend(extra)
        else:
            layers.append(extra)
    return layers


# ── Stat tasks ────────────────────────────────────────────────────────────────

@register(tags=["fire", "stats"])
def count_burned_area_ha(geodataframe: _GDF) -> float:
    """Total burned area (Low severity or higher, severity_index ≥ 2) in hectares."""
    if geodataframe.empty:
        return 0.0
    burned = geodataframe[geodataframe["severity_index"] >= 2]
    if burned.empty:
        return 0.0
    return float(burned.to_crs(burned.estimate_utm_crs()).geometry.area.sum()) / 10_000.0


@register(tags=["fire", "stats"])
def count_high_severity_area_ha(geodataframe: _GDF) -> float:
    """Total area classified Moderate-High or High (severity_index ≥ 4) in hectares."""
    if geodataframe.empty:
        return 0.0
    high = geodataframe[geodataframe["severity_index"] >= 4]
    if high.empty:
        return 0.0
    return float(high.to_crs(high.estimate_utm_crs()).geometry.area.sum()) / 10_000.0


@register(tags=["fire", "stats"])
def get_aoi_area_ha(geodataframe: _GDF) -> float:
    """AOI area (ha) stored on the result GeoDataFrame."""
    if geodataframe.empty or "aoi_area_ha" not in geodataframe.columns:
        return 0.0
    return float(geodataframe["aoi_area_ha"].iloc[0])


@register(tags=["fire", "stats"])
def get_percent_burned(
    burned_ha: Annotated[float, Field(description="Burned area in hectares.")],
    aoi_area_ha: Annotated[float, Field(description="Total AOI area in hectares.")],
) -> float:
    """Percentage of the AOI that burned."""
    if aoi_area_ha <= 0:
        return 0.0
    return (burned_ha / aoi_area_ha) * 100.0


@register(tags=["fire", "stats"])
def format_percent_burned(
    percent: Annotated[float, Field(description="Percentage of AOI burned.")],
) -> str:
    """Format percent-burned for the dashboard (e.g. '12.4 %')."""
    return f"{percent:.1f} %"


@register(tags=["fire", "stats"])
def count_mean_dnbr(geodataframe: _GDF) -> float:
    """Area-weighted mean dNBR across burned polygons (a single burn-intensity summary)."""
    if geodataframe.empty or "dNBR" not in geodataframe.columns:
        return 0.0
    w = geodataframe["area_ha"].astype(float)
    if w.sum() <= 0:
        return float(geodataframe["dNBR"].mean())
    return float((geodataframe["dNBR"].astype(float) * w).sum() / w.sum())


@register(tags=["fire", "stats"])
def format_mean_dnbr(
    dnbr_value: Annotated[float, Field(description="Mean dNBR value to format.")],
) -> str:
    """Format a mean dNBR value as a rounded integer string."""
    return str(int(round(dnbr_value)))


@register(tags=["fire", "stats"])
def count_pre_images(geodataframe: _GDF) -> int:
    """Number of scenes in the pre-fire mean composite (higher ⇒ more reliable)."""
    if geodataframe.empty or "pre_image_count" not in geodataframe.columns:
        return 0
    return int(geodataframe["pre_image_count"].iloc[0])


@register(tags=["fire", "stats"])
def count_post_images(geodataframe: _GDF) -> int:
    """Number of scenes in the post-fire mean composite (low counts ⇒ noisier signal)."""
    if geodataframe.empty or "post_image_count" not in geodataframe.columns:
        return 0
    return int(geodataframe["post_image_count"].iloc[0])


@register(tags=["fire", "stats"])
def format_image_count(
    count: Annotated[int, Field(description="Number of satellite scenes.")],
) -> str:
    """Format a scene count as 'N scene(s)'."""
    return f"{count} scene{'s' if count != 1 else ''}"


@register(tags=["fire", "stats"])
def get_dnbr_threshold(geodataframe: _GDF) -> float:
    """Extract the dNBR detection threshold used in this run."""
    if geodataframe.empty or "dnbr_threshold" not in geodataframe.columns:
        return 0.0
    return float(geodataframe["dnbr_threshold"].iloc[0])


@register(tags=["fire", "stats"])
def format_dnbr_threshold(
    threshold: Annotated[float, Field(description="dNBR threshold value.")],
) -> str:
    """Format the dNBR threshold for display (e.g. '0.20')."""
    return f"{threshold:.2f}"


@register(tags=["fire", "stats"])
def format_area_ha(
    area_ha: Annotated[float, Field(description="Area in hectares to format for display.")],
) -> str:
    """Format an area value as a human-readable string (m², ha, or km²)."""
    if area_ha <= 0:
        return "0 ha"
    elif area_ha >= 10_000:
        return f"{area_ha / 10_000:.1f} km²"
    elif area_ha >= 1:
        return f"{int(round(area_ha)):,} ha"
    else:
        return f"{int(round(area_ha * 10_000)):,} m²"
