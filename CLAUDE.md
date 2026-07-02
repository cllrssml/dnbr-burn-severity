# dnbr-burn-severity — workflow notes

Per-class dNBR burn-severity map across an ER AOI for a chosen fire window.
Merges the two earlier fire workflows into one: AOI + date-window input (from
`cfw_burn_scar_mapping`) + per-class severity map across the ROI (from
`fire-severity-dnbr`) + a Sentinel-2 / Landsat sensor switch. Built for Britt
Klaassen to test (community NBR workflow request, June 2026).

**The two source workflows are intentionally left intact** — this is a new,
third workflow, not a replacement.

Custom package: `dnbr-severity-tasks` (module `dnbr_severity_tasks`), bundled
into the compiled dir after each compile.

---

## Science (all coefficients verified against primary sources 2026-07-02)

- **Compositing:** Parks et al. 2018 — mean composite over each window.
- **dNBR classes:** Key & Benson 2006 — 0.1 / 0.27 / 0.44 / 0.66 (×1000: 100/270/440/660).
- **MIRBI:** Trigg & Flasse 2001 — `10·SWIR2 − 9.8·SWIR1 + 2`. ΔMIRBI = post − pre (positive = burn, OPPOSITE sign to dNBR). Drives the confirmed/probable tier.
- **Landsat C2 L2 scaling (critical):** reflectance = `DN·0.0000275 − 0.2`. The **−0.2 offset must be applied before the normalized difference** — it does NOT cancel (only the multiplicative factor does). Skipping it biases NBR toward zero and under-reports severity by ~2 classes. This was the bug in the original `dnbr-tasks`; the merged package applies scaling in `_l89_indices` / `_l57_indices`.
- **Sentinel-2:** `COPERNICUS/S2_SR_HARMONIZED` already removes the PB 04.00 −1000 offset, so its scale is purely multiplicative and cancels in the ratio — no scaling needed. Do not divide S2 for NBR; DO divide by 10000 for MIRBI (coefficients need real reflectance).

### Severity index trick
`sev = dnbr.gte(-100)+gte(100)+gte(270)+gte(440)+gte(660)` → integer 0..5 matching
`SEVERITY_CLASSES` exactly (verified at every boundary). The severity_index/colour
assigned to a pixel is ALWAYS the unmodified Key & Benson bins — it never changes with
`dnbr_threshold`. Only classes ≥ 2 (Low+) that also pass the threshold + patch-size gate
are vectorised for the map via `reduceToVectors(labelProperty="severity_index")` — scales
to reserve size, unlike per-pixel `.sample()`.

