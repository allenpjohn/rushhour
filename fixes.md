# RushHour: fix log (Oct 11, 2026)

A walkthrough of what was wrong, why it was wrong, and how it was fixed, written so you can
reuse the ideas. Every fix has a test in `tests/test_regressions.py`; those tests **fail on your
original code** and pass now. `scripts/repro_bugs.py` prints the before/after behaviour.

## How I worked (a repeatable method)

1. **Baseline first.** Ran your existing tests and `ruff`: they all passed. So the bugs were ones
   your tests didn't exercise. A green test suite only proves the things it checks.
2. **Hunt, then reproduce.** I read each module looking for places where the code's *intent*
   (its docstring, the UI text, the README) disagreed with what it would actually do. Then I wrote
   `scripts/repro_bugs.py` to turn each suspicion into a concrete wrong number.
3. **Fix, then pin with a test.** For each bug: smallest fix, then a test that fails on the old
   code. I ran the new tests against your untouched originals to prove they catch the bug.
4. **Check nothing else moved.** I regenerated the demo comparison: only the one metric tied to
   a bug changed.

## The fixes

All eight fixes below are implemented in the repository and covered by
`tests/test_regressions.py`; the PDF renderer also has coverage in
`tests/test_tracing.py`.

### 1. Snap tolerance of 0 split a crossing into two disconnected nodes  (`tracing.py`)
- **Symptom:** two roads crossing at one point produced 6 nodes and 2 disconnected pieces.
- **Cause:** `_snap_node` only compared points `if tolerance:`. `0` is falsy in Python, so with
  the slider at 0 even *identical* points were never merged.
- **Fix:** always compare `distance <= tolerance`. With tolerance 0 that means "merge exact matches".
- **Lesson:** `if x:` is not the same as `if x is not None:` or "x was provided". Zero is a valid value.

### 2. A side street stopping just short of a road never connected  (`tracing.py`)
- **Symptom:** a street ending 3 px from another road gave two separate networks, so demand
  between them fails with "no route".
- **Cause:** intersections were only found where two drawn segments *actually cross*. Hand-drawn
  lines almost never touch exactly. The "snap tolerance" only merged nearby *nodes*, never an
  endpoint near the *middle* of another road.
- **Fix:** `_project_onto_segment` finds the closest point on another segment. If an endpoint is
  within the tolerance, that segment is split there, creating a T-junction.
- **Lesson:** "closest point on a segment" (clamp the projection parameter t to 0..1) is a core
  geometry tool. You'll reuse it for the PDF feature.

### 3. Opposite one-way roads were merged into one  (`tracing.py`)
- **Cause:** edges were de-duplicated with `frozenset({a, b})`, which ignores direction. For
  one-way roads A→B and B→A are different roads.
- **Fix:** use `frozenset` for two-way roads but an ordered `(a, b)` tuple for one-way roads.

### 4. Lowering a speed limit *reduced* "delay"  (`scenarios.py`, `networks.py`, `metrics.py`)
- **Symptom (visible in your own `demo-comparison.json`):** lower_speed_limit had a *longer* mean
  trip (+2.9%) but delay **−16%**. Delay of 2,854 vs 3,400 baseline. Slower roads, less delay?
- **Cause:** delay = time above *free-flow* time. The scenario overwrote `free_flow_min` with the
  slower value, so the slowdown became the new "free flow" and stopped counting as delay.
- **Fix:** each edge remembers `reference_free_flow_min` (its original free-flow time).
  Scenarios keep it; `normalize_graph` carries it through; delay is measured against it.
- **Result:** lower_speed_limit delay went 2,854 → 3,920 veh-min (baseline 3,400). Mean trip times
  are unchanged, and every other scenario is identical to before.
- **Lesson:** a metric needs a *fixed* reference. If the thing you're measuring against changes
  with the scenario, you're measuring nothing.

### 5. The convergence gap described the wrong flows  (`assignment.py`)
- **Symptom:** `simulate(..., max_iterations=1)` reported a gap of `0.0`, which reads as "perfectly
  converged". It was just 0/0 on an empty network.
- **Cause:** each loop iteration measured the gap, *then* updated the flows. If the loop ran out
  of iterations, the reported gap belonged to the flows from before the last update.
- **Fix:** moved "load the network and measure the gap" into one inner function. If the loop does
  not converge, measure once more on the final flows. Same pass also: skips driver classes with
  share 0 and sets link costs once per class instead of once per origin.
- **Honest note on speed:** I timed old vs new on the demo (2.49 s vs 2.56 s for 80 iterations):
  **no speedup**. The real cost is NetworkX's alternative-path search (`shortest_simple_paths`).
  Worth attacking if you scale to a real city.

### 6. Fabric.js line coordinates ignored the object's origin  (`tracing.py`)
- **Cause:** Fabric stores a line's `x1..y2` relative to the line's *center*, but `left/top` is the
  position of the object's *origin* (often its left edge). The old code treated `left/top` as the
  center, so lines would be shifted by half their width/height whenever the origin isn't "center".
