# dNBR Burn Severity — EarthRanger Workflow

An [Ecoscope Desktop](https://ecoscope.io) workflow that maps **how severely a fire burned**
across an area of interest, using free satellite imagery from Google Earth Engine. You
give it an area (a boundary you already have in EarthRanger) and a fire date window — it
returns a colour-coded burn-severity map plus summary statistics. No fire report or
uploaded file is required.

It computes the **dNBR (Differenced Normalized Burn Ratio)**, the standard remote-sensing
index for burn severity, and classifies every burned pixel into the widely used USGS
severity classes (Key & Benson 2006).

---

## What you provide

| Input | What it is |
|---|---|
| **Area of Interest** | An EarthRanger spatial features group — a reserve boundary, a block, or any polygon you want to scan. |
| **Fire Start / End Date** | The window the fire burned in. Use the same date twice for a single-day fire. |
| **Satellite** | **Sentinel-2** (recommended) — 10–20 m, from 2015. **Landsat** — 30 m, archive back to 1984, for older fires. |

Everything else has sensible defaults.

---

## What it measures

```
NBR  = (NIR − SWIR2) / (NIR + SWIR2)
dNBR = (pre-fire NBR − post-fire NBR) × 1000
```

Healthy vegetation reflects strongly in the near-infrared (NIR) and moderately in
shortwave-infrared (SWIR2); fire flips that relationship. A large positive dNBR means the
vegetation was consumed and soil exposed — a severe burn.

Rather than a single image, the workflow uses **mean compositing** (Parks et al. 2018): it
averages every available cloud-free scene in the pre- and post-fire windows, which is far
more robust than picking one image.

A second, savanna-specific index (**MIRBI**, Trigg & Flasse 2001) is computed independently.
Where MIRBI *also* indicates burning, a patch is tagged **confirmed**; where only dNBR does,
it is tagged **probable**. This gives a built-in second opinion and is recorded in the
downloadable GeoJSON.

### Severity classes (Key & Benson 2006)

| Colour | Class | dNBR range | Meaning |
|---|---|---|---|
| 🟡 Yellow | Low | 0.10 – 0.27 | Surface/grass fire; canopy mostly intact |
| 🟠 Orange | Moderate-Low | 0.27 – 0.44 | Partial scorch; understory consumed |
| 🔴 Dark orange | Moderate-High | 0.44 – 0.66 | Significant canopy loss |
| 🟥 Deep red | High | > 0.66 | Near-complete vegetation loss; soil exposed |

Only burned pixels (Low and above) are drawn on the map; unburned ground stays as basemap.

---

## Dashboard outputs

| Stat card | What it shows |
|---|---|
| **Burned** | Total area classified Low severity or higher |
| **High Sev** | Area classified Moderate-High or High — the most ecologically significant portion |
| **% Burned** | Burned area as a percentage of the whole AOI |
| **Threshold** | The dNBR Detection Threshold used for this run — check this if comparing runs |
| **Mean dNBR** | Area-weighted mean dNBR across the burned area — a single burn-intensity number |
| **Pre Imgs** | Number of scenes in the pre-fire composite (higher = more reliable) |
| **Post Imgs** | Number of scenes in the post-fire composite (low counts = noisier signal) |
| **Map** | Burned area colour-coded by severity, over a satellite basemap |

The **Files** tab has a downloadable **GeoJSON** of every burned polygon with its area,
severity class, mean dNBR, ΔMIRBI, and confidence tier.

---

## Requirements

- **Ecoscope Desktop** (Windows) — [download here](https://ecoscope.io)
- An **EarthRanger** data source configured in Desktop, containing the spatial feature
  group you want to use as the AOI
- A **Google Earth Engine** data source configured in Desktop

## Installation

1. In Ecoscope Desktop, go to **Workflow Templates → + Add Template**
2. Paste this repository's GitHub URL
3. Desktop imports and installs it automatically

---

## Configuration

### Area of Interest *(required)*
The EarthRanger spatial features group defining the area to map. Find groups under
**Admin → Map Layers → Feature Groups**.

### Fire Start / End Date *(required)*
The period the fire burned. The pre-fire composite is built from imagery *before* the start
date; the post-fire composite from imagery *after* the end date.

> **Use the narrowest window you actually know, not a convenient calendar month.** If you
> scan a full month rather than a specific fire's real start/end, the pre- and post-fire
> composites can land in genuinely different seasons (e.g. wetter autumn vs. cured-grass
> winter). That produces a landscape-wide "Low" severity signal from ordinary vegetation
> drying, not fire — see the caveats section below for how to recognise this.

### Satellite *(default: Sentinel-2)*
- **Sentinel-2** — 10–20 m resolution, 2–5 day revisit, available from 2015. Best for
  recent fires and the higher-resolution option.
- **Landsat** — 30 m, but the archive reaches back to 1984. Use it for fires before ~2015.

### Advanced (sensible defaults)

**dNBR Detection Threshold** *(default 0.20)* — the minimum dNBR for a pixel to count as
burned at all. This is separate from the severity colour a pixel gets once it clears the
threshold — colours always follow the standard Key & Benson bins. Raise this (0.27–0.30) if
a run shows a large, diffuse "Low" burn spread evenly across most of the AOI: that pattern
is the signature of seasonal vegetation drying, not a real fire scar (see caveats below). A
genuine fire is spatially coherent — it does not cover 70%+ of a whole reserve uniformly.

**Pre-Fire Window (days)** *(default 90)* — days before the start date to composite. For a
fire late in the dry season, increase to 120–180 to keep the baseline anchored in greener
conditions.

**Post-Fire Window (days)** *(default 30)* — days after the end date to composite. In fast
-recovering grassland, keep it short (14–30) so regrowth doesn't dilute the signal; use up
to 60 for slow-recovering vegetation.

**Analysis Scale (metres)** *(default 100)* — pixel size for the analysis. 100 m works for
reserves up to ~500,000 ha. Drop to 20–50 m for small areas (< 5,000 ha) where detail
matters. Finer scales over large areas will exceed Earth Engine's memory limit — the
workflow tells you and suggests a coarser scale.

---

## Interpreting results — important caveats

**A large, diffuse "Low" burn spread evenly across most of the AOI is very likely NOT a
real fire — it's seasonal vegetation drying.** Ordinary grass curing (green → brown as the
dry season progresses) reduces vegetation moisture the same way fire does, producing a weak
but widespread positive dNBR. A real fire scar is spatially coherent — a patch or a group of
patches with clear edges, not a uniform wash across 70%+ of a whole reserve. If you see the
latter: (1) raise the **dNBR Detection Threshold** toward 0.27–0.30, and (2) check whether
your Fire Start/End Date is a real, narrow, known fire window rather than a calendar month
used to "scan" for any fire — the wider that window, and the more it straddles a seasonal
transition, the more of this false signal you'll get.

**The "confirmed" / "probable" confidence tier does NOT reliably separate real fire from
seasonal drying.** MIRBI (the second index behind the tier) is also sensitive to vegetation
moisture loss, so cured grass can independently trigger it too — a high "confirmed" rate is
not on its own proof of a real burn. It's still useful for ruling out cloud/shadow/water
artifacts; just don't treat it as a fire/no-fire verdict.

**dNBR thresholds were calibrated in North American conifer forests.** In savanna and
grassland, even a complete grass burn may only reach Low/Unburned dNBR because pre-fire
vegetation is sparser. Within a single fire the *relative* pattern (which areas burned
hardest) is still meaningful; do not compare absolute dNBR between a savanna fire and a
forest fire.

**Check the Pre/Post Imgs cards.** A composite from many scenes is far more reliable than
one from 1–2. If either count is very low, widen the corresponding window — a single cloudy
or smoky image can distort the result.

**Recent post-fire imagery can carry smoke.** Standard cloud masking doesn't remove all
smoke; a very short post-fire window in smoky conditions may underestimate severity.

**The minimum patch-size filter (0.5 ha) is a no-op at the default 100 m scale.** One pixel
is already 1 ha at 100 m, so there's nothing smaller to filter out. It only removes
single-pixel speckle below ~70 m analysis scale. It will not, by itself, remove a
widespread drying signal — use the detection threshold for that.

---

## Troubleshooting

**"No Sentinel-2 / Landsat imagery for the pre-fire window"** — no cloud-free scenes over
this area in the window. Increase the Pre-Fire Window, or (for older fires) switch the
Satellite to Landsat.

**"AOI … would need ~N pixels — Earth Engine's limit is ~500,000"** — the area is too large
for the chosen scale. Increase Analysis Scale to the value suggested in the message.

**Map shows no burned area** — check the dates cover the actual fire, confirm the AOI covers
the burn, and check Post Imgs isn't 1–2 (a smoky scene can suppress the signal). In fast
-recovering grassland, a long post-fire window can also erase the signal.

---

## Science references

Key, C. H., & Benson, N. C. (2006). Landscape Assessment: Ground measure of severity, the
Composite Burn Index; and remote sensing of severity, the Normalized Burn Ratio. In
*FIREMON: Fire Effects Monitoring and Inventory System*, USDA Forest Service RMRS-GTR-164-CD.

Parks, S. A., Holsinger, L. M., Voss, M. A., Loehman, R. A., & Robinson, N. P. (2018). Mean
composite fire severity metrics computed with Google Earth Engine offer improved accuracy
and expanded mapping potential. *Remote Sensing*, 10(6), 879.
https://doi.org/10.3390/rs10060879

Trigg, S., & Flasse, S. (2001). An evaluation of different bi-spectral spaces for
discriminating burned shrub-savanna. *International Journal of Remote Sensing*, 22(13),
2641–2647.

Bastarrika, A., et al. (2024). Dual-index burned-area confirmation for Sentinel-2.
*ISPRS Journal of Photogrammetry and Remote Sensing*, 218.

---

## License

BSD 3-Clause — see [LICENSE](LICENSE). Copyright Sam Cilliers.