### Detection threshold vs. classification (added v1.0.0, 2026-07-02)
`dnbr_threshold` (default 0.20, floor 0.10, matches `burn-scar-tasks`' own field) gates
whether a pixel counts as burned at all — decoupled from the Key & Benson colour it's
given once detected. Floor is 0.10, not 0.05 like `burn-scar-tasks`, specifically to avoid
a "burned pixel labelled Unburned" contradiction (at dNBR exactly 100 the severity_index is
already 2/Low — verified, no gap). Restored the 0.5 ha connected-pixel patch filter that
`burn-scar-tasks` had (dropped by mistake during the original merge) — **note this filter
is a no-op at the default 100 m scale** (`min_pixels=1` since one pixel is already 1 ha);
it only helps below ~70 m. The threshold field is the actual lever for false-positive
control at default scale, not the patch filter.

---

## Task chain

`set_workflow_details` → `set_er_connection` → `set_gee_connection` →
`set_aoi_group_name` → `get_spatial_features_group` id=`aoi_features` →
`set_overlay_group_name` → `get_spatial_features_group` id=`overlay_features` (`skipif: any_dependency_is_empty_string`) →
`set_base_maps` →
`set_fire_start_date` / `set_fire_end_date` (custom; own steps, not inline params, so the
raw date strings can be reused below) → `parse_fire_start_datetime` / `parse_fire_end_datetime`
(custom; str→tz-aware datetime, end date advanced +1 day for inclusive end-of-day) →
`set_time_range` (built-in) id=`fire_time_range` →
`set_controlled_burn_event_type` / `set_firms_event_type` (custom; blank-default text
fields, example slugs `controlled_burn` / `firms_rep` in the description) →
`get_events` (built-in) id=`controlled_burn_events` / `firms_events` — `event_types` bound
as a one-item list `[${{ workflow.<event_type>.return }}]`, `time_range` = fire window
(not the wider pre/post composite window), `raise_on_empty: false` (must not error the
whole run when a reserve has zero controlled burns/FIRMS hits that period) — both
`skipif: any_dependency_is_empty_string` →
`create_styled_overlay_layer` id=`controlled_burn_layer` (color=`#1E90FF`) /
`firms_layer` (color=`#FF00FF`) — `skipif: any_dependency_skipped, any_is_empty_df` →
`compute_dnbr_severity` (custom; partial client+aoi+fire_start_date+fire_end_date
[now bound to the shared steps above, not inline]; user params: satellite=Sentinel-2,
pre_fire_days=90, post_fire_days=30, scale=100, dnbr_threshold=0.20) →
`create_dnbr_severity_layer` (zoom=False) →
`create_styled_overlay_layer` id=`aoi_layer` (zoom=true — AOI boundary is the zoom target) →
`create_styled_overlay_layer` id=`overlay_layer` (zoom=false; `skipif: any_dependency_skipped, any_is_empty_df`) →
`combine_severity_layers` (`skipif: any_is_empty_df` ONLY — handles SkipSentinel; now
also accepts optional `controlled_burn_layer`/`firms_layer`) →
`draw_ecomap` → `persist_text` → `create_map_widget_single_view` (`skipif: never`) →
`persist_df` id=`severity_file` (geojson → Files tab) →
stat chain: burned_area → high_severity → (aoi_area → percent_burned) → threshold → mean_dnbr → pre_scenes → post_scenes →
`gather_dashboard` (`time_range: ~`).

### Two dashboard charts — v4.0.0, added 2026-07-02

Added the two charts recommended when scoping this out: "Burned Area by Severity
Class" (bar chart, `severity_class` × sum `area_ha`) and "dNBR Distribution"
(histogram-as-bar-chart, 50-unit dNBR bins × polygon count). Both are widgets 8/9 in
`gather_dashboard`, new row in `layout.json` (`y=22`, `w=5` each, `h=8`).

- **Pattern:** `prepare_*_chart_data` (custom, plain non-geometry table) →
  `draw_bar_chart` (built-in) → `persist_text` → `create_plot_widget_single_view` —
  identical shape to the existing map chain (`draw_ecomap` → `persist_text` →
  `create_map_widget_single_view`). `create_plot_widget_single_view`'s `data` param is
  `PrecomputedHTMLWidgetData = Path | Url | None`, i.e. a **path**, not the raw chart
  HTML string `draw_bar_chart` returns directly — the `persist_text` step in between
  isn't optional.
- **`draw_bar_chart`'s own `data.groupby(category)` sorts alphabetically**, not by
  input row order. For the severity chart this would render "High, Low,
  Moderate-High, Moderate-Low" instead of the Key & Benson Low→High progression.
  Fixed by prefixing `severity_class` with its ordinal ("1. Low" … "4. High") before
  it reaches `draw_bar_chart` — lexicographic sort on a single leading digit gives the
  right order for free. For the dNBR histogram, used the bin's integer lower edge as
  the category instead (ints sort numerically, no hack needed) — also reads better on
  a histogram x-axis than a text label would.
- **Reused the `_GDF` shim for plain (non-geometry) DataFrames**, not a new shim for
  `draw_bar_chart`'s `DataFrame[JsonSerializableDataFrameModel]` param — same
  reasoning as `persist_df`'s `AnyDataFrame` param already accepting a `_GDF`-typed
  return (`severity_result` → `severity_file`, working since v1.0.0): the shim is a
  compiler-side "this is tabular" tag, not a strict runtime type check, and this
  compiled clean on the first try.
- **`draw_bar_chart` leaves an optional `layout_kwargs` field unbound** (Advanced,
  `LayoutStyle`, defaults to null) — same class of thing as `severity_layer`'s already-
  accepted `opacity` field, not the kind of required/confusing field removed in
  v3.0.0, so left as-is rather than binding it away.
- **Diagnostic intent (dNBR Distribution):** this is the chart that would have caught
  the Trap 30 false-positive before it reached a live Desktop run — a real fire's
  polygons form a right-skewed/bimodal shape well clear of the detection floor; a
  landscape-wide seasonal-drying false positive shows a tight mass hugging just above
  the threshold with no separation.

### Fix: burn polygon's low-alpha fill blocked the severity tooltip — v3.3.0, added 2026-07-02

Live-tested regression from v3.2.0: fixing the Controlled Burn tooltip (by making its
polygon `filled=True`, see below) meant that polygon's fill now sat in the picking
buffer on top of `severity_layer` wherever they overlapped — hovering inside the burn
scar returned the Controlled Burn tooltip and the dNBR severity tooltip became
unreachable in that whole region. User's own instinct was "bring dNBR on top," but a
flat reorder trades one bug for another: severity's fill is ~85% opaque, so on top it
would visually erase the blue burn outline wherever they overlap — losing the "where's
the recorded burn relative to the detected severity" reference the overlay exists for.

**Fix: split each interactive polygon into two layers instead of one.**
`create_styled_overlay_layer` now returns `{"below": [...], "above": [...]}` instead of
a flat list — "below" holds the pickable low-alpha fill (tooltip source), "above" holds
a `pickable=False` visible-outline-only decorative copy of the same polygon.
`combine_severity_layers` places `severity_layer` between the two buckets from every
overlay (AOI, free-text overlay, controlled burn, FIRMS), so: the outline is always
visible on top (nothing changed there), severity's tooltip always wins in the overlap
(the "above" outline can't intercept hover — it's explicitly non-pickable), and the burn
polygon's own tooltip still fires wherever it's exposed outside the severity footprint.
Non-interactive layers (AOI boundary, free-text overlay) are unaffected — they only ever
populate "above", identical to their pre-v3.2.0 single-layer behaviour.

### Fix: polygon tooltips never fired — v3.2.0, added 2026-07-02

Real bug found via live testing: FIRMS point tooltips worked, Controlled Burn polygon
tooltips did not. Root cause is a deck.gl picking quirk, not a workflow logic bug —
`PolygonLayerStyle(filled=False, ...)` (used for outline-only overlays like the AOI
boundary) means deck.gl never draws/rasterizes the polygon interior, and picking
piggybacks on that same draw pass. So an unfilled polygon can only register a hover hit
on the thin stroke line, which in practice is nearly impossible to land a cursor on —
looks exactly like "no tooltip." Points don't have this problem; a point marker is
always "filled." **Fix:** `create_styled_overlay_layer` now switches to
`filled=True` with a low-alpha fill (`get_fill_color=[r, g, b, 40]`, alpha 40/255 ≈ 16%)
whenever the layer actually has `tooltip_columns` or a `legend_label` set — i.e. only
for layers meant to be interactive. Plain outline-only layers (AOI boundary, the
free-text overlay) are untouched (`interactive` is `False` for both, since neither
passes tooltip/legend params), so their appearance doesn't change.

### Tooltips, legend, area, and consistent hectares — v3.1.0, added 2026-07-02

- **Tooltips on the ER event overlays.** `create_styled_overlay_layer` gained
  `tooltip_columns` and `legend_label` params (both explicitly bound to `[]`/`~` for
  `aoi_layer`/`overlay_layer` so they stay hidden — same discipline as every other
  param added to this task). Controlled Burn shows title/time/area_ha on hover; FIRMS
  shows title/time (points have no area).
- **Area for controlled-burn polygons.** New task `add_polygon_area_ha` (UTM
  reprojection, same technique `compute_dnbr_severity` already uses for the severity
  polygons) adds `area_ha` for polygon/multipolygon rows only — NaN for points/lines,
  since area isn't a meaningful concept for a FIRMS point detection. Wired as its own
  step between `get_events` and `create_styled_overlay_layer` for the controlled-burn
  branch only.
- **One shared legend, not per-layer boxes.** Confirmed from `draw_ecomap` source:
  every `LayerDefinition.legend` in `geo_layers` gets flattened into a single
  `m.add_legend()` call — so a legend entry added to `controlled_burn_layer`/
  `firms_layer` merges into the same panel as the severity classes, not a second box.
  `legend_label` is attached to only the first non-empty geometry-type sub-layer a
  gdf produces, so a mixed-geometry input still yields one swatch, not one per type.
  Renamed the shared `legend_style.title` from "Burn Severity" to "Legend" since the
  panel can now contain non-severity entries too.
- **AOI boundary vs. free-text overlay were both `#FF8C00`** — indistinguishable on
  the map when both were in use. AOI Boundary is now `#000000` (black, standard
  cartographic convention for a boundary outline); Overlay Layer stays `#FF8C00`
  (unchanged, matches pre-existing behaviour for anyone already using that field).
- **All areas now consistently hectares.** `format_area_ha` previously switched units
  by magnitude (m² below 1 ha, km² above 10,000 ha) — removed; it's `ha` always
  (1 decimal below 100 ha, whole numbers at/above). Per-polygon `area_ha` on the
  severity result is now rounded to 1 decimal at the source in `compute_dnbr_severity`
  rather than left as a raw float, so both the map tooltip and the stat widgets that
  sum it show clean, consistent numbers.

### Form simplification — v3.0.0, added 2026-07-02

v2.0.0 accidentally exposed a wall of technical fields alongside the two event-type
slugs: `get_events`'s `event_columns`/`include_*`/`force_point_geometry` flags (one set
per overlay), a raw hex `color` field on `aoi_layer`/`overlay_layer` (a side-effect of
parameterising `create_styled_overlay_layer` for the new overlay colours), a
`timezone`/`time_format` pair on `fire_time_range` that duplicated the Fire Start/End
Date fields already answered elsewhere, and a "Severity GeoJSON" section whose only
field was an optional hash-based `filename`. Fixed by binding every one of those via
`partial:` instead of leaving them unbound — once a step has zero unbound params it
disappears from the form entirely (same mechanism `controlled_burn_layer`/`firms_layer`
already relied on). User-facing form is now: connections, AOI, optional overlay group,
fire dates, the two event-type slugs, satellite/threshold params, severity opacity —
nothing else.