- **Fix:** convert origin → center using `originX/originY` and the scaled width/height first.
- **Confidence:** based on Fabric's documented behaviour and tested with synthetic objects. I could
  not run the live canvas here, so please draw one line in the app and check the preview lines up.

### 7. PDF library was a hard import, and the PDF handle was never closed  (`tracing.py`)
- `import pymupdf` now happens inside `render_pdf_page`, so the tracing/graph code works without
  it and you get a clear error if it's missing. The document is closed with `with document:`.

### 8. Background image would crash with newer Streamlit  (`canvas_compat.py`, `app.py`)
- `streamlit-drawable-canvas` calls `streamlit.elements.image.image_to_url`, which newer Streamlit
  removed, which breaks `st_canvas(background_image=...)` and so the whole PDF tab.
- **Fix:** a small shim installs a replacement (inline `data:` URL) **only if the function is
  missing**. It's a no-op on versions that still have it.
- **Confidence:** tested against both a stand-in legacy module and the installed current Streamlit
  app. The dashboard starts and renders; manually drawing on the PDF canvas remains a useful
  final interactive check.

### 9. City GIS street-centerline data can become a simulated network (`gis.py`, `app.py`)
- **Inputs:** GeoJSON, zipped Shapefile, zipped File Geodatabase, public ArcGIS FeatureServer
  layers, and GeoJSON URLs. Multiple-layer containers allow choosing the street-centerline layer.
- **Processing:** optional `.[gis]` dependencies read the source, then RushHour projects to a local
  metric CRS, clips an optional WGS84 study-area box, snaps nearby endpoints, and nodes crossings.
  The resulting graph can be selected and simulated like GraphML or PDF traces.
- **Import efficiency:** ArcGIS requests now fetch only object ID plus geometry by default, support a
  server-side `WHERE` filter, and allow up to 100,000 features. GeoJSON bounds are applied by the
  file reader and unused properties are skipped. Network construction no longer repeats archive
  extraction internally; the UI still inspects an archive once to populate its layer selector.
  Stable source signatures and in-memory graph caching avoid repeatedly serializing large graphs
  when Streamlit reruns.
- **Limits:** GIS line geometry does not by itself tell RushHour direction, speed, or capacity.
  The UI exposes uniform defaults and a two-way toggle instead of pretending those attributes were
  inferred. ArcGIS requests are paginated, and imports are capped at 100,000 features/segments.
- **Checks:** generated GeoJSON and zipped-Shapefile fixtures both produce routable graphs. The
  installed Pyogrio/GDAL advertises the OpenFileGDB read driver; no NYC production dataset was
  downloaded during this change.

### 10. Large imported networks no longer use the full route-choice workload by default (`app.py`, `assignment.py`, `metrics.py`, `networks.py`)
- **Symptom:** the 4.4 MB Tallahassee GeoJSON imported quickly, but running a simulation appeared
  stuck while assigning traffic. The file has 5,657 line features and builds a graph with 33,943
  nodes and 69,100 directed links.
- **Cause:** the delay was in route assignment, not file loading. The usual 80-iteration,
  multi-route profile repeatedly searches for alternative paths, which is costly on this graph.
- **Fix:** networks above 5,000 nodes use a fast profile by default: six default OD pairs, at most
  five iterations, and one route per trip. The user can turn the profile off for the full model.
  Iteration progress is shown in the UI. Trip-time metrics also reuse shortest-path trees for
  destinations sharing an origin.
- **Observed check:** on the supplied GeoJSON, the fast profile finished in about 7.3 seconds and
  converged after three iterations (relative gap about 0.0028). Timing varies by machine; this
  profile is a quick initial assignment, not a substitute for the full route-choice model.
- **Behavior check:** demo comparison output was unchanged by the shortest-path metrics
  optimization.

### 11. Dense city maps no longer collapse into a black patch, and local city files are discoverable (`visualization.py`, `gis.py`, `app.py`)
- **Symptom:** the Tallahassee network map looked black because tens of thousands of dark,
  large intersection markers and thick, overlapping links were drawn together.
- **Fix:** dense maps draw thin, pale road geometry without individual node markers, then overlay
  colored links that carry assigned flow. The color bar is volume/capacity, and its display
  threshold is adjustable independently of the simulation.
- **Congestion settings:** imported GIS roads now expose a configurable default capacity per
  lane. Together with the existing speed, lane, demand-scale, and BPR alpha/beta controls, this
  lets users adjust the assumed congestion response when the source has geometry but no traffic
  capacity attributes.
- **Local network catalog:** files under `data/networks/<city>/` are discovered recursively and
  loaded as named network choices when the app starts. Supported files are GraphML, GeoJSON,
  zipped Shapefile, and zipped File Geodatabase; multi-layer GIS archives expose each layer.
- **Checks:** map regression tests verify the flow color mapping and skip markers above 5,000
  nodes. The supplied 33,943-node network rendered with no node-marker collection and produced
  orange/red active links; the Streamlit app health endpoint and baseline run also succeeded.

