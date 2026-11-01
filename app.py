"""Interactive traffic simulation, network builder, and scenario comparison."""

from __future__ import annotations

import hashlib
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import streamlit as st

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.canvas_compat import install_image_to_url_compat
from rushhour.gis import (
    discover_local_network_files,
    list_vector_layers,
    load_arcgis_network,
    load_vector_network,
)
from rushhour.networks import (
    OSM_FEATURE_CATEGORIES,
    load_graphml_bytes,
    load_osm_features,
    load_osm_network,
)
from rushhour.scenarios import (
    SCENARIOS,
    apply_scenario,
    demand_critical_corridor,
    demand_critical_edges,
)
from rushhour.tracing import extract_canvas_lines, lines_to_graph, render_pdf_page
from rushhour.visualization import draw_network

install_image_to_url_compat()
from streamlit_drawable_canvas import st_canvas

CANVAS_WIDTH = 1000
CANVAS_HEIGHT = 700


@st.cache_resource(show_spinner=False)
def _load_uploaded_graph(content: bytes) -> nx.DiGraph:
    graph = load_graphml_bytes(content)
    graph.graph["rushhour_source_signature"] = hashlib.sha256(content).hexdigest()
    return graph


@st.cache_resource(show_spinner=False)
def _load_osm_place(place: str) -> nx.DiGraph:
    return load_osm_network(place)


@st.cache_resource(show_spinner=False)
def _load_osm_map_features(place: str, categories: tuple[str, ...]):
    return load_osm_features(place, categories)


@st.cache_data(show_spinner=False)
def _list_uploaded_gis_layers(content: bytes, filename: str) -> tuple[str, ...]:
    return list_vector_layers(content, filename)


@st.cache_resource(show_spinner=False)
def _load_uploaded_gis_network(
    content: bytes,
    filename: str,
    layer: str,
    bbox_wgs84: tuple[float, float, float, float] | None,
    speed_kph: float,
    lanes: int,
    capacity_per_lane_vph: float,
    bidirectional: bool,
    snap_tolerance_m: float,
) -> nx.DiGraph:
    return load_vector_network(
        content,
        filename,
        layer=layer,
        bbox_wgs84=bbox_wgs84,
        speed_kph=speed_kph,
        lanes=lanes,
        capacity_per_lane_vph=capacity_per_lane_vph,
        bidirectional=bidirectional,
        snap_tolerance_m=snap_tolerance_m,
    )


@st.cache_resource(show_spinner=False)
def _load_gis_url_network(
    url: str,
    bbox_wgs84: tuple[float, float, float, float] | None,
    speed_kph: float,
    lanes: int,
    capacity_per_lane_vph: float,
    bidirectional: bool,
    snap_tolerance_m: float,
    where_clause: str,
) -> nx.DiGraph:
    return load_arcgis_network(
        url,
        bbox_wgs84=bbox_wgs84,
        speed_kph=speed_kph,
        lanes=lanes,
        capacity_per_lane_vph=capacity_per_lane_vph,
        bidirectional=bidirectional,
        snap_tolerance_m=snap_tolerance_m,
        where_clause=where_clause or None,
    )


@st.cache_resource(show_spinner=False)
def _load_catalog_network(
    path: str,
    modified_ns: int,
    file_size: int,
    layer: str,
    bbox_wgs84: tuple[float, float, float, float] | None,
    speed_kph: float,
    lanes: int,
    capacity_per_lane_vph: float,
    bidirectional: bool,
    snap_tolerance_m: float,
) -> nx.DiGraph:
    source = Path(path)
    source_stat = source.stat()
    if source_stat.st_mtime_ns != modified_ns or source_stat.st_size != file_size:
        raise RuntimeError(f"Local network changed while loading: {source}")
    content = source.read_bytes()
    if source.suffix.lower() in {".graphml", ".xml"}:
        graph = load_graphml_bytes(content)
        graph.graph["rushhour_source_signature"] = hashlib.sha256(content).hexdigest()
        return graph
    return load_vector_network(
        content,
        source.name,
        layer=layer,
        bbox_wgs84=bbox_wgs84,
        speed_kph=speed_kph,
        lanes=lanes,
        capacity_per_lane_vph=capacity_per_lane_vph,
        bidirectional=bidirectional,
        snap_tolerance_m=snap_tolerance_m,
    )


@st.cache_data(show_spinner=False)
def _render_pdf(content: bytes, page_number: int):
    return render_pdf_page(content, page_number, width=CANVAS_WIDTH, height=CANVAS_HEIGHT)


def _parse_bbox(value: str) -> tuple[float, float, float, float] | None:
    if not value.strip():
        return None
    try:
        coordinates = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as error:
        raise ValueError("Enter the bounding box as west,south,east,north in WGS84.") from error
    if len(coordinates) != 4:
        raise ValueError("Enter the bounding box as west,south,east,north in WGS84.")
    return coordinates