**Real bug caught in the process, not just UX noise:** `get_events` defaults
`force_point_geometry=True` ("If True, polygon/multipolygon event geometries are reduced
to their centroid"). Left unbound in v2.0.0, this meant Controlled Burn polygons would
have been silently collapsed to points on the map — the opposite of what was asked for
("bring in the polygons"). Now explicitly `false` for both overlays.

GeoJSON filename is no longer a form field either — a new tiny task
(`format_output_filename`) builds `dnbr_burn_severity_<fire_start_date>_to_<fire_end_date>`
from the two date steps already in the DAG, so exports in the Files tab stay
distinguishable across runs without asking the user to name anything.

### ER event overlays (controlled burns, FIRMS) — v2.0.0, added 2026-07-02

Two optional blank-by-default text fields ("Controlled Burn Event Type", "FIRMS
Detections Event Type") let the user paste their ER event-type slug; when filled, matching
events within the same fire window as the dNBR computation are drawn on the map as a
**visual reference only** — they never feed the dNBR computation, threshold, or any stat
widget. `create_styled_overlay_layer` gained a `color` param (default `#FF8C00`, unchanged
for `aoi_layer`/`overlay_layer`) so these two new layers get distinct colours from the
general-purpose overlay and from the severity palette.

**Trap: `ecoscope_workflows_core` is NOT importable at module level in a custom task
package**, even though it's the lightweight core framework (no geopandas/GEE) — same
failure mode as Trap 27, wider than just `ecoscope.*`. `wt-compiler`'s task-discovery env is
a bare pip/uv venv with only `wt-registry` + the package's own declared deps installed; it
does not have any conda-installed packages, including `ecoscope-workflows-core`. A
module-level `from ecoscope_workflows_core.tasks.filter import TimeRange` silently broke
discovery for **every** task in the file (all showed as "not a registered known task
name", not just the new ones) — no import traceback surfaced, so the symptom looked
unrelated to the actual cause. Also do not add `ecoscope-workflows-core` to the package's
`pyproject.toml` dependencies — it isn't on PyPI, so the compiler's pip-install step
fails outright ("was not found in the package registry").
**Fix:** never construct a `TimeRange` (or import anything from `ecoscope_workflows_core`)
inside a custom task. Instead, do the date-string parsing in plain stdlib
(`datetime.strptime` → tz-aware `datetime`, exactly like `compute_dnbr_severity` already
does internally) and feed the result into the **built-in** `set_time_range` task via
`partial:` — it already knows how to construct the exact `TimeRange` type downstream
tasks expect, so a custom package never needs to depend on the class itself.

## Dashboard layout (8 widgets, 0-indexed)

| widget_id | Widget | x | w | y | h |
|---|---|---|---|---|---|
| 0 | Burned | 0 | 3 | 0 | 3 |
| 1 | High Sev | 3 | 3 | 0 | 3 |
| 2 | % Burned | 6 | 2 | 0 | 3 |
| 3 | Threshold | 8 | 2 | 0 | 3 |
| 4 | Mean dNBR | 0 | 4 | 3 | 3 |
| 5 | Pre Imgs | 4 | 3 | 3 | 3 |
| 6 | Post Imgs | 7 | 3 | 3 | 3 |
| 7 | Map | 0 | 10 | 6 | 16 |

Row 1 & 2 each sum to 10 (3+3+2+2, 4+3+3). Map full width.

## Requirements (working pattern — top-level Trap 28)

```yaml
requirements:
  - name: "ecoscope-platform"
    version: ">=2.15.0,<2.16.0"
    channel: "https://repo.prefix.dev/ecoscope-workflows/"
  - name: pydeck
    version: "0.9.2"
  - name: "dnbr-severity-tasks"
    path: "/home/sam/Ecoscope_Projects/dnbr-severity-tasks"
    editable: true
```

## Post-compile patch (every recompile)

```bash
cp -r /home/sam/Ecoscope_Projects/dnbr-severity-tasks ecoscope-workflows-*-workflow/dnbr-severity-tasks
sed -i 's|path = "/home/sam/Ecoscope_Projects/dnbr-severity-tasks"|path = "./dnbr-severity-tasks"|' \
  ecoscope-workflows-*-workflow/pixi.toml
cd ecoscope-workflows-*-workflow && pixi install && cd ..
```

## Verification status (2026-07-02)

Compiled clean at v1.0.0; `pixi install` lock includes win-64; task module imports; form
schema correct (satellite dropdown, two required dates, dnbr_threshold field verified
0.10-0.50/default 0.20, client/aoi hidden). Pure-Python logic unit-tested (severity index
at all boundaries, stat tasks, empty-GDF guards, threshold formatter/getter, patch-size
math, threshold-floor consistency). **First live Desktop run completed** on a real reserve
— see incident below. `--mock-io` still hangs in the headless sandbox (blocks on GEE init),
not a workflow defect — real runs must happen in Desktop.

## Incident: first Desktop run showed 73.5% of reserve "burned" (false positive)

Real run 2026-07-02: fire_start=2026-05-01, fire_end=2026-05-31 (a full calendar month
scan, not a known fire date), Sentinel-2, defaults otherwise (this was BEFORE the
threshold field existed — v0.0.0/first compile). Result: 73.5% of AOI classified burned,
Low-class mean dNBR only 123 (barely above the old fixed 100 floor). User confirmed this
area did not actually burn.

**Root cause diagnosis (confirmed by pulling the run's own GeoJSON):**
1. **Primary cause — seasonal mismatch, not a code bug.** Pre-composite (Jan31-May1,
   SA autumn, greener) vs post-composite (May31-Jun30, deeper into dry season, cured
   grass) straddles a genuine wet→dry phenological transition across the whole reserve.
   This produces a landscape-wide weak-positive dNBR unrelated to fire. The 8 small
   Moderate-High patches (53 ha, dNBR≥440) in the same run are far more likely to be a
   real fire — senescence doesn't produce spatially coherent high-severity patches.
2. **Contributing — no tunable detection floor.** The workflow drew anything dNBR≥100
   (the most lenient point on the Key & Benson scale) with no way to raise it. Fixed by
   adding `dnbr_threshold` (this changelog entry).
3. **Checked and ruled out as a discriminator — MIRBI confidence tier.** 98% of this
   run's polygons were "confirmed" (MIRBI agrees with dNBR) despite this being a false
   positive. **MIRBI does not reliably separate real fire from grass senescence** — both
   processes reduce vegetation moisture and shift SWIR reflectance the same direction. Do
   not present "confirmed" rate to users as evidence of real fire; it only rules out
   cloud/shadow/water artifacts. Corrected this framing in the README (was previously
   implied to be a stronger signal than it is).
4. **Also found and fixed while investigating:** the 0.5 ha connected-pixel patch filter
   from `burn-scar-tasks` had been dropped during the original merge (my oversight).
   Restored it — but confirmed it's a no-op at the default 100 m scale, so it would not
   have fixed this particular incident on its own.

**Guidance added to README as a result:** use the narrowest genuinely-known fire window
rather than a calendar month; a uniform reserve-wide "Low" wash is diagnostic of seasonal
drying, not fire; raise `dnbr_threshold` toward 0.27-0.30 when that pattern appears.

## GitHub

Repo: https://github.com/cllrssml/dnbr-burn-severity (public, pushed 2026-07-02).
Org/reserve identity redacted from all tracked files before publishing (only the generic
`/home/sam/Ecoscope_Projects/...` dev path remains, matching the precedent already public
in sibling repos `fire-severity-dnbr` and `burn-scar-mapping`).

## Next steps before handing to Britt

- Re-run against the same reserve/month with the new `dnbr_threshold` field at 0.20 and
  at ~0.28 to confirm the false-positive area drops while the 8 real-looking patches survive.
- If possible, also re-run with a narrower, real fire-date window instead of a calendar month.
- Then share the published repo URL with Britt.
