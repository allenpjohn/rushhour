"""PDF rendering and road-network reconstruction from hand-traced map lines."""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

import networkx as nx
from PIL import Image, ImageOps

from rushhour.networks import add_road_pair


def render_pdf_page(
    content: bytes, page_number: int, *, width: int = 1000, height: int = 700
) -> tuple[Image.Image, int]:
    """Render a PDF page into a fixed-size, aspect-preserving canvas background."""
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError(
            "PDF tracing requires PyMuPDF; install the project dependencies to enable it."
        ) from error
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, ValueError) as error:
        raise ValueError("This file is not a readable PDF.") from error
    with document:
        if document.page_count == 0:
            raise ValueError("The uploaded PDF has no pages.")
        if page_number < 0 or page_number >= document.page_count:
            raise ValueError(f"PDF page must be between 1 and {document.page_count}.")
        page = document.load_page(page_number)
        page_scale = min(width / page.rect.width, height / page.rect.height)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(page_scale, page_scale), alpha=False)
        page_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        background = Image.new("RGB", (width, height), "white")
        fitted = ImageOps.contain(page_image, (width, height), method=Image.Resampling.LANCZOS)
        background.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        return background, document.page_count


def extract_canvas_lines(canvas_json: dict | None) -> list[tuple[float, float, float, float]]:
    """Extract line endpoints from Fabric.js canvas objects."""
    if not canvas_json:
        return []
    lines = []
    for item in canvas_json.get("objects", []):
        if item.get("type") != "line":
            continue
        x1, y1, x2, y2 = _line_coordinates(item)
        if all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            lines.append((x1, y1, x2, y2))
    return lines


def lines_to_graph(
    lines: Sequence[tuple[float, float, float, float]],
    *,
    canvas_width: int,
    scale_km_per_pixel: float,
    speed_kph: float = 35.0,
    lanes: int = 1,
    bidirectional: bool = True,
    snap_tolerance_px: float = 8.0,
    name: str = "PDF traced road network",
) -> nx.DiGraph:
    """Split hand-drawn road segments at intersections and build a directed graph."""
    if canvas_width <= 0 or not math.isfinite(scale_km_per_pixel) or scale_km_per_pixel <= 0:
        raise ValueError("Canvas width and map scale must be greater than zero.")
    if not math.isfinite(speed_kph) or speed_kph <= 0 or lanes < 1:
        raise ValueError("Road speed and lane count must be positive.")
    if snap_tolerance_px < 0:
        raise ValueError("Snap tolerance cannot be negative.")

    segments = []
    for line in lines:
        points = tuple(float(value) for value in line)
        if len(points) != 4 or not all(math.isfinite(value) for value in points):
            raise ValueError("Each traced road must have four finite canvas coordinates.")
        first, second = (points[0], points[1]), (points[2], points[3])
        if math.dist(first, second) >= max(2.0, snap_tolerance_px):
            segments.append((first, second))
    if not segments:
        raise ValueError("Trace at least one road segment on the map before creating a network.")

    split_points: list[list[tuple[float, float, float]]] = [
        [(0.0, *first), (1.0, *second)] for first, second in segments
    ]
    for first_index, (first_start, first_end) in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            second_start, second_end = segments[second_index]
            crossing = _segment_intersection(first_start, first_end, second_start, second_end)
            if crossing is not None:
                first_t, second_t, point = crossing
                split_points[first_index].append((first_t, *point))
                split_points[second_index].append((second_t, *point))
            for endpoint in (first_start, first_end):
                projection = _project_onto_segment(endpoint, second_start, second_end)
                if (
                    projection is not None
                    and math.dist(endpoint, projection[1:]) <= snap_tolerance_px
                ):
                    split_points[second_index].append(projection)
            for endpoint in (second_start, second_end):
                projection = _project_onto_segment(endpoint, first_start, first_end)
                if (
                    projection is not None
                    and math.dist(endpoint, projection[1:]) <= snap_tolerance_px
                ):
                    split_points[first_index].append(projection)

    node_locations: list[tuple[float, float]] = []
    segment_nodes: list[list[int]] = []
    for points in split_points:
        ordered = sorted(points, key=lambda point: point[0])
        ids = []
        for _, x, y in ordered:
            ids.append(_snap_node((x, y), node_locations, snap_tolerance_px))
        segment_nodes.append(ids)

    graph = nx.DiGraph(name=name)
    graph.graph.update(
        source_format="manual_pdf_trace",
        canvas_width_px=int(canvas_width),
        scale_km_per_pixel=float(scale_km_per_pixel),
    )
    for index, (x, y) in enumerate(node_locations):
        graph.add_node(
            f"n{index}",
            x=x * scale_km_per_pixel,
            y=-y * scale_km_per_pixel,
            canvas_x_px=x,
            canvas_y_px=y,
        )

    added_edges: set[tuple[int, int] | frozenset[int]] = set()
    for node_ids in segment_nodes:
        for start_id, end_id in pairwise(node_ids):
            if start_id == end_id:
                continue
            edge_id = frozenset((start_id, end_id)) if bidirectional else (start_id, end_id)
            if edge_id in added_edges:
                continue
            start = node_locations[start_id]
            end = node_locations[end_id]
            length_km = math.dist(start, end) * scale_km_per_pixel
            if length_km <= 0:
                continue
            source, target = f"n{start_id}", f"n{end_id}"
            if bidirectional:
                add_road_pair(
                    graph,
                    source,
                    target,
                    length_km=length_km,
                    speed_kph=speed_kph,
                    lanes=lanes,
                )
            else:
                graph.add_edge(
                    source,
                    target,
                    length_km=length_km,
                    speed_kph=speed_kph,
                    lanes=lanes,
                    capacity_vph=float(lanes * 900),
                    free_flow_min=length_km / speed_kph * 60.0,
                    signal_delay_min=0.0,
                    bridge=False,
                )
            added_edges.add(edge_id)
    if graph.number_of_edges() == 0:
        raise ValueError("No usable roads were found in the drawing.")
    return graph