### 12. City-network interventions target roads actually used by the demand (`scenarios.py`, `app.py`, `cli.py`)
- **Symptom:** on the Tallahassee network, baseline, bridge closure, added lane, and lower speed
  all reported exactly the same mean (130.750889 min), P95 (150.291807 min), and delay
  (1,391.143 veh-min); only the signal scenario differed slightly.
- **Cause:** GIS imports do not have bridge tags, so scenario fallback selected a sampled
  edge-betweenness link unrelated to the six default trips. Four interventions therefore changed
  unused roads and had no effect on the measured demand. The fallback closure could also disconnect
  an OD pair.
- **Fix:** for untagged networks, app and CLI comparisons run baseline first. Lane, speed, and
  signal changes target every directed edge along a busy end-to-end OD route, selected using the
  baseline generalized travel times and assigned flows. A separate high-flow closure target is
  screened to preserve reachability for all OD pairs; closing an entire route would generally
  disconnect its endpoints. Single-scenario runs also get a baseline assignment to identify both
  targets. GIS lane additions use the network's existing capacity per lane rather than assuming
  900 vehicles/hour.
- **Validation:** the scenario regression test asserts that lane and speed changes cover every
  edge in the selected corridor while bridge closure uses its separate reachability-safe target.
  On the supplied Tallahassee file (33,943 intersections, 69,100 directed links, six demand
  pairs), all interventions now differ from baseline: mean times were 130.751 min baseline,
  142.722 closure, 129.145 added lane, 158.078 lower speed, and 130.917 with a signal. The full
  corridor had 3,146 directed links; closure used two directed links and preserved all OD routes.

## Noticed but deliberately not changed

- **Heatmap hides one direction.** Two-way roads are drawn on top of each other, so you only see
  one direction's congestion. Easy win later: offset each direction slightly.
- **Logit scale is relative.** In `_load_paths` the route-choice "spread" is the cost range of the
  candidate routes (min 0.1), so it adapts per OD pair. Defensible, but not a standard
  calibrated logit; worth documenting as a modeling choice.
- **`_run_signature` rebuilds GraphML on every rerun**, which will get slow on a big network.
- **Direct `apply_scenario` calls without demand-aware target edges** still use a graph-centrality
  fallback; on unusual sparse networks, closing that fallback link can disconnect an OD pair.
- **Build artifacts are in the project:** `requires.txt`, `SOURCES.txt`, `entry_points.txt`,
  `top_level.txt`, `dependency_links.txt` are generated into `*.egg-info/` and shouldn't be
  committed. `*.egg-info/` is already in `.gitignore`.
- `LICENSE`, `Dockerfile`, CI config, and `assets/` are referenced by the README but weren't in
  what you sent.

### 13. OSM feature support and misleading install prompt (`networks.py`, `app.py`)
- **Symptom:** the sidebar kept telling you to install OSMnx after you had installed it.
- **Cause:** the installation instruction was rendered unconditionally; it did not inspect the
  Python environment running Streamlit.
- **Fix:** the sidebar now reports the installed OSMnx version when available. If missing, it
  displays an install command using the active dashboard's `sys.executable` and asks for a
  Streamlit restart. The running dashboard here uses the project's `.venv`, where OSMnx 2.1.1
  was verified installed.
- **OSM entities:** road graphs preserve coordinates for routing nodes, one-way topology, road
  ways' line WKT, and mapped tags. A separate optional feature query uses OSMnx's feature API to
  retrieve tagged nodes, open/closed ways, and relation geometries for amenity, building,
  highway, land-use/leisure, natural, boundary, transit-route, and restriction categories.
  Turn-restriction relations are available for inspection but are not yet enforced in routing.
- **Validation:** mocked importer tests cover OSM feature tag selection and graph attribute
  preservation. All 44 tests pass, Ruff passes, and the live dashboard was restarted; its sidebar
  now reports **OpenStreetMap support ready · OSMnx 2.1.1** instead of prompting installation.

## What I could and couldn't run

| Check | Result |
|---|---|
| 34 tests (model, scenarios, networks, tracing, GIS, regressions, shim) | pass |
| `ruff check src tests scripts app.py` | pass |
| PDF rendering and document-close regression tests | pass |
| GeoJSON and zipped-Shapefile network imports | pass |
| GIS optional dependencies and OpenFileGDB driver | installed; read driver detected |
| Streamlit startup and dashboard page | pass; manually drawing on the PDF canvas not exercised |
| NYC service request / real File Geodatabase sample | not run |
| Original-version regression comparison | not repeated in this checkout |

The corrected `results/demo-comparison.json` changes only the lower-speed delay:
2,854 -> 3,920 vehicle-minutes (baseline 3,400), with the scenario's trip-time
results unchanged.

Run the standard checks with `python -m pip install -e ".[dev]"`, `pytest`, and
`streamlit run app.py`. Add `.[gis]` when using city GIS files or services.