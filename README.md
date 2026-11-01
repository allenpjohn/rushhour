# RushHour

RushHour is an interactive urban traffic-assignment lab. It models congestion on a
directed road network, lets driver populations choose different routes, and
compares infrastructure interventions using travel-time and delay metrics.

## Run it locally

Requirements: Python 3.10 or newer.

```bash
# macOS / Linux: create and activate an isolated environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install the app and development tools
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

# Start the interactive dashboard
streamlit run app.py
```

Open the local URL printed by Streamlit (normally http://localhost:8501). On
Windows, create the environment with `py -3.12 -m venv .venv` and activate it
with `.venv\Scripts\activate`.

The deterministic demo grid works offline and is suitable for a first run. To
download and simulate an OpenStreetMap road network:

```bash
python -m pip install -e ".[osm]"
rushhour download-osm \
  --place "Cambridge, Massachusetts, USA" \
  --output data/cambridge.graphml
rushhour compare --network data/cambridge.graphml --iterations 100 \
  --output results/cambridge.json
```

The OSMnx import is optional; route assignment, the demo network, CLI, and UI do
not need a network connection. You can also load a place directly from the
dashboard sidebar using **Load roads from OpenStreetMap**; the downloaded graph
appears as a selectable network and is cached while the app runs. OpenStreetMap
can provide road geometry and mapped attributes such as one-way direction,
speed limits, lane counts, road class, and bridge tags, where available. Missing
speeds use the app's fallback, and missing capacities are estimated from the
lane count. OSM does not provide complete traffic counts, live traffic, or an
origin–destination trip table, so realistic traffic demand and calibration
observations still need to come from other data. OpenStreetMap-derived outputs
should be attributed to OpenStreetMap contributors when published.

Both OSM buttons use public services: place-name lookup and Overpass for map
data. If an error identifies Overpass and returns HTTP `429`, `502`, `503`, or
`504`, the upstream Overpass service is busy or rate-limiting requests; this is
not an error in the simulator's route assignment. Try again later and keep the
query area small. To use an alternate compatible Overpass API, set
`RUSHHOUR_OVERPASS_URL` to its HTTPS API base URL before starting Streamlit, for
example:

```bash
export RUSHHOUR_OVERPASS_URL="https://your-overpass-host/api"
streamlit run app.py
```

Use an endpoint you trust and that permits your usage. The variable configures
the OSMnx Overpass endpoint for both road and feature queries. It does not
change the separate place-name lookup service. Restart the app after changing
the variable.

The sidebar's optional **Explore OSM map features** query can fetch selected
tagged amenities, buildings, roads and paths, land use, natural features,
administrative boundaries, transit routes, and turn-restriction relations. OSMnx
returns these as feature geometries: tagged nodes as points, open ways as lines,
closed ways as polygons, and relations as their available multipart or other
complex geometries. The app reports element and geometry types and previews
their tags, plots up to 5,000 geometries for preview, and lets you download the
full selected result as GeoJSON. These map features are separate from the
drivable routing graph; turn-restriction relations can be inspected but are not
yet enforced during route assignment. Select a small area and only the
categories you need to limit Overpass requests.

## Use a PDF road map

RushHour can render a PDF page as a tracing background. It does **not** infer
road topology automatically from a scan: draw road centerlines with the line
tool, and RushHour splits line crossings into intersections. Set the displayed
map width using its scale bar or another known distance, then choose default
speed and lane values. Review those assumptions and the preview; download the
resulting GraphML network to keep using or share it.

In the dashboard, open **Build from a PDF map**, upload the PDF, select its
page, calibrate the full displayed canvas width, trace roads, and select
**Create network from trace**. The generated graph becomes available under
**Active network**. You can also upload a CSV demand table with columns
`origin,destination,volume_vph` and download a template from the simulation tab.
Traced streets default to two-way; you can opt into one-way links, whose travel
direction follows the direction in which each line is drawn.

PDF tracing is useful when the map is legible and has a known scale, but it does
not infer road class, capacity, or traffic counts. Every 2D crossing is treated
as an intersection, so grade-separated roads should come from a routable source
or be carefully reviewed before analysis. For a ready-to-use and more accurate
real-city network, prefer **GraphML** exported from OSMnx or a GIS pipeline; it
retains node coordinates and road attributes. Use the map PDF as a visual
reference or manual-tracing source when you do not have that road graph.

## Import city GIS street data

The dashboard can turn street-centerline data into a routable network. Install
the optional geospatial readers before starting the app:

```bash
python -m pip install -e ".[gis]"
```

Supported inputs are GeoJSON (`.geojson`/`.json`), a ZIP containing one complete
ESRI Shapefile (`.shp`, `.shx`, `.dbf`, and preferably `.prj`), a ZIP containing
one File Geodatabase (`.gdb`), and public HTTPS ArcGIS FeatureServer layer or
GeoJSON URLs. A File Geodatabase must be zipped with its `.gdb` directory
preserved. If a dataset has multiple layers, choose its street-centerline layer.
The source must include a coordinate reference system; GeoJSON is interpreted
as WGS84.

For local, reusable city choices, place datasets under `data/networks/`. The
dashboard recursively discovers and loads supported GraphML, GeoJSON, and ZIP
files when it starts; directories directly below `networks/` become city
groups in the network names. For example:

```text
data/networks/
  tallahassee/
    Roadway_Functional_Classification.geojson
  nyc/
    street-centerlines.geojson
    arterials.zip
```

Multi-layer ZIP files appear as one choice per layer. Local networks use the
same GIS speed, lane, per-lane capacity, direction, snap, and study-area
settings as uploaded GIS datasets. They are cached while the app runs; restart
the app after replacing a source file.

For example, paste the NYC DCP **DCM Street Center Line** FeatureServer layer
URL into the GIS URL field:

```text
https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services/DCM_Street_Center_Line/FeatureServer/0
```

Use the optional `west,south,east,north` WGS84 bounds field to limit downloads
or clip a citywide file to a study area. The importer pages ArcGIS responses,
projects lines into a local metric coordinate system, nodes crossings, and
snaps nearby line endpoints. The default speed and lane count apply uniformly
to imported roads, and streets are treated as two-way unless disabled; these
are modeling assumptions, not values inferred from the map. GIS imports are
limited to 100,000 input features/road segments per dataset, so use bounds or
pre-filter a citywide source if necessary. ArcGIS imports can also use an
attribute `WHERE` clause; they request only the object ID and geometry unless
the URL explicitly asks for other fields. GeoJSON file imports apply the study
area during file reading and discard unused properties. For networks above 5,000 nodes, use the **Assignment detail** slider to trade
runtime for a more complete assignment. It adjusts the number of generated
sample trips, assignment iterations, and (at the highest settings) alternative
routes for flexible drivers. The slider ranges from Quick to Thorough; higher
settings can take substantially longer, especially on city-sized networks.
Driver share and congestion-aversion settings apply at every detail setting.
Gray links are in the imported network but carry none of the trips in the
current demand.

**OD** means origin–destination: one pair identifies a starting intersection
and an ending intersection. `volume_vph` is the vehicles per hour traveling
between that pair. The built-in trips are illustrative peak-hour samples
generated from locations near the edges of the network; they are not official
traffic counts or a standard real-world commute dataset. To model actual
citywide demand, download the editable example CSV and replace its rows with
your own origin and destination node IDs and hourly volumes, then upload it.
Imported graph identity is cached in memory without repeatedly serializing the
full graph. Review the generated graph and set representative local demand
before interpreting results.
Set **Default GIS capacity per lane** to calibrate congestion on centerline
sources that do not provide capacity. The BPR alpha/beta controls change how
quickly travel times rise with volume/capacity; the map's red-at-v/c control
changes only the display scale. Dense networks are rendered with thin neutral
street lines and colored only on links carrying assigned flow.
For networks without bridge tags, intervention comparisons first assign the
baseline demand, then apply lane, speed, and signal changes along a busy
end-to-end route actually used by that demand. The closure instead removes a
single high-flow link only when every OD pair remains connected.

## CLI

```bash
# Run an individual intervention
rushhour run --scenario bridge_closure --output results/bridge-closure.json

# Run baseline plus all interventions and report deltas from baseline
rushhour compare --output results/demo-comparison.json
rushhour compare --output results/demo-comparison.json \
  --plot assets/scenario-comparison.png

# Try higher demand or a different assignment iteration limit
rushhour run --demand-scale 1.4 --iterations 120
```

Built-in interventions: `baseline`, `bridge_closure`, `add_lane`,
`lower_speed_limit`, and `add_signal`. On networks with tagged bridges, these
target those links. Otherwise, comparisons first run a baseline, then target
the busiest corridor used by the current OD demand; a fallback closure target
is chosen only if it leaves every OD pair connected. Results are JSON,
including assignment convergence, trip-time summaries, and the ten most
congested directed links.

## Model

- Each directed link has a free-flow time, hourly capacity, lane count, and
  optional signal delay.
- Congested link time follows the Bureau of Public Roads (BPR) curve:

  `t = t0 * (1 + 0.15 * (flow / capacity)^4) + signal_delay`

- Each driver class has a population share, a congestion-risk perception,
  acceptable detour tolerance, and route-revision rate. Drivers with detour
  tolerance can split across up to three near-shortest routes using a logit
  choice. Repeated successive averages update assigned flows toward the routes
  drivers currently prefer.
- The default UI run assigns 65% of demand to steady commuters (no detours,
  low congestion aversion) and 35% to flexible drivers (12% detour tolerance,
  0.35 congestion aversion, and 0.65 route-revision rate). The sidebar controls
  let you change these settings. A route-revision rate changes how quickly the
  assignment iterations adjust flows; this is a static assignment model, not
  minute-by-minute traffic or GPS navigation.
- The baseline route is the currently lowest perceived-cost path in the loaded
  directed network. Link travel costs reflect free-flow time, modeled BPR
  congestion, and any signal delay. Flexible alternatives are limited by the
  maximum path count and detour tolerance. To change where trips start/end,
  upload an OD demand CSV; the generated default demand is illustrative and
  based on network positions, not observed traffic.
- The reported relative gap is the remaining class-weighted perceived-cost
  improvement versus shortest paths. The flexible-driver model is a
  bounded-rational stochastic user-equilibrium approximation. It is not
  presented as an exact deterministic Wardrop equilibrium when classes use
  different perceptions or accept detours.
- The volume-weighted P95 is calculated over shortest perceived-cost paths for
  the OD demand set; it is a modeled OD percentile, not a field-observed
  vehicle-level travel-time distribution. Total delay is the sum of link-level
  vehicle-minutes above free-flow time.

The default network is an explicitly labeled, synthetic teaching grid; it does
not imply that demo results are observations from a real city. For city-specific
exploration, import a GraphML drive network from OpenStreetMap. The default OD
matrix selects representative west/east and south/north trips from the largest
strongly connected component; replace it with local counts or travel surveys
before using results for planning decisions.

## Validation and reproducibility

The test suite checks the analytic BPR values at zero, one-times, and two-times
capacity; verifies that demand diverts to a parallel route as congestion grows;
checks that heterogeneity changes assigned flows; and checks infrastructure
scenario effects and GraphML-style attribute normalization.

```bash
pytest
ruff check src tests scripts app.py
rushhour compare --output results/demo-comparison.json
```

Each run is deterministic for a given graph, OD table, driver mix, and iteration
count. The solver vectorizes BPR link-cost updates with NumPy, uses NetworkX
single-source shortest paths for the common single-route case, and only
enumerates alternative simple paths for classes that allow detours. Runtime
depends on network size, OD count, number of driver classes, and alternative
paths; benchmark a representative imported graph before describing a scale
limit.

To profile the assignment kernel without import/startup overhead:

```bash
python -m scripts.profile_simulation
```

In a local cProfile run on the 25-node, 80-link demo with 30 OD pairs and two
driver classes, restricting detour generation to demanded destinations reduced
the profiled kernel from 3.31 s to 0.84 s. This is a machine- and
Python-version-specific profile result, not a throughput guarantee.

The checked-in comparison below is generated from the synthetic demo grid at
80 assignment iterations. In this example, the modeled mean trip is **5.41
minutes** at baseline and **6.66 minutes** after closing the marked arterial
(about **23% longer**); adding a lane lowers the mean to **5.21 minutes**.
These are reproducible model outputs, not measured city data.

![RushHour modeled scenario comparison](assets/scenario-comparison.png)

## Deploy

### Security before publishing

The dashboard does not require API keys and currently has no user accounts,
authentication, or per-user rate limits. For a public or organization-wide
deployment, configure access control, TLS, and request/rate limits in the
hosting platform or a trusted reverse proxy. Do not treat the app's input limits
as a substitute for deployment-level access controls.

The Streamlit configuration enables CORS and XSRF protections and limits each
upload/message to 50 MB. GIS ZIP files are additionally limited to 200 MB when
expanded, and remote GIS downloads are restricted to approved HTTPS ArcGIS
Online and NYC.gov hosts (including redirects). Review those limits against
your host's memory and disk capacity before increasing them.

Never commit credentials. Local `.env` files and `.streamlit/secrets.toml` are
ignored by Git and Docker; put deployment credentials in the hosting provider's
secret manager. If a key has ever been committed, revoke and rotate it: adding
it to `.gitignore` does not remove it from repository history.

### Docker

```bash
docker build -t rushhour .
docker run --rm -p 8501:8501 rushhour
```

The image runs Streamlit as a non-root user and includes a health check. Deploy
the image to a container host that supports HTTP on port `8501`; terminate TLS
and apply any required authentication and rate limits at the host or proxy.

### Streamlit Community Cloud

Push the project to a GitHub repository, select `app.py` as the app entry point,
and deploy with Python 3.12. `requirements.txt` lists the dashboard runtime
dependencies. OSM downloads are intentionally not automatic in the hosted app;
download and upload a GraphML region separately.

### CI

GitHub Actions installs the package, runs Ruff, and executes tests on Python
3.10 and 3.12 for pushes and pull requests.

## Project structure

```text
app.py                  Streamlit screens, state, import controls, orchestration, and downloads
src/rushhour/
  assignment.py         Driver classes, BPR costs, iterative route assignment
  metrics.py            Travel-time, delay, convergence, and congestion metrics
  networks.py           Demo graph, demand, GraphML normalization, optional OSMnx
  gis.py                GeoJSON/ZIP/ArcGIS import, validation, and network building
  scenarios.py          Road closures and infrastructure interventions
  tracing.py            PDF rendering and manually traced graph creation
  visualization.py      Network and congestion figures
  catalog.py            Local bundled-network discovery
  canvas_compat.py      Streamlit canvas compatibility
  cli.py                Command-line simulation and OSM tools
tests/                  Model, GIS, scenario, tracing, and UI-support regressions
data/networks/           Bundled example street-centerline datasets
assets/                  Static images used by the documentation
.streamlit/config.toml   Dashboard theme, browser, upload, and request settings
Dockerfile               Container build and non-root runtime
.github/workflows/ci.yml Lint and test workflow
```

### Where the logic and data handling happen

`app.py` is the web application's entry point and is where most dashboard
layout and interaction handling lives. It builds the sidebar and tabs, reads
uploads and settings, stores the current user's working selections/results in
Streamlit session state, and calls the reusable package modules. The app uses
Streamlit's in-memory caches for expensive imports and PDF rendering; it does
not save uploaded datasets or simulation results as persistent server records.
Users explicitly download exported GraphML, CSV, or GeoJSON files.

The simulation engine is separate from the page: `assignment.py` normalizes
the graph and repeatedly assigns origin-destination demand to routes;
`scenarios.py` makes independent network copies with interventions; and
`metrics.py` calculates the reported outcomes. The network/data path is
`app.py` upload or selection → `networks.py`/`gis.py`/`tracing.py` validation
and graph construction → demand and driver settings → `assignment.py` and
`scenarios.py` → `metrics.py` → `visualization.py` and downloadable outputs.
`cli.py` reuses the same model modules without the web interface, while
`tests/` exercises the model, importers, and regressions.

For web design, the Streamlit widgets and page composition are in `app.py`;
the professional light palette and browser/server defaults are in
`.streamlit/config.toml`. Hosting is not implemented in the Python package:
`Dockerfile` describes one container deployment, and Streamlit Community Cloud
is documented below. The app has no built-in authentication or API-key
management, so public access policy and rate limiting belong to the chosen host
or a reverse proxy.

## Extend the project

For a study, use a locally calibrated road graph and OD matrix, estimate
capacities and free-flow speeds from documented sources, and validate modeled
travel times against independent counts or probe data. Add new interventions
in `scenarios.py`, then add tests comparing the new network attributes and
outcomes. Avoid interpreting an uncalibrated demo scenario as a forecast.
