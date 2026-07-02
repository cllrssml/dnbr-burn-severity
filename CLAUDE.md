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
`compute_dnbr_severity` (custom; partial client+aoi; user params: fire_start_date, fire_end_date, satellite=Sentinel-2, pre_fire_days=90, post_fire_days=30, scale=100, dnbr_threshold=0.20) →
`create_dnbr_severity_layer` (zoom=False) →
`create_styled_overlay_layer` id=`aoi_layer` (zoom=true — AOI boundary is the zoom target) →
`create_styled_overlay_layer` id=`overlay_layer` (zoom=false; `skipif: any_dependency_skipped, any_is_empty_df`) →
`combine_severity_layers` (`skipif: any_is_empty_df` ONLY — handles SkipSentinel) →
`draw_ecomap` → `persist_text` → `create_map_widget_single_view` (`skipif: never`) →
`persist_df` id=`severity_file` (geojson → Files tab) →
stat chain: burned_area → high_severity → (aoi_area → percent_burned) → threshold → mean_dnbr → pre_scenes → post_scenes →
`gather_dashboard` (`time_range: ~`).

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

## Next steps before handing to Britt

- Re-run against the same reserve/month with the new `dnbr_threshold` field at 0.20 and
  at ~0.28 to confirm the false-positive area drops while the 8 real-looking patches survive.
- If possible, also re-run with a narrower, real fire-date window instead of a calendar month.
- Then publish repo `cllrssml/dnbr-burn-severity` and share with Britt.