st.set_page_config(
    page_title="RushHour | Urban traffic lab",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.title("RushHour")
st.caption(
    "Trace a network or load a road graph, then explore congestion, driver behavior, "
    "and infrastructure scenarios."
)


def _driver_classes(
    flexible_share: float,
    risk_aversion: float,
    detour_tolerance: float,
    reroute_rate: float,
) -> tuple[DriverClass, DriverClass]:
    return (
        DriverClass("steady commuters", 1.0 - flexible_share, risk_aversion=0.05),
        DriverClass(
            "flexible local drivers",
            flexible_share,
            risk_aversion=risk_aversion,
            detour_tolerance=detour_tolerance,
            reroute_rate=reroute_rate,
        ),
    )


def _run_scenario(
    graph,
    demand,
    driver_classes,
    scenario,
    options,
    progress_callback=None,
    target_edges=None,
    closure_edges=None,
):
    return simulate(
        apply_scenario(
            graph,
            scenario,
            target_edges=target_edges,
            closure_edges=closure_edges,
        ),
        demand,
        driver_classes,
        max_iterations=options["iterations"],
        convergence_threshold=options["convergence"],
        bpr_alpha=options["bpr_alpha"],
        bpr_beta=options["bpr_beta"],
        max_paths=options["max_paths"],
        progress_callback=progress_callback,
    )


def _simulation_progress(progress_bar, start: float = 0.0, end: float = 1.0):
    def update(completed: int, total: int, relative_gap: float) -> None:
        progress_bar.progress(
            start + (end - start) * completed / total,
            text=f"Assignment iteration {completed}/{total} · relative gap {relative_gap:.1%}",
        )

    return update


def _scenario_label(scenario: str, has_tagged_bridges: bool) -> str:
    if has_tagged_bridges or scenario == "baseline":
        return scenario.replace("_", " ").title()
    return {
        "bridge_closure": "Close busiest route corridor",
        "add_lane": "Add lane to busiest route corridor",
        "lower_speed_limit": "Lower speed on busiest route corridor",
        "add_signal": "Add signal on busiest route corridor",
    }[scenario]


def _graphml_bytes(graph: nx.DiGraph) -> bytes:
    return "".join(nx.generate_graphml(graph)).encode("utf-8")


def _run_signature(graph, demand, driver_classes, options, scenario):
    network_signature = graph.graph.get("rushhour_source_signature")
    if network_signature is None:
        network_signature = hashlib.sha256(_graphml_bytes(graph)).hexdigest()
    payload = {
        "network": network_signature,
        "demand": demand,
        "drivers": [driver.__dict__ for driver in driver_classes],
        "options": options,
        "scenario": scenario,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


with st.sidebar:
    st.header("1 · Choose a network")
    graphml_upload = st.file_uploader(
        "Import a road network (GraphML)",
        type=["graphml", "xml"],
        help="GraphML is the recommended ready-to-use network format.",
    )
    with st.form("osm_place_import_form"):
        osm_place = st.text_input(
            "Load roads from OpenStreetMap",
            placeholder="e.g. Cambridge, Massachusetts, USA",
            help=(
                "Downloads and caches drivable roads and mapped road attributes for this area. "
                "Enter a neighborhood or city rather than a large region. This does not include "
                "live traffic counts or trip demand."
            ),
        )
        load_osm_place = st.form_submit_button("Load OpenStreetMap roads")
    try:
        osm_version = version("osmnx")
    except PackageNotFoundError:
        st.warning(
            "OSMnx is not installed in the Python environment running this dashboard. "
            f"Install it there with `{sys.executable} -m pip install -e '.[osm]'`, "
            "then restart Streamlit."
        )
    else:
        st.caption(
            f"OpenStreetMap support ready · OSMnx {osm_version}. Roads and map features both "
            "depend on public Overpass capacity; a 429/502/503/504 response means that upstream "
            "service is busy. Configure RUSHHOUR_OVERPASS_URL to use another compatible HTTPS "
            "Overpass endpoint, then restart Streamlit."
        )
    if load_osm_place:
        try:
            with st.spinner("Finding the place and downloading OpenStreetMap roads..."):
                st.session_state["osm_place_graph"] = _load_osm_place(osm_place)
                st.session_state["osm_place_name"] = osm_place.strip()
                st.session_state["pending_network_choice"] = (
                    f"OpenStreetMap · {osm_place.strip()}"
                )
        except RuntimeError as error:
            st.error(
                f"{error} Active dashboard Python: `{sys.executable}`. "
                f"Install with `{sys.executable} -m pip install -e '.[osm]'` and restart."
            )
        except (ValueError, OSError, nx.NetworkXException) as error:
            st.error(f"Could not load OpenStreetMap roads: {error}")
    if "osm_place_graph" in st.session_state:
        st.caption(
            "OpenStreetMap provides road geometry and some mapped attributes (such as road "
            "direction, speed limit, lanes, and bridge status), when contributors have recorded "
            "them. It does not provide complete traffic counts or an OD demand matrix. "
            "© OpenStreetMap contributors."
        )

    with st.form("osm_features_import_form"):
        st.markdown("**Explore OSM map features (not all are roads)**")
        st.caption(
            "Fetch tagged points, open ways, closed-way polygons, and relation geometries. "
            "Choose a neighborhood or small area to avoid large Overpass queries."
        )
        osm_feature_place = st.text_input(
            "Feature area",
            value=st.session_state.get("osm_place_name", ""),
            placeholder="e.g. Kendall Square, Cambridge, Massachusetts, USA",
        )
        osm_feature_categories = st.multiselect(
            "Feature categories",
            options=tuple(OSM_FEATURE_CATEGORIES),
            default=["Amenities and services", "Buildings"],
        )
        load_osm_feature_button = st.form_submit_button("Load selected OSM features")
    if load_osm_feature_button:
        try:
            with st.spinner("Querying OpenStreetMap features..."):
                features = _load_osm_map_features(
                    osm_feature_place.strip(),
                    tuple(osm_feature_categories),
                )
            st.session_state["osm_features"] = features
            st.session_state["osm_features_place"] = osm_feature_place.strip()
            st.session_state["osm_features_categories"] = tuple(osm_feature_categories)
        except RuntimeError as error:
            st.error(
                f"{error} Active dashboard Python: `{sys.executable}`. "
                f"Install with `{sys.executable} -m pip install -e '.[osm]'` and restart."
            )
        except (ValueError, OSError, nx.NetworkXException) as error:
            st.error(f"Could not load OpenStreetMap features: {error}")
    if "osm_features" in st.session_state:
        features = st.session_state["osm_features"]
        with st.expander(
            f"Loaded OSM features for {st.session_state['osm_features_place']}",
            expanded=False,
        ):
            geometry_counts = features.geometry.geom_type.value_counts()
            element_counts = features.index.get_level_values(0).value_counts()
            st.caption(
                f"{len(features):,} tagged features · geometries: "
                + ", ".join(
                    f"{geometry_type} {count:,}"
                    for geometry_type, count in geometry_counts.items()
                )
                + " · OSM elements: "
                + ", ".join(
                    f"{element_type} {count:,}"
                    for element_type, count in element_counts.items()
                )
            )
            index_frame = features.index.to_frame(index=False).head(50)
            index_frame.columns = [
                name if name is not None else f"osm_id_part_{index}"
                for index, name in enumerate(index_frame.columns)
            ]
            tag_names = sorted(
                {
                    tag
                    for category in st.session_state["osm_features_categories"]
                    for tag in OSM_FEATURE_CATEGORIES[category]
                }
            )
            tag_frame = features.reindex(
                columns=[tag for tag in tag_names if tag in features.columns]
            ).head(50)
            preview = pd.concat(
                [
                    index_frame,
                    features.geometry.geom_type.head(50).rename("geometry_type").reset_index(
                        drop=True
                    ),
                    tag_frame.reset_index(drop=True),
                ],
                axis=1,
            )
            st.dataframe(preview, hide_index=True, width="stretch")
            st.download_button(
                "Download OSM features (GeoJSON)",
                data=features.to_json(),
                file_name="rushhour-osm-features.geojson",
                mime="application/geo+json",
            )
            map_features = (
                features
                if len(features) <= 5_000
                else features.sample(n=5_000, random_state=42)
            ).copy()
            map_features["geometry_type"] = map_features.geometry.geom_type
            feature_figure, feature_axis = plt.subplots(figsize=(8, 5))
            map_features.plot(
                ax=feature_axis,
                column="geometry_type",
                categorical=True,
                legend=True,
                markersize=6,
                linewidth=0.5,
            )
            feature_axis.set_axis_off()
            feature_figure.tight_layout()
            st.pyplot(feature_figure, width="stretch")
            plt.close(feature_figure)
            if len(features) > len(map_features):
                st.caption(
                    f"Map shows a deterministic sample of {len(map_features):,} geometries; "
                    "the GeoJSON download contains all loaded features."
                )
            st.caption(
                "These tagged features preserve their OSM geometry and properties for review; "
                "only the separate drivable-road graph is used for traffic assignment. "
                "Turn-restriction relations are retrieved as features when selected, but are "
                "not currently enforced by route assignment."
            )

    with st.expander("GIS import settings"):
        st.caption(
            "Street-centerline datasets provide geometry, not necessarily traffic speeds or "
            "capacity. These values are defaults for every imported road."
        )
        gis_speed_kph = st.number_input(
            "Default GIS speed (km/h)", min_value=5, max_value=120, value=35
        )
        gis_lanes = st.number_input("Default GIS lanes", min_value=1, max_value=12, value=1)
        gis_capacity_per_lane_vph = st.number_input(
            "Default GIS capacity per lane (vehicles/hour)",
            min_value=100,
            max_value=3000,
            value=900,
            step=50,
            help=(
                "Centerline files often omit capacity. This assumption controls when modeled "
                "congestion rises on imported roads."
            ),
        )
        gis_bidirectional = st.checkbox("Treat imported GIS streets as two-way", value=True)
        gis_snap_tolerance_m = st.number_input(
            "GIS endpoint snap tolerance (m)", min_value=0.0, max_value=50.0, value=2.0
        )
        gis_bbox_text = st.text_input(
            "Optional study-area bounds (west,south,east,north; WGS84)",
            help=(
                "Clip citywide files or limit ArcGIS downloads to a smaller study area. "
                "Leave blank to import all features."
            ),
        )
        gis_where_clause = st.text_input(
            "Optional ArcGIS attribute filter (WHERE clause)",
            placeholder="1=1",
            help=(
                "Example: OBJECTID < 10000. Only used for ArcGIS REST imports; "
                "use the field names available in that service. A smaller selection imports faster."
            ),
        )
    gis_upload = st.file_uploader(
        "Import GIS streets (GeoJSON or ZIP)",
        type=["geojson", "json", "zip"],
        help=(
            "ZIP uploads must contain one complete Shapefile (.shp/.shx/.dbf/.prj) "
            "or one File Geodatabase (.gdb). Install the optional GIS dependencies first."
        ),
        key="gis_network_upload",
    )
    gis_url = ""
    with st.form("gis_url_import_form"):
        gis_url = st.text_input(
            "Public ArcGIS REST layer or GeoJSON URL",
            placeholder="https://services5.arcgis.com/.../FeatureServer/0",
        )
        load_gis_url = st.form_submit_button("Load GIS service")
    if gis_url:
        st.caption(
            "Example: [NYC DCM Street Center Line REST layer]"
            "(https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/arcgis/rest/services/"
            "DCM_Street_Center_Line/FeatureServer/0)"
        )

    try:
        gis_bbox = _parse_bbox(gis_bbox_text)
        gis_bbox_error = None
    except ValueError as error:
        gis_bbox = None
        gis_bbox_error = str(error)
        st.error(str(error))

    demo_graph = build_demo_network()
    demo_graph.graph["rushhour_source_signature"] = "rushhour-demo-network-v1"
    available_networks = {"Demo network": demo_graph}
    if "osm_place_graph" in st.session_state:
        osm_place_name = st.session_state.get("osm_place_name", "OpenStreetMap place")
        available_networks[f"OpenStreetMap · {osm_place_name}"] = st.session_state[
            "osm_place_graph"
        ]
    local_network_dir = Path(__file__).parent / "data" / "networks"
    local_network_files = discover_local_network_files(local_network_dir)
    if local_network_files and gis_bbox_error is None:
        with st.spinner("Loading bundled city networks..."):
            for local_path in local_network_files:
                try:
                    stat_result = local_path.stat()
                    if local_path.suffix.lower() in {".graphml", ".xml"}:
                        local_layers = ("",)
                    else:
                        local_content = local_path.read_bytes()
                        local_layers = _list_uploaded_gis_layers(
                            local_content, local_path.name
                        )
                    for local_layer in local_layers:
                        relative_name = local_path.relative_to(local_network_dir).as_posix()
                        label = f"Local · {relative_name}"
                        if local_layer:
                            label += f" · {local_layer}"
                        available_networks[label] = _load_catalog_network(
                            str(local_path),
                            stat_result.st_mtime_ns,
                            stat_result.st_size,
                            local_layer,
                            gis_bbox,
                            float(gis_speed_kph),
                            int(gis_lanes),
                            float(gis_capacity_per_lane_vph),
                            gis_bidirectional,
                            float(gis_snap_tolerance_m),
                        )
                except (OSError, ValueError, RuntimeError, nx.NetworkXException) as error:
                    relative_name = local_path.relative_to(local_network_dir).as_posix()
                    st.warning(f"Could not load local network {relative_name}: {error}")
        st.caption("Local networks are discovered recursively in `data/networks/`.")
    elif local_network_files:
        st.info("Fix the study-area bounds above to load local GIS networks.")
    if graphml_upload is not None:
        try:
            graphml_name = f"GraphML · {graphml_upload.name}"
            available_networks[graphml_name] = _load_uploaded_graph(
                graphml_upload.getvalue()
            )
        except (ValueError, nx.NetworkXException) as error:
            st.error(f"Could not load GraphML: {error}")
    if gis_upload is not None:
        try:
            if gis_bbox_error is not None:
                raise ValueError(gis_bbox_error)
            gis_content = gis_upload.getvalue()
            with st.spinner("Reading GIS streets and building the road network..."):
                gis_layers = _list_uploaded_gis_layers(gis_content, gis_upload.name)
                gis_layer = (
                    st.selectbox("GIS layer", gis_layers, key="gis_layer_choice")
                    if len(gis_layers) > 1
                    else gis_layers[0]
                )
                gis_name = f"GIS · {gis_upload.name} · {gis_layer}"
                available_networks[gis_name] = _load_uploaded_gis_network(
                    gis_content,
                    gis_upload.name,
                    gis_layer,
                    gis_bbox,
                    float(gis_speed_kph),
                    int(gis_lanes),
                    float(gis_capacity_per_lane_vph),
                    gis_bidirectional,
                    float(gis_snap_tolerance_m),
                )
        except (ValueError, RuntimeError, nx.NetworkXException) as error:
            st.error(f"Could not load GIS dataset: {error}")
    if load_gis_url:
        try:
            parsed_url = _parse_bbox(gis_bbox_text)
            with st.spinner("Downloading and preparing street centerlines..."):
                imported_graph = _load_gis_url_network(
                    gis_url,
                    parsed_url,
                    float(gis_speed_kph),
                    int(gis_lanes),
                    float(gis_capacity_per_lane_vph),
                    gis_bidirectional,
                    float(gis_snap_tolerance_m),
                    gis_where_clause.strip(),
                )
            st.session_state["gis_url_graph"] = imported_graph
            st.session_state["gis_url_name"] = gis_url
        except (ValueError, RuntimeError, nx.NetworkXException) as error:
            st.error(f"Could not load GIS service: {error}")
    if "gis_url_graph" in st.session_state:
        gis_url_name = st.session_state.get("gis_url_name", "ArcGIS service")
        available_networks[f"GIS URL · {gis_url_name}"] = st.session_state["gis_url_graph"]
    if "last_trace_graph" in st.session_state:
        available_networks[
            f"PDF trace · {st.session_state.get('last_trace_name', 'custom map')}"
        ] = st.session_state["last_trace_graph"]
    if st.session_state.get("network_choice") not in available_networks:
        st.session_state["network_choice"] = "Demo network"
    pending_network = st.session_state.pop("pending_network_choice", None)
    if pending_network in available_networks:
        st.session_state["network_choice"] = pending_network
    network_source = st.selectbox(
        "Active network", list(available_networks), key="network_choice"
    )
    network = available_networks[network_source]
    st.caption(f"Active: **{network_source}** · {network.number_of_nodes()} intersections · {network.number_of_edges()} directed links")
    has_tagged_bridges = any(
        data["bridge"] for _, _, data in network.edges(data=True)
    )
    large_network = network.number_of_nodes() >= 5_000
    assignment_detail = 3
    if large_network:
        assignment_detail = st.slider(
            "Assignment detail",
            min_value=1,
            max_value=5,
            value=2,
            help=(
                "Higher settings use more example trips, more assignment iterations, and "
                "alternative routes for flexible drivers. They can take substantially longer."
            ),
        )
        detail_settings = {
            1: (2, 2, 1),
            2: (3, 3, 1),
            3: (5, 5, 1),
            4: (7, 8, 2),
            5: (10, 12, 3),
        }
        example_demand_samples, assignment_iterations, assignment_paths = (
            detail_settings[assignment_detail]
        )
        detail_name = ("Quick", "Light", "Standard", "Detailed", "Thorough")[
            assignment_detail - 1
        ]
        st.caption(
            f"{detail_name} · up to {assignment_iterations} iterations, "
            f"{assignment_paths} route(s) per trip, and sample trips from up to "
            f"{example_demand_samples} locations along each map edge. Higher detail can take "
            "much longer."
        )
    else:
        example_demand_samples = 5

    st.header("2 · Set driver behavior")
    flexible_share = st.slider(
        "Flexible-driver share", 0.0, 1.0, 0.35, 0.05,
        help="Share of drivers willing to consider alternative routes.",
    )
    with st.expander("Advanced route-choice and congestion settings"):
        risk_aversion = st.slider("Flexible-driver congestion aversion", 0.0, 1.0, 0.35, 0.05)
        detour_tolerance = st.slider(
            "Maximum accepted detour",
            0.0,
            0.5,
            0.12,
            0.02,
            help=(
                "Flexible drivers may use routes up to this much longer than the shortest "
                "route. On large networks, alternative routes are used at assignment detail 4 or 5."
            ),
        )
        reroute_rate = st.slider("Route revision rate", 0.1, 1.0, 0.65, 0.05)
        if not large_network:
            iterations = st.slider("Assignment iterations", 20, 200, 80, 10)
        convergence = st.slider("Convergence threshold (%)", 0.1, 2.0, 0.5, 0.1) / 100
        bpr_alpha = st.slider("BPR alpha", 0.05, 0.5, 0.15, 0.05)
        bpr_beta = st.slider("BPR beta", 2.0, 6.0, 4.0, 0.5)
        if not large_network:
            max_paths = st.slider("Flexible-driver route alternatives", 2, 5, 3)
        map_color_scale = st.slider(
            "Map turns red at v/c",
            0.25,
            3.0,
            1.0,
            0.25,
            help="Changes the congestion color scale only; it does not change the simulation.",
        )

options = {
    "iterations": (
        assignment_iterations if large_network else iterations
    ),
    "convergence": convergence,
    "bpr_alpha": bpr_alpha,
    "bpr_beta": bpr_beta,
    "max_paths": assignment_paths if large_network else max_paths,
    "large_network_fast_mode": large_network,
}
driver_classes = _driver_classes(
    flexible_share, risk_aversion, detour_tolerance, reroute_rate
)

explore_tab, build_tab, guide_tab = st.tabs(
    ["Simulate & compare", "Build from a PDF map", "Guide"]
)

with build_tab:
    st.subheader("Trace a road network from a map")
    if st.session_state.pop("trace_created_notice", False):
        st.success("PDF road network created and selected as the active network.")
    st.write(
        "Upload a PDF map, choose its page, and draw road centerlines with the line tool. "
        "RushHour splits crossings into intersections and builds a network you can simulate."
    )
    pdf_upload = st.file_uploader("Upload a map PDF", type=["pdf"], key="road_pdf")
    if pdf_upload is None:
        st.info("Tip: a high-resolution map with a visible scale bar is easiest to trace.")
    else:
        pdf_content = pdf_upload.getvalue()
        try:
            first_page, page_count = _render_pdf(pdf_content, 0)
            pdf_page = st.number_input(
                "PDF page", min_value=1, max_value=page_count, value=1, step=1
            )
            map_image, _ = (
                (first_page, page_count)
                if pdf_page == 1
                else _render_pdf(pdf_content, int(pdf_page) - 1)
            )
            scale_column, speed_column, lane_column, snap_column = st.columns(4)
            map_width_km = scale_column.number_input(
                "Distance across full canvas (km)",
                min_value=0.01,
                value=5.0,
                step=0.1,
                help=(
                    "Use the map scale bar or a known distance to estimate how many "
                    "kilometers the full 1000-pixel canvas width represents."
                ),
            )
            default_speed = speed_column.number_input(
                "Default speed (km/h)", min_value=5, max_value=120, value=35
            )
            default_lanes = lane_column.number_input(
                "Default lanes", min_value=1, max_value=12, value=1
            )
            snap_tolerance = snap_column.slider(
                "Intersection snap tolerance (px)", 0, 20, 8
            )
            bidirectional = st.checkbox(
                "Treat traced roads as two-way",
                value=True,
                help="If unchecked, road direction follows the direction in which you draw each line.",
            )
            upload_key = hashlib.sha256(pdf_content).hexdigest()[:10]
            canvas_key = f"road-canvas-{upload_key}-{pdf_page}"
            canvas = st_canvas(
                fill_color="rgba(0, 0, 0, 0)",
                stroke_width=3,
                stroke_color="#2563eb",
                background_image=map_image,
                update_streamlit=True,
                height=CANVAS_HEIGHT,
                width=CANVAS_WIDTH,
                drawing_mode="line",
                key=canvas_key,
            )
            create_column, clear_column = st.columns([1, 4])
            with create_column:
                create_network = st.button(
                    "Create network from trace", type="primary", width="stretch"
                )
            with clear_column:
                st.caption(
                    "Draw each road segment once. Connect roads at intersections; use the toolbar "
                    "to undo or clear a line. One-way direction follows the line you draw."
                )
            if create_network:
                lines = extract_canvas_lines(canvas.json_data)
                try:
                    traced_graph = lines_to_graph(
                        lines,
                        canvas_width=CANVAS_WIDTH,
                        scale_km_per_pixel=map_width_km / CANVAS_WIDTH,
                        speed_kph=float(default_speed),
                        lanes=int(default_lanes),
                        bidirectional=bidirectional,
                        snap_tolerance_px=float(snap_tolerance),
                        name=f"{pdf_upload.name} · page {pdf_page}",
                    )
                    traced_graph.graph["rushhour_source_signature"] = hashlib.sha256(
                        _graphml_bytes(traced_graph)
                    ).hexdigest()
                    st.session_state["last_trace_graph"] = traced_graph
                    st.session_state["last_trace_name"] = f"{pdf_upload.name} · page {pdf_page}"
                    st.session_state["pending_network_choice"] = (
                        f"PDF trace · {st.session_state['last_trace_name']}"
                    )
                    st.session_state["trace_created_notice"] = True
                    st.rerun()
                except ValueError as error:
                    st.error(f"Could not create the network: {error}")

            if "last_trace_graph" in st.session_state:
                traced_graph = st.session_state["last_trace_graph"]
                preview = draw_network(traced_graph)
                st.pyplot(preview, width="stretch")
                plt.close(preview)
                st.download_button(
                    "Download traced network (GraphML)",
                    data=_graphml_bytes(traced_graph),
                    file_name="rushhour-traced-network.graphml",
                    mime="application/graphml+xml",
                )
        except (ValueError, RuntimeError) as error:
            st.error(f"Could not read this PDF: {error}")

with explore_tab:
    st.subheader("Explore an intervention")
    demand_column, scenario_column = st.columns([1, 2])
    demand_scale = demand_column.slider(
        "Peak-hour demand scale", 0.5, 2.0, 1.0, 0.1
    )
    scenario = scenario_column.selectbox(
        "Scenario to run",
        SCENARIOS,
        format_func=lambda value: _scenario_label(value, has_tagged_bridges),
    )
    try:
        demand_template = default_demand(
            network,
            scale=demand_scale,
            max_samples=example_demand_samples,
        )
    except ValueError as error:
        demand_template = []
        st.error(f"Could not create example demand for this network: {error}")
    template_csv = pd.DataFrame(
        demand_template, columns=["origin", "destination", "volume_vph"]
    ).to_csv(index=False).encode("utf-8")
    with st.expander("What are trips and an OD demand CSV?"):
        st.markdown(
            "**OD** means **origin to destination**. One OD pair describes travel from one "
            "network intersection to another. Its `volume_vph` is the number of vehicles per "
            "hour making that trip, not the travel time and not a single vehicle."
        )
        st.markdown(
            "The built-in trips are illustrative peak-hour samples between points near the "
            "edges of the selected network. They are generated from the map geometry; they are "
            "not official traffic counts or a standard real-world commute dataset. Changing "
            "the Assignment detail slider creates more sample trips."
        )
        st.download_button(
            "Download editable example trips (CSV)",
            data=template_csv,
            file_name="rushhour-demand-template.csv",
        )
        st.markdown(
            "To use your own trips, edit the downloaded CSV and upload it below. Keep the "
            "columns `origin`, `destination`, and `volume_vph`; use intersection IDs from the "
            "selected network. The downloaded file starts with valid intersection IDs, which "
            "you can replace with your own. A typical row looks like "
            "`gis12,gis845,250` (250 vehicles per hour from intersection `gis12` to `gis845`)."
        )
        demand_upload = st.file_uploader(
            "Upload your trips (CSV)",
            type=["csv"],
            key="demand_csv",
        )
        demand = demand_template
        if demand_upload is not None:
            try:
                demand_frame = pd.read_csv(
                    BytesIO(demand_upload.getvalue()),
                    dtype={"origin": str, "destination": str},
                    keep_default_na=False,
                )
                required_columns = {"origin", "destination", "volume_vph"}
                if not required_columns.issubset(demand_frame.columns):
                    missing = ", ".join(sorted(required_columns - set(demand_frame.columns)))
                    raise ValueError(f"Demand CSV is missing required columns: {missing}.")
                if demand_frame.empty:
                    raise ValueError("Demand CSV must contain at least one trip pair.")
                demand_frame["volume_vph"] = pd.to_numeric(
                    demand_frame["volume_vph"], errors="raise"
                )
                if (
                    demand_frame[["origin", "destination"]].apply(
                        lambda column: column.str.strip().eq("").any()
                    ).any()
                    or not np.isfinite(demand_frame["volume_vph"]).all()
                    or (demand_frame["volume_vph"] <= 0).any()
                ):
                    raise ValueError(
                        "Origins and destinations must be named and all hourly volumes must be positive."
                    )
                demand = list(
                    demand_frame[["origin", "destination", "volume_vph"]]
                    .itertuples(index=False, name=None)
                )
                st.success(f"Loaded {len(demand)} origin–destination pairs.")
            except (ValueError, pd.errors.ParserError) as error:
                st.error(f"Could not read demand CSV: {error}")
                demand = []

    if demand:
        st.caption(
            f"Current demand has {len(demand)} origin–destination pairs. "
            "Only roads carrying these trips are highlighted; other imported streets remain gray."
        )

    if not has_tagged_bridges:
        st.caption(
            "This network has no tagged bridge links. Lane, speed, and signal scenarios target "
            "a busy end-to-end route used by the current demand; a separate closure target is "
            "chosen to keep all OD pairs connected."
        )
    run_column, compare_column = st.columns(2)
    run_selected = run_column.button(
        "Run selected scenario", type="primary", width="stretch"
    )
    compare_all = compare_column.button(
        "Compare all scenarios", width="stretch"
    )
    if not demand:
        st.warning("Provide a valid demand table before running the simulation.")
    elif run_selected:
        assignment_progress = st.progress(0.0, text="Starting traffic assignment...")
        with st.spinner("Assigning traffic to routes and updating congestion..."):
            try:
                scenario_targets = None
                closure_targets = None
                progress_callback = _simulation_progress(assignment_progress)
                if scenario != "baseline" and not has_tagged_bridges:
                    baseline_result = _run_scenario(
                        network,
                        demand,
                        driver_classes,
                        "baseline",
                        options,
                        progress_callback=_simulation_progress(
                            assignment_progress, 0.0, 0.5
                        ),
                    )
                    closure_targets = demand_critical_edges(
                        network,
                        demand,
                        baseline_result.edge_flows,
                        baseline_result.edge_travel_times,
                        sum(
                            driver.share * driver.risk_aversion
                            for driver in driver_classes
                        ),
                    )
                    scenario_targets = demand_critical_corridor(
                        network,
                        demand,
                        baseline_result.edge_flows,
                        baseline_result.edge_travel_times,
                        sum(
                            driver.share * driver.risk_aversion
                            for driver in driver_classes
                        ),
                    )
                    progress_callback = _simulation_progress(
                        assignment_progress, 0.5, 1.0
                    )
                result = _run_scenario(
                    network,
                    demand,
                    driver_classes,
                    scenario,
                    options,
                    progress_callback=progress_callback,
                    target_edges=scenario_targets,
                    closure_edges=closure_targets,
                )
                assignment_progress.empty()
                st.session_state["simulation_result"] = (scenario, result)
                st.session_state["simulation_scenario_targets"] = scenario_targets
                st.session_state["simulation_closure_targets"] = closure_targets
                st.session_state["simulation_signature"] = _run_signature(
                    network, demand, driver_classes, options, scenario
                )
            except (ValueError, nx.NetworkXException) as error:
                assignment_progress.empty()
                st.error(f"Simulation failed: {error}")
    elif compare_all:
        comparison = {}
        try:
            progress = st.progress(0, text="Comparing intervention scenarios...")
            with st.spinner("Running baseline and intervention assignments..."):
                scenario_targets = None
                closure_targets = None
                candidates = SCENARIOS
                if not has_tagged_bridges:
                    baseline = _run_scenario(network, demand, driver_classes, "baseline", options)
                    comparison["baseline"] = baseline
                    closure_targets = demand_critical_edges(
                        network,
                        demand,
                        baseline.edge_flows,
                        baseline.edge_travel_times,
                        sum(
                            driver.share * driver.risk_aversion
                            for driver in driver_classes
                        ),
                    )
                    scenario_targets = demand_critical_corridor(
                        network,
                        demand,
                        baseline.edge_flows,
                        baseline.edge_travel_times,
                        sum(
                            driver.share * driver.risk_aversion
                            for driver in driver_classes
                        ),
                    )
                    candidates = SCENARIOS[1:]
                    progress.progress(1 / len(SCENARIOS), text="Completed baseline")
                for index, candidate in enumerate(
                    candidates,
                    start=0 if has_tagged_bridges else 1,
                ):
                    comparison[candidate] = _run_scenario(
                        network,
                        demand,
                        driver_classes,
                        candidate,
                        options,
                        target_edges=scenario_targets,
                        closure_edges=closure_targets,
                    )
                    progress.progress(
                        (index + 1) / len(SCENARIOS),
                        text=f"Completed {candidate.replace('_', ' ')}",
                    )
            st.session_state["scenario_comparison"] = comparison
            st.session_state["comparison_target_edges"] = scenario_targets
            st.session_state["comparison_closure_edges"] = closure_targets
            st.session_state["comparison_signature"] = _run_signature(
                network, demand, driver_classes, options, "all-scenarios"
            )
        except (ValueError, nx.NetworkXException) as error:
            st.error(f"Scenario comparison failed: {error}")

    simulation_signature = _run_signature(
        network, demand, driver_classes, options, scenario
    ) if demand else ""
    if (
        "simulation_result" in st.session_state
        and st.session_state.get("simulation_signature") == simulation_signature
    ):
        result_scenario, result = st.session_state["simulation_result"]
        metrics = result.metrics
        st.markdown(
            f"#### {_scenario_label(result_scenario, has_tagged_bridges)} results"
        )
        metric_columns = st.columns(5)
        metric_columns[0].metric("Mean trip time", f"{metrics.average_travel_time_min:.1f} min")
        metric_columns[1].metric("P95 trip time", f"{metrics.p95_travel_time_min:.1f} min")
        metric_columns[2].metric("Total delay", f"{metrics.total_delay_vehicle_min:,.0f} veh-min")
        metric_columns[3].metric("Relative gap", f"{result.relative_gap:.2%}")
        metric_columns[4].metric("Vehicles / hour", f"{result.total_demand:,.0f}")
        map_column, edge_column = st.columns([1.4, 1])
        with map_column:
            st.markdown("##### Network congestion")
            figure = draw_network(
                apply_scenario(
                    network,
                    result_scenario,
                    target_edges=st.session_state.get("simulation_scenario_targets"),
                    closure_edges=st.session_state.get("simulation_closure_targets"),
                ),
                result,
                color_scale_max=map_color_scale,
            )
            st.pyplot(figure, width="stretch")
            active_links = sum(flow > 0 for flow in result.edge_flows.values())
            st.caption(
                f"Traffic assigned to {active_links:,} of {network.number_of_edges():,} "
                f"directed links ({active_links / max(1, network.number_of_edges()):.1%}); "
                "gray links are imported but unused by the current demand."
            )
            plt.close(figure)
        with edge_column:
            st.markdown("##### Most congested links")
            edge_table = pd.DataFrame(
                [edge.__dict__ for edge in metrics.congested_edges]
            ).rename(
                columns={
                    "origin": "From",
                    "destination": "To",
                    "flow_vph": "Flow (veh/h)",
                    "capacity_vph": "Capacity (veh/h)",
                    "volume_capacity_ratio": "v/c",
                    "travel_time_min": "Time (min)",
                }
            )
            st.dataframe(edge_table, hide_index=True, width="stretch")

    comparison = st.session_state.get("scenario_comparison")
    comparison_signature = _run_signature(
        network, demand, driver_classes, options, "all-scenarios"
    ) if demand else ""
    if comparison and st.session_state.get("comparison_signature") == comparison_signature:
        st.markdown("#### Scenario comparison")
        baseline_mean = comparison["baseline"].metrics.average_travel_time_min
        comparison_rows = []
        for key, item in comparison.items():
            item_metrics = item.metrics
            comparison_rows.append(
                {
                    "Scenario": _scenario_label(key, has_tagged_bridges),
                    "Mean (min)": item_metrics.average_travel_time_min,
                    "P95 (min)": item_metrics.p95_travel_time_min,
                    "Delay (veh-min)": item_metrics.total_delay_vehicle_min,
                    "Mean change vs baseline": (
                        item_metrics.average_travel_time_min / baseline_mean - 1
                    )
                    * 100,
                    "Gap": item.relative_gap,
                }
            )
        comparison_frame = pd.DataFrame(comparison_rows)
        st.dataframe(
            comparison_frame.style.format(
                {
                    "Mean (min)": "{:.2f}",
                    "P95 (min)": "{:.2f}",
                    "Delay (veh-min)": "{:,.0f}",
                    "Mean change vs baseline": "{:+.1f}%",
                    "Gap": "{:.2%}",
                }
            ),
            hide_index=True,
            width="stretch",
        )
        chart_column, network_column = st.columns([1.4, 1])
        with chart_column:
            chart_data = comparison_frame.set_index("Scenario")[
                ["Mean (min)", "P95 (min)"]
            ]
            st.bar_chart(chart_data, y_label="Minutes")
        with network_column:
            selected_map_scenario = st.selectbox(
                "Show congestion map for",
                list(comparison),
                format_func=lambda value: _scenario_label(value, has_tagged_bridges),
                key="comparison-map-select",
            )
            selected_map = draw_network(
                apply_scenario(
                    network,
                    selected_map_scenario,
                    target_edges=st.session_state.get("comparison_target_edges"),
                    closure_edges=st.session_state.get("comparison_closure_edges"),
                ),
                comparison[selected_map_scenario],
                color_scale_max=map_color_scale,
            )
            st.pyplot(selected_map, width="stretch")
            plt.close(selected_map)
        comparison_csv = comparison_frame.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download comparison results (CSV)",
            data=comparison_csv,
            file_name="rushhour-scenario-comparison.csv",
            mime="text/csv",
        )

with guide_tab:
    st.subheader("A clear, repeatable workflow")
    st.markdown(
        """
        1. **Choose a road network.** Start with the demo, import GraphML or GIS centerlines,
           or create a network from a PDF map in the **Build from a PDF map** tab.
        2. **Calibrate and trace.** Use a visible scale bar to set the displayed map width in
           kilometers, then draw roads with the line tool. Crossings become intersections.
        3. **Set demand and driver mix.** Use the default illustrative demand or upload your own
           CSV (`origin,destination,volume_vph`). Adjust flexibility, risk, detour, and rerouting.
        4. **Run or compare.** Inspect the congestion heatmap, mean and P95 travel time, total
           delay, convergence gap, and most congested links.
        5. **Export and share.** Download a traced GraphML network or a CSV of the scenario comparison.
        """
    )
    st.info(
        "PDF input is a visual background for manual tracing, not automatic road recognition. "
        "Tracing does not infer one-way streets, road classes, lane counts, or measured traffic; "
        "review and calibrate those assumptions before interpreting results."
    )
    st.markdown(
        """
        **Recommended formats:** GraphML is ready to simulate and retains network attributes.
        GeoJSON, zipped Shapefiles, zipped File Geodatabases, and public ArcGIS REST services can
        be converted from street-centerline geometry. GIS imports need default speed, lane, and
        direction assumptions; install `.[gis]` for those formats. A PDF works well as a tracing
        background when it has a usable scale bar. For automatic and accurate road topology, use
        OpenStreetMap/OSMnx or a GIS-exported road graph rather than a scanned PDF.
        """
    )
    st.markdown(
        """
        **How route choice works:** RushHour first takes the roads and directions in the selected
        network, then assigns the selected origin–destination demand to available paths. The
        shortest path is based on modeled link travel time, not live navigation or current traffic.
        Congestion updates link time with the BPR curve. Steady drivers use the shortest perceived-
        cost route; flexible drivers may split across up to the configured number of near-shortest
        paths. Their perceived cost increases with congestion aversion, and their detour tolerance
        sets how much longer an alternative can be than the shortest perceived-cost path.

        Change **Flexible-driver share**, **Flexible-driver congestion aversion**, **Maximum accepted
        detour**, and **Route revision rate** in the sidebar under **Set driver behavior**. The
        advanced settings also include assignment iterations and BPR alpha/beta. On large networks,
        **Assignment detail** changes the sample-trip count, iteration limit, and available paths.
        A higher route revision rate makes the iterative assignment adjust more quickly; it does not
        simulate minute-by-minute traffic or individual GPS rerouting. For realistic travel demand,
        upload your own `origin,destination,volume_vph` CSV instead of relying on the illustrative
        default trips.
        """
    )
    st.markdown(
        "Travel times use the BPR curve "
        "`t = t0 * (1 + alpha * (flow / capacity)^beta) + signal delay`. "
        "The reported assignment gap measures remaining perceived-cost improvement; "
        "driver detours make this a bounded-rational equilibrium approximation."
    )
