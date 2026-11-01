import networkx as nx
import pymupdf
import pytest

from rushhour.assignment import simulate
from rushhour.networks import load_graphml_bytes
from rushhour.tracing import extract_canvas_lines, lines_to_graph, render_pdf_page


def test_crossing_traces_are_split_into_routable_intersections():
    graph = lines_to_graph(
        [(10, 50, 90, 50), (50, 10, 50, 90)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        snap_tolerance_px=1,
    )

    assert graph.number_of_nodes() == 5
    assert graph.number_of_edges() == 8
    assert any(
        (data["canvas_x_px"], data["canvas_y_px"]) == (50.0, 50.0)
        for _, data in graph.nodes(data=True)
    )


def test_canvas_line_extraction_handles_fabric_line_coordinates():
    canvas = {
        "objects": [
            {
                "type": "line",
                "left": 10,
                "top": 25,
                "width": 80,
                "height": 0,
                "originX": "left",
                "originY": "top",
                "x1": -40,
                "y1": 0,
                "x2": 40,
                "y2": 0,
            },
            {"type": "rect", "left": 20, "top": 30, "width": 5, "height": 5},
        ]
    }

    assert extract_canvas_lines(canvas) == [(10.0, 25.0, 90.0, 25.0)]


def test_traced_graph_exports_as_importable_graphml():
    traced = lines_to_graph(
        [(0, 0, 100, 0), (50, -50, 50, 50)],
        canvas_width=100,
        scale_km_per_pixel=0.005,
        speed_kph=40,
        lanes=2,
    )
    graphml = "".join(nx.generate_graphml(traced)).encode("utf-8")

    restored = load_graphml_bytes(graphml)

    assert restored.number_of_nodes() == 5
    assert restored.number_of_edges() == 8
    assert all(data["lanes"] == 2 for _, _, data in restored.edges(data=True))


def test_traced_graph_can_assign_demand_and_support_one_way_streets():
    graph = lines_to_graph(
        [(0, 0, 100, 0)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        bidirectional=False,
        snap_tolerance_px=1,
    )

    assert graph.number_of_edges() == 1
    result = simulate(graph, [("n0", "n1", 100.0)], max_iterations=10)
    assert result.total_demand == 100


def test_render_pdf_page_preserves_canvas_size_and_page_count():
    document = pymupdf.open()
    document.new_page(width=300, height=500)
    document.new_page(width=500, height=300)

    image, page_count = render_pdf_page(document.tobytes(), 1, width=200, height=120)

    assert image.size == (200, 120)
    assert page_count == 2


def test_empty_or_invalid_traces_are_reported():
    with pytest.raises(ValueError, match="Trace at least one road"):
        lines_to_graph([], canvas_width=100, scale_km_per_pixel=0.01)
    with pytest.raises(ValueError, match="scale"):
        lines_to_graph([(0, 0, 10, 10)], canvas_width=100, scale_km_per_pixel=0)
