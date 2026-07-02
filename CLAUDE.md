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
`SEVERITY_CLASSES` exactly (verified at every boundary). Only classes ≥ 2 (Low+) are
vectorised for the map via `reduceToVectors(labelProperty="severity_index")` — scales to
reserve size, unlike per-pixel `.sample()`.

---

## Task chain

`set_workflow_details` → `set_er_connection` → `set_gee_connection` →
`set_aoi_group_name` → `get_spatial_features_group` id=`aoi_features` →
`set_overlay_group_name` → `get_spatial_features_group` id=`overlay_features` (`skipif: any_dependency_is_empty_string`) →
`set_base_maps` →
`compute_dnbr_severity` (custom; partial client+aoi; user params: fire_start_date, fire_end_date, satellite=Sentinel-2, pre_fire_days=90, post_fire_days=30, scale=100) →
`create_dnbr_severity_layer` (zoom=False) →
`create_styled_overlay_layer` id=`aoi_layer` (zoom=true — AOI boundary is the zoom target) →
`create_styled_overlay_layer` id=`overlay_layer` (zoom=false; `skipif: any_dependency_skipped, any_is_empty_df`) →
`combine_severity_layers` (`skipif: any_is_empty_df` ONLY — handles SkipSentinel) →
`draw_ecomap` → `persist_text` → `create_map_widget_single_view` (`skipif: never`) →
`persist_df` id=`severity_file` (geojson → Files tab) →
stat chain: burned_area → high_severity → (aoi_area → percent_burned) → mean_dnbr → pre_scenes → post_scenes →
`gather_dashboard` (`time_range: ~`).

## Dashboard layout (7 widgets, 0-indexed)

| widget_id | Widget | x | w | y | h |
|---|---|---|---|---|---|
| 0 | Burned | 0 | 4 | 0 | 3 |
| 1 | High Sev | 4 | 3 | 0 | 3 |
| 2 | % Burned | 7 | 3 | 0 | 3 |
| 3 | Mean dNBR | 0 | 4 | 3 | 3 |
| 4 | Pre Imgs | 4 | 3 | 3 | 3 |
| 5 | Post Imgs | 7 | 3 | 3 | 3 |
| 6 | Map | 0 | 10 | 6 | 16 |

Row 1 & 2 each sum to 10 (4+3+3). Map full width.

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

Compiled clean; `pixi install` lock includes win-64; task module imports; form schema
correct (satellite dropdown, two required dates, client/aoi hidden). Pure-Python logic
unit-tested (severity index at all boundaries, stat tasks, empty-GDF guards).
**Not yet run against live GEE or imported to Desktop** — needs Sam's ER + GEE
credentials. `--mock-io` hangs in the headless sandbox (blocks on GEE init), not a
workflow defect.

## Next steps before handing to Britt

- Import to Desktop via GitHub URL and run against a known CFW fire (both sensors).
- Confirm the severity map, legend, and GeoJSON render; sanity-check burned area vs a known burn.
- Then publish repo `cllrssml/dnbr-burn-severity` and share with Britt.
