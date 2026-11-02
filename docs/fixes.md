# RushHour fix log

A running list of bugs I found and fixed while building RushHour, why each one happened, and what I learned. Most fixes have a regression test in `tests/test_regressions.py` that fails on the old code. `scripts/repro_bugs.py` prints before/after numbers for the main ones.

## How I tracked bugs down

1. **Start from green tests.** My tests and `ruff` already passed, so the bugs were things the tests didn't cover. Passing tests only prove what they check.
2. **Look for places where the code and the docs disagree.** I read each module against its docstrings, the UI text, and the README, then wrote `scripts/repro_bugs.py` to turn each suspicion into a concrete wrong number.
3. **Smallest fix, then a test.** Each test fails on the old code and passes on the new.
4. **Re-run the demo comparison** to make sure only the metric tied to the bug changed.

## Fixes

### 1. Snap tolerance of 0 split a crossing into two nodes (`tracing.py`)
- **What happened:** two roads crossing at one point gave 6 nodes and 2 disconnected pieces.
- **Why:** `_snap_node` only compared points `if tolerance:`, and `0` is falsy in Python. With the slider at 0, even identical points never merged.
- **Fix:** always check `distance <= tolerance`. At 0 that means "merge exact matches".
- **Lesson:** `if x:` is not the same as "x was provided". Zero is a valid value.

### 2. A side street stopping just short of a road never connected (`tracing.py`)
- **What happened:** a street ending 3 px from another road produced two separate networks, so trips between them failed with "no route".
- **Why:** intersections were only found where drawn lines actually cross, and hand-drawn lines almost never touch exactly. Snap tolerance only merged nearby nodes, never an endpoint near the middle of another road.
- **Fix:** `_project_onto_segment` finds the closest point on another segment. If an endpoint is within tolerance, that segment is split there, making a T-junction.
- **Lesson:** "closest point on a segment" (clamp the projection to 0..1) is a core geometry tool, and I reused it for PDF tracing.

### 3. Opposite one-way roads got merged (`tracing.py`)
- **Why:** edges were de-duplicated with `frozenset({a, b})`, which ignores direction. For one-way roads, A→B and B→A are different roads.
- **Fix:** `frozenset` for two-way roads, an ordered `(a, b)` tuple for one-way.

### 4. Lowering a speed limit *reduced* delay (`scenarios.py`, `networks.py`, `metrics.py`)
- **What happened:** in my own `demo-comparison.json`, `lower_speed_limit` had a longer mean trip (+2.9%) but delay went *down* 16% (2,854 vs 3,400 baseline). Slower roads, less delay didn't make sense.
- **Why:** delay is time above free-flow time, and the scenario overwrote `free_flow_min` with the slower value. The slowdown became the new "free flow" and stopped counting.
- **Fix:** each edge keeps `reference_free_flow_min` (its original free-flow time). Scenarios preserve it, `normalize_graph` carries it through, and delay is measured against it.
- **Result:** lower-speed delay went from 2,854 to 3,920 veh-min. Mean trip times and the other scenarios didn't change.
- **Lesson:** a metric needs a fixed reference. If what you measure against changes with the scenario, you're not measuring anything.

### 5. The convergence gap described the wrong flows (`assignment.py`)
- **What happened:** `simulate(..., max_iterations=1)` reported a gap of `0.0`, which reads as "perfectly converged". It was really 0/0 on an empty network.
- **Why:** each iteration measured the gap and *then* updated flows, so if the loop ran out of iterations the reported gap belonged to the flows from before the last update.
- **Fix:** one inner function loads the network and measures the gap. If the loop doesn't converge, it measures once more on the final flows. While there, I made it skip driver classes with share 0 and set link costs once per class instead of once per origin.
- **Honest note:** I timed old vs new on the demo (2.49 s vs 2.56 s for 80 iterations), so no speedup. The real cost is NetworkX's alternative-path search (`shortest_simple_paths`), which matters once networks get big.

### 6. Canvas line coordinates ignored the object's origin (`tracing.py`)
- **Why:** Fabric.js stores a line's `x1..y2` relative to its center, but `left/top` is the position of the object's origin (often the left edge). The old code treated `left/top` as the center, so lines shifted by half their width or height.
- **Fix:** convert origin to center using `originX/originY` and the scaled width/height first.
- **Caveat:** this is based on Fabric's documented behavior and tested with synthetic objects. I still need to draw a line in the live app and check that the preview lines up.

### 7. PDF library was a hard import and the file handle never closed (`tracing.py`)
- `import pymupdf` now happens inside `render_pdf_page`, so graph-building works without it and a missing install gives a clear error. The document is closed with `with document:`.

### 8. Background image crashed on newer Streamlit (`canvas_compat.py`, `app.py`)
- **Why:** `streamlit-drawable-canvas` calls `streamlit.elements.image.image_to_url`, which newer Streamlit removed, so `st_canvas(background_image=...)` and the whole PDF tab broke.
- **Fix:** a small shim installs a replacement (inline `data:` URL) only if the function is missing. It does nothing on versions that still have it.
- **Caveat:** the dashboard starts and renders, but I haven't manually drawn on the PDF canvas yet.

