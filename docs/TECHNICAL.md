# RushHour technical notes

Details behind the [README](../README.md): data sources, the model, the CLI, development, and deployment.

## Networks

| Source | Notes |
|---|---|
| Demo grid | Synthetic teaching network; works offline |
| GraphML | Recommended ready-to-use format; keeps node coordinates and attributes |
| OpenStreetMap | Search by place name; needs `.[osm]`. Provides geometry and mapped tags (one-way, speed, lanes, bridges), but no traffic counts or demand |
| GIS files | GeoJSON, zipped Shapefile, or zipped File Geodatabase; needs `.[gis]` |
| ArcGIS REST / GeoJSON URL | Public HTTPS layers on approved hosts; supports a bounding box and a `WHERE` filter |
| PDF trace | Draw road centerlines over a PDF map; crossings become intersections |
| `data/networks/<city>/` | Discovered recursively and offered as named choices at startup |

GIS centerlines carry geometry only. Speed, lanes, capacity per lane, and two-way direction are uniform assumptions you set in **GIS import settings**. Imports are capped at 100,000 features per dataset; use a bounding box to clip larger sources.

OpenStreetMap-derived output should be attributed to OpenStreetMap contributors when published.

### Demand

`volume_vph` is vehicles per hour for one origin-destination (OD) pair. The generated example trips are illustrative samples near the edges of the network, not observed traffic counts. For realistic results, download the example CSV from the app, replace its rows with your own intersection IDs and volumes, and upload it.

## How the model works

- **Link cost.** Each directed link has a free-flow time, capacity, lane count, and optional signal delay. Congested time follows the Bureau of Public Roads (BPR) curve:

  `t = t0 * (1 + alpha * (flow / capacity)^beta) + signal_delay`

  with defaults `alpha = 0.15` and `beta = 4`.
- **Route choice.** Steady commuters take the lowest perceived-cost path. Flexible drivers can split across up to a configurable number of near-shortest routes within their detour tolerance, using a logit choice, with perceived cost rising with congestion aversion.
- **Assignment.** Flows are updated iteratively with the method of successive averages until the relative gap falls below the convergence threshold or the iteration limit is reached.
- **Relative gap** is the remaining class-weighted perceived-cost improvement versus shortest paths. Because classes can have different perceptions and accept detours, this is a bounded-rational stochastic user-equilibrium approximation, not an exact Wardrop equilibrium.
- **Metrics.** P95 is a volume-weighted percentile over shortest perceived-cost paths for the OD set. Total delay is the sum of link-level vehicle-minutes above free-flow time.
- **Scenarios** are `baseline`, `bridge_closure`, `add_lane`, `lower_speed_limit`, and `add_signal`. Where bridges are tagged, they target those links. Otherwise a baseline run is assigned first, lane, speed, and signal changes target a busy end-to-end route used by the current demand, and the closure target is chosen so every OD pair stays connected.

This is a **static assignment model**: it finds steady-state flows and does not simulate minute-by-minute traffic or individual vehicles. Default demand and capacities are uncalibrated, so treat results as illustrations, not forecasts.

## Command line

```bash
# One scenario
rushhour run --scenario bridge_closure --output results/bridge-closure.json

# Baseline plus all interventions, with deltas from baseline
rushhour compare --output results/demo-comparison.json \
  --plot assets/scenario-comparison.png

# Higher demand or a different iteration limit
rushhour run --demand-scale 1.4 --iterations 120

# Download an OpenStreetMap network, then compare on it
rushhour download-osm --place "Cambridge, Massachusetts, USA" --output data/cambridge.graphml
rushhour compare --network data/cambridge.graphml --iterations 100 --output results/cambridge.json
```

Results are JSON, including convergence, trip-time summaries, and the ten most congested links.

## Development

```bash
pytest
ruff check src tests scripts app.py
python -m scripts.profile_simulation    # profile the assignment kernel
```

The tests check analytic BPR values, diversion to parallel routes under congestion, driver-class effects, scenario behavior, graph normalization, GIS and tracing importers, and map rendering. Runs are deterministic for a given graph, OD table, driver mix, and iteration count.

Runtime depends on network size, OD count, driver classes, and alternative routes. Most of the cost on large networks is NetworkX's alternative-path search, so benchmark a representative graph before relying on a scale limit.

### Project structure

```text
app.py                    Streamlit screens, state, import controls, downloads
src/rushhour/
  assignment.py           Driver classes, BPR costs, iterative route assignment
  metrics.py              Travel time, delay, convergence, congestion metrics
  networks.py             Demo graph, demand, GraphML normalization, optional OSMnx
  gis.py                  GeoJSON/ZIP/ArcGIS import and network building
  scenarios.py            Closures and infrastructure interventions
  tracing.py              PDF rendering and traced-graph creation
  visualization.py        Network and congestion figures
  catalog.py              Bundled-network discovery
  canvas_compat.py        Streamlit canvas compatibility shim
  cli.py                  Command-line tools
tests/                    Model, GIS, scenario, tracing, and rendering tests
data/networks/            Bundled city street-centerline datasets
assets/                   Images used by this README
.streamlit/config.toml    Theme, upload, CORS, and XSRF settings
```

The simulation engine is separate from the page: `app.py` handles uploads and settings and calls `networks.py`/`gis.py`/`tracing.py` to build a graph, then `assignment.py` and `scenarios.py` to simulate, then `metrics.py` and `visualization.py` for outputs. `cli.py` reuses the same modules without the web interface. To add an intervention, extend `scenarios.py` and add tests.

## Deploy

**Docker**

```bash
docker build -t rushhour .
docker run --rm -p 8501:8501 rushhour
```

The image runs as a non-root user with a health check and installs the core dependencies only. The OSM and GIS extras and the bundled city networks are not included, so the demo grid, GraphML, and PDF tracing work out of the box.

**Streamlit Community Cloud.** Push to GitHub, choose `app.py` as the entry point, and use Python 3.12. `requirements.txt` lists the runtime dependencies. Add `geopandas`, `pyogrio`, and `shapely` to it to enable GIS imports and the bundled cities. OpenStreetMap downloads are not automatic in the hosted app; download a GraphML region separately and upload it.

**Security.** The app has no authentication, accounts, or rate limits. For public deployment, add access control, TLS, and rate limiting at the host or a reverse proxy. Uploads are limited to 50 MB, expanded GIS ZIPs to 200 MB, and remote GIS downloads to approved HTTPS ArcGIS Online and NYC.gov hosts. Never commit credentials; `.env` and `.streamlit/secrets.toml` are git-ignored.

**CI.** GitHub Actions runs Ruff and the tests on Python 3.10 and 3.12 for pushes and pull requests.

## Troubleshooting

- **OpenStreetMap errors with HTTP 429/502/503/504** mean the public Overpass service is busy or rate-limiting you, not that the simulator failed. Retry later with a smaller area, or point to another compatible endpoint with `RUSHHOUR_OVERPASS_URL` and restart Streamlit.
- **"OSMnx is not installed"** means the Python running Streamlit lacks it. Run `python -m pip install -e ".[osm]"` in that environment and restart.
- **A simulation looks stuck on a large city network.** Lower the **Assignment detail** setting.

## Limitations

- OpenStreetMap and GIS centerlines do not provide traffic counts, live traffic, or OD demand.
- Turn restrictions are not enforced during route assignment.
- Every 2D crossing in a PDF trace becomes an intersection, so grade-separated roads need review.
- On two-way roads, the congestion map draws both directions on top of each other.
- Before using results for planning, calibrate capacities and speeds against local data and validate modeled travel times against independent counts or probe data.