def _line_coordinates(item: dict) -> tuple[float, float, float, float]:
    if all(key in item for key in ("x1", "y1", "x2", "y2")):
        left, top = float(item.get("left", 0)), float(item.get("top", 0))
        scale_x, scale_y = float(item.get("scaleX", 1)), float(item.get("scaleY", 1))
        x1, y1 = float(item["x1"]), float(item["y1"])
        x2, y2 = float(item["x2"]), float(item["y2"])
        width = float(item.get("width", abs(x2 - x1))) * abs(scale_x)
        height = float(item.get("height", abs(y2 - y1))) * abs(scale_y)
        origin_x = item.get("originX", "left")
        origin_y = item.get("originY", "top")
        center_x = left + width * (0.5 - _origin_fraction(origin_x, "left"))
        center_y = top + height * (0.5 - _origin_fraction(origin_y, "top"))
        return (
            center_x + x1 * scale_x,
            center_y + y1 * scale_y,
            center_x + x2 * scale_x,
            center_y + y2 * scale_y,
        )
    left, top = float(item.get("left", 0)), float(item.get("top", 0))
    return (
        left,
        top,
        left + float(item.get("width", 0)) * float(item.get("scaleX", 1)),
        top + float(item.get("height", 0)) * float(item.get("scaleY", 1)),
    )


def _origin_fraction(origin: object, axis: str) -> float:
    if origin in {"center"}:
        return 0.5
    if origin in ({"left", "right"} if axis == "left" else {"top", "bottom"}):
        return 0.0 if origin in {"left", "top"} else 1.0
    return 0.0


def _project_onto_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float, float] | None:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return None
    t = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared),
    )
    return t, (start[0] + t * dx), (start[1] + t * dy)


def _segment_intersection(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, float, tuple[float, float]] | None:
    dx1, dy1 = first_end[0] - first_start[0], first_end[1] - first_start[1]
    dx2, dy2 = second_end[0] - second_start[0], second_end[1] - second_start[1]
    denominator = dx1 * dy2 - dy1 * dx2
    if abs(denominator) < 1e-9:
        return None
    offset_x, offset_y = second_start[0] - first_start[0], second_start[1] - first_start[1]
    first_t = (offset_x * dy2 - offset_y * dx2) / denominator
    second_t = (offset_x * dy1 - offset_y * dx1) / denominator
    if not (-1e-9 <= first_t <= 1 + 1e-9 and -1e-9 <= second_t <= 1 + 1e-9):
        return None
    point = (first_start[0] + first_t * dx1, first_start[1] + first_t * dy1)
    return first_t, second_t, point


def _snap_node(
    point: tuple[float, float],
    locations: list[tuple[float, float]],
    tolerance: float,
) -> int:
    for index, location in enumerate(locations):
        if math.dist(point, location) <= tolerance:
            return index
    locations.append(point)
    return len(locations) - 1