### 9. City GIS street data can become a simulated network (`gis.py`, `app.py`)
- **Inputs:** GeoJSON, zipped Shapefile, zipped File Geodatabase, public ArcGIS FeatureServer layers, and GeoJSON URLs. Multi-layer files let you pick the street layer.
- **Processing:** the optional `.[gis]` packages read the source. RushHour projects to a local metric CRS, clips an optional WGS84 study area, snaps nearby endpoints, and nodes crossings.
- **Efficiency:** ArcGIS requests fetch only object ID and geometry by default, support a server-side `WHERE` filter, and cap at 100,000 features. GeoJSON bounds are applied while reading, and unused properties are skipped. Stable source signatures and in-memory caching avoid re-serializing big graphs on every Streamlit rerun.
- **Limits:** centerlines don't tell RushHour direction, speed, or capacity, so the UI exposes uniform defaults and a two-way toggle instead of pretending to infer them.
- **Checks:** generated GeoJSON and zipped-Shapefile fixtures both produce routable graphs, and the OpenFileGDB driver is available. I haven't tested a real File Geodatabase or the live NYC ArcGIS service.

### 10. Big networks made the simulation look stuck (`app.py`, `assignment.py`, `metrics.py`, `networks.py`)
- **What happened:** the ~4 MB Tallahassee GeoJSON (5,657 line features, 33,943 nodes, 69,100 directed links) imported fast, but running a simulation looked frozen.
- **Why:** the delay was route assignment, not loading. The default 80-iteration, multi-route setup repeatedly searches for alternative paths, which is costly on a graph this size.
- **Fix:** networks above 5,000 nodes use a lighter setup controlled by an **Assignment detail** slider. The default (Light) uses 3 sample trip locations, 3 iterations and 1 route per trip. Progress shows in the UI, and trip-time metrics reuse shortest-path trees for destinations that share an origin.
- **Check:** on the Tallahassee file, the fast setting finished in about 7 seconds and converged in 3 iterations (gap about 0.003). Timing varies by machine, and this is a quick first-pass assignment, not the full route-choice model.

### 11. Dense city maps turned into a black blob (`visualization.py`, `gis.py`, `app.py`)
- **Why:** tens of thousands of dark, large node markers and thick overlapping links were drawn on top of each other.
- **Fix:** dense maps draw thin, pale roads with no node markers, then overlay colored links only where traffic is assigned. The color bar is v/c, and the "turns red at" threshold only changes the display, not the simulation.
- **Also added:** a default capacity-per-lane setting for GIS roads, and the local network catalog that discovers files under `data/networks/<city>/` and lists them as choices at startup.

### 12. City-network scenarios didn't touch roads the demand used (`scenarios.py`, `app.py`, `cli.py`)
- **What happened:** on Tallahassee, baseline, closure, added lane, and lower speed all gave exactly the same mean (130.75 min), P95, and delay. Only the signal scenario differed slightly.
- **Why:** GIS data has no bridge tags, so the fallback picked a sampled high-betweenness link unrelated to my six default trips. Four scenarios changed roads nobody drove on. The closure fallback could also disconnect an OD pair.
- **Fix:** for untagged networks, comparisons run a baseline first. Lane, speed, and signal changes then target every directed edge along a busy end-to-end route that the demand actually uses. The closure target is chosen separately and checked so all OD pairs stay connected (closing a whole route would cut its endpoints off). Added lanes use the network's existing capacity per lane instead of assuming 900 veh/h.
- **Result on Tallahassee (6 OD pairs):** mean trip times are now 130.75 min baseline, 142.72 closure, 129.15 added lane, 158.08 lower speed, and 130.92 with a signal. The corridor had 3,146 directed links, and the closure used 2.

### 13. OSM features and a wrong "install OSMnx" warning (`networks.py`, `app.py`)
- **What happened:** the sidebar kept telling me to install OSMnx after I had.
- **Why:** the message was shown unconditionally and never checked the Python environment running Streamlit.
- **Fix:** the sidebar now shows the installed OSMnx version, or, if it's missing, an install command using the current `sys.executable`.
- **Also added:** an optional OSM feature query (amenities, buildings, roads, land use, natural features, boundaries, transit routes, turn restrictions) with a preview, map, and GeoJSON download. These are separate from the routing graph, and turn restrictions are not enforced in routing.

## Known issues / not changed yet

- **Heatmap hides one direction.** Two-way roads are drawn on top of each other. An easy fix later is to offset each direction slightly.
- **The logit scale is relative.** In `_load_paths`, route-choice spread is the cost range of the candidate routes (min 0.1), so it adapts per OD pair. It's defensible but not a standard calibrated logit, so I should document it as a modeling choice.
- **`_run_signature` can be slow on big graphs.** Graphs without a stored source signature get serialized to GraphML on every rerun. Setting a signature at load time for OSM and GIS-upload graphs would fix it.
- **Local city networks all load at startup.** That's fine for two cities but won't scale. Loading only the selected one would be better.
- **`apply_scenario` without demand-aware targets** still falls back to graph centrality, which can disconnect an OD pair on odd sparse networks.

## Current checks

- `pytest`: 48 passed
- `ruff check src tests scripts app.py`: clean
- GeoJSON and zipped-Shapefile imports: tested with fixtures and the bundled Tallahassee file
- Streamlit app starts and runs baseline and comparison: works
- Not tested yet: drawing on the PDF canvas, a real File Geodatabase, and the live NYC ArcGIS service

```bash
python -m pip install -e ".[dev,gis,osm]"
pytest
ruff check src tests scripts app.py
streamlit run app.py
```