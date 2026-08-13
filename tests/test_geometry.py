from __future__ import annotations

from math import cos, radians, sin, sqrt
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from shroud_designer.geometry import (
    ConnectorStackConfig,
    FanConfig,
    FanStackConfig,
    FunnelConfig,
    GeometryError,
    analyze_connector,
    analyze_fan,
    build_assembly_parts,
    export_stl,
    make_custom_fan,
    make_funnel,
    mesh_component_count,
    union_assembly,
)
from shroud_designer.geometry import _resample_boundary, _slice_polygons


ROOT = Path(__file__).resolve().parents[1]
GPU = ROOT / "GPU Connectors" / "cmp front.stl"
FAN = ROOT / "Fans" / "default 120mm fan for shroud.stl"


@pytest.fixture(scope="module")
def connector():
    return analyze_connector(GPU)


@pytest.mark.parametrize(
    "polygon",
    [
        Polygon([(0.0, 0.0), (37.0, 0.0), (37.0, 19.0), (0.0, 19.0)]),
        Polygon(
            [
                (0.0, 0.0),
                (40.0, 0.0),
                (40.0, 30.0),
                (24.0, 30.0),
                (24.0, 18.0),
                (0.0, 18.0),
            ]
        ),
    ],
)
def test_boundary_resampling_preserves_sharp_corners(polygon: Polygon) -> None:
    sampled = _resample_boundary(polygon, 32)
    corners = np.asarray(polygon.exterior.coords[:-1], dtype=float)
    distances = np.linalg.norm(corners[:, None, :] - sampled[None, :, :], axis=2)

    assert sampled.shape == (32, 2)
    assert np.all(np.min(distances, axis=1) < 1e-9)


def test_detects_topmost_gpu_opening(connector) -> None:
    assert connector.mesh.is_watertight
    assert connector.top_z == pytest.approx(21.75, abs=0.01)
    assert connector.opening.width == pytest.approx(38.705, abs=0.05)
    assert connector.opening.depth == pytest.approx(98.397, abs=0.05)
    assert connector.opening.wall_thickness == pytest.approx(1.898, abs=0.02)


def test_funnel_automatically_traces_connector_top_rim(connector) -> None:
    parts = build_assembly_parts(
        connector,
        FunnelConfig(length=30.0, radial_segments=128, path_segments=24),
        fan_config=FanConfig(),
    )
    inlet_z = connector.top_z - FunnelConfig().join_overlap
    inlet_vertices = parts.funnel.vertices[
        np.isclose(parts.funnel.vertices[:, 2], inlet_z, atol=1e-6)
    ][:, :2]
    outer_points = np.asarray(connector.outer_polygon.exterior.coords[:-1])
    incoming = outer_points - np.roll(outer_points, 1, axis=0)
    outgoing = np.roll(outer_points, -1, axis=0) - outer_points
    cosine = np.sum(incoming * outgoing, axis=1) / (
        np.linalg.norm(incoming, axis=1) * np.linalg.norm(outgoing, axis=1)
    )
    outer_corners = outer_points[
        np.arccos(np.clip(cosine, -1.0, 1.0)) >= radians(15.0)
    ]
    distances = np.linalg.norm(
        outer_corners[:, None, :] - inlet_vertices[None, :, :], axis=2
    )

    assert FunnelConfig().wall_thickness is None
    assert np.all(np.min(distances, axis=1) < 1e-6)
    assert parts.funnel.is_watertight


def test_deep_slots_can_begin_smoothing_immediately_without_opening_the_wall() -> None:
    opening = Polygon(
        [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 80.0),
            (72.0, 80.0),
            (72.0, 34.0),
            (61.0, 34.0),
            (61.0, 80.0),
            (39.0, 80.0),
            (39.0, 34.0),
            (28.0, 34.0),
            (28.0, 80.0),
            (0.0, 80.0),
        ]
    )
    result = make_funnel(
        opening,
        0.0,
        FunnelConfig(
            wall_thickness=2.0,
            length=25.0,
            rounding_start=0.0,
            radial_segments=96,
            path_segments=48,
        ),
        inlet_outer_polygon=opening.buffer(2.0, join_style="round"),
    )

    assert result.mesh.is_watertight
    assert mesh_component_count(result.mesh) == 1
    first_airway_area: float | None = None
    for height in (0.05, 0.21, 0.5, 1.0, 2.0, 4.0, 6.0, 7.9):
        contours = _slice_polygons(result.mesh, height)
        assert len(contours) == 2
        assert contours[0].contains(contours[1])
        if height == 0.5:
            first_airway_area = contours[1].area
    assert first_airway_area is not None
    assert first_airway_area > opening.area


def test_rounding_start_preserves_cable_access_profile_before_transition() -> None:
    opening = Polygon(
        [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 80.0),
            (72.0, 80.0),
            (72.0, 34.0),
            (61.0, 34.0),
            (61.0, 80.0),
            (39.0, 80.0),
            (39.0, 34.0),
            (28.0, 34.0),
            (28.0, 80.0),
            (0.0, 80.0),
        ]
    )
    result = make_funnel(
        opening,
        0.0,
        FunnelConfig(
            wall_thickness=2.0,
            length=30.0,
            rounding_start=8.0,
            radial_segments=96,
            path_segments=48,
        ),
        inlet_outer_polygon=opening.buffer(2.0, join_style="round"),
    )

    before = _slice_polygons(result.mesh, 7.5)
    after = _slice_polygons(result.mesh, 9.5)
    assert len(before) == 2
    assert len(after) == 2
    assert before[1].area == pytest.approx(opening.area, abs=1.0)
    assert after[1].area > before[1].area + 1.0


def test_reference_fan_measurements() -> None:
    fan = analyze_fan(FAN)
    assert fan.mesh.is_watertight
    assert fan.hole_diameter == pytest.approx(116.005, abs=0.03)
    assert fan.z_max - fan.z_min == pytest.approx(3.0, abs=0.01)


@pytest.mark.parametrize(
    ("size", "opening", "expected_extent"),
    [(120.0, 116.0, 120.0), (140.0, 136.0, 140.0)],
)
def test_custom_fan_is_not_scaled(size: float, opening: float, expected_extent: float) -> None:
    mesh = make_custom_fan(FanConfig(size=size, hole_diameter=opening, screw_hole_diameter=4.6))
    assert mesh.is_watertight
    assert mesh.extents[0] == pytest.approx(expected_extent, abs=0.02)
    assert mesh.extents[1] == pytest.approx(expected_extent, abs=0.02)
    assert mesh.extents[2] == pytest.approx(3.0, abs=0.001)


def test_straight_offset_keeps_fan_parallel_and_unions(connector) -> None:
    config = FunnelConfig(length=50.0, offset_x=15.0, offset_y=-8.0)
    parts = build_assembly_parts(connector, config, fan_config=FanConfig())
    assert parts.funnel.is_watertight
    assert parts.funnel_result.outlet_center[0] == pytest.approx(
        connector.opening.polygon.centroid.x + 15.0
    )
    assert parts.funnel_result.outlet_center[1] == pytest.approx(
        connector.opening.polygon.centroid.y - 8.0
    )
    assert np.allclose(parts.funnel_result.outlet_basis, np.eye(3))
    final = union_assembly(parts)
    assert final.is_watertight
    assert mesh_component_count(final) == 1


def test_compound_curve_rotates_fan_perpendicular(connector) -> None:
    angle_x, angle_y = 30.0, -20.0
    magnitude = sqrt(angle_x**2 + angle_y**2)
    theta = radians(magnitude)
    expected_tangent = np.array(
        [
            sin(theta) * angle_x / magnitude,
            sin(theta) * angle_y / magnitude,
            cos(theta),
        ]
    )
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            curved=True,
            angle_x=angle_x,
            angle_y=angle_y,
            lead_in=25.0,
            lead_out=25.0,
            arc_diameter=60.0,
        ),
        fan_config=FanConfig(),
    )
    assert parts.funnel.is_watertight
    assert np.allclose(parts.funnel_result.outlet_basis[:, 2], expected_tangent, atol=1e-6)
    final = union_assembly(parts)
    assert final.is_watertight
    assert mesh_component_count(final) == 1


def test_imported_fan_uses_detected_opening(connector) -> None:
    fan = analyze_fan(FAN)
    parts = build_assembly_parts(
        connector,
        FunnelConfig(length=35.0),
        imported_fan=fan,
    )
    assert parts.fan.is_watertight
    assert parts.funnel_result.mesh.is_watertight
    assert parts.funnel_result.outlet_center[2] == pytest.approx(connector.top_z + 35.0)


def test_exports_binary_stl(connector, tmp_path: Path) -> None:
    target = tmp_path / "print-ready.stl"
    parts = build_assembly_parts(
        connector,
        FunnelConfig(length=40.0),
        fan_config=FanConfig(),
    )
    result = export_stl(parts, target)
    assert target.is_file()
    assert target.stat().st_size > 1_000
    assert result.is_watertight
    reloaded = trimesh.load(target, force="mesh", process=True)
    assert reloaded.is_watertight
    assert mesh_component_count(reloaded) == 1
    assert not np.any(reloaded.area_faces < 1e-10)
    # Binary STL: 80-byte header, uint32 triangle count, then 50 bytes per triangle.
    assert target.stat().st_size == 84 + len(result.faces) * 50


def test_stacked_connector_layout_uses_clear_spacing(connector) -> None:
    spacing = 7.0
    count = 3
    parts = build_assembly_parts(
        connector,
        FunnelConfig(length=30.0, split_distance=20.0, radial_segments=64, path_segments=24),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(count=count, axis="x", spacing=spacing),
    )
    expected_x = connector.mesh.extents[0] * count + spacing * (count - 1)
    assert parts.gpu.extents[0] == pytest.approx(expected_x, abs=0.01)
    assert parts.gpu.extents[1] == pytest.approx(connector.mesh.extents[1], abs=0.01)
    assert parts.funnel_result.centerline_length == pytest.approx(50.0)


@pytest.mark.parametrize("count", [2, 4, 10])
def test_multi_connector_manifold_exports_as_one_watertight_solid(
    connector, count: int
) -> None:
    parts = build_assembly_parts(
        connector,
        FunnelConfig(length=35.0, split_distance=20.0, radial_segments=64, path_segments=24),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(count=count, axis="y", spacing=5.0),
    )
    result = union_assembly(parts)
    assert result.is_watertight
    assert mesh_component_count(result) == 1


def test_multi_connector_compound_curve_keeps_fan_perpendicular(connector) -> None:
    angle_x, angle_y = 20.0, 15.0
    magnitude = sqrt(angle_x**2 + angle_y**2)
    theta = radians(magnitude)
    expected_tangent = np.array(
        [
            sin(theta) * angle_x / magnitude,
            sin(theta) * angle_y / magnitude,
            cos(theta),
        ]
    )
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            curved=True,
            angle_x=angle_x,
            angle_y=angle_y,
            lead_in=25.0,
            lead_out=25.0,
            split_distance=20.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(count=2, axis="x", spacing=5.0),
    )
    assert np.allclose(parts.funnel_result.outlet_basis[:, 2], expected_tangent, atol=1e-6)
    final = union_assembly(parts)
    assert final.is_watertight
    assert mesh_component_count(final) == 1


@pytest.mark.parametrize(("count", "axis"), [(2, "x"), (4, "y")])
def test_multi_fan_manifold_exports_as_one_watertight_solid(
    connector, count: int, axis: str
) -> None:
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            length=35.0,
            fan_split_distance=20.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        fan_stack_config=FanStackConfig(
            count=count, axis=axis, spacing=5.0
        ),
    )
    result = union_assembly(parts)
    assert result.is_watertight
    assert mesh_component_count(result) == 1
    assert mesh_component_count(parts.fan) == count
    assert parts.funnel_result.centerline_length == pytest.approx(55.0)


def test_multi_fan_layout_uses_clear_spacing(connector) -> None:
    spacing = 7.0
    count = 3
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            length=30.0,
            fan_split_distance=20.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        fan_stack_config=FanStackConfig(
            count=count, axis="x", spacing=spacing
        ),
    )
    assert parts.fan.extents[0] == pytest.approx(
        120.0 * count + spacing * (count - 1), abs=0.02
    )
    assert parts.fan.extents[1] == pytest.approx(120.0, abs=0.02)


def test_simultaneous_gpu_and_fan_splits_are_watertight(
    connector, tmp_path: Path
) -> None:
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            length=30.0,
            split_distance=15.0,
            fan_split_distance=25.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(
            count=3, axis="x", spacing=5.0
        ),
        fan_stack_config=FanStackConfig(
            count=2, axis="y", spacing=10.0
        ),
    )
    target = tmp_path / "multi-gpu-multi-fan.stl"
    result = export_stl(parts, target)
    assert result.is_watertight
    assert mesh_component_count(result) == 1
    assert target.is_file()
    assert target.stat().st_size == 84 + len(result.faces) * 50
    assert parts.funnel_result.centerline_length == pytest.approx(70.0)


def test_curved_multi_fan_outlets_remain_perpendicular(connector) -> None:
    angle_x, angle_y = 20.0, -15.0
    magnitude = sqrt(angle_x**2 + angle_y**2)
    theta = radians(magnitude)
    expected_normal = np.array(
        [
            sin(theta) * angle_x / magnitude,
            sin(theta) * angle_y / magnitude,
            cos(theta),
        ]
    )
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            curved=True,
            angle_x=angle_x,
            angle_y=angle_y,
            lead_in=20.0,
            lead_out=20.0,
            arc_diameter=60.0,
            fan_split_distance=20.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        fan_stack_config=FanStackConfig(
            count=2, axis="x", spacing=5.0
        ),
    )
    normal = parts.funnel_result.outlet_basis[:, 2]
    assert np.allclose(normal, expected_normal, atol=1e-6)
    result = union_assembly(parts)
    assert result.is_watertight
    assert mesh_component_count(result) == 1


@pytest.mark.parametrize("mode", ["full", "front", "back"])
def test_gpu_bridge_modes_fuse_connector_bodies(connector, mode: str) -> None:
    unbridged = build_assembly_parts(
        connector,
        FunnelConfig(
            length=25.0,
            split_distance=15.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(
            count=2, axis="y", spacing=5.0
        ),
    )
    bridged = build_assembly_parts(
        connector,
        FunnelConfig(
            length=25.0,
            split_distance=15.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        stack_config=ConnectorStackConfig(
            count=2,
            axis="y",
            spacing=5.0,
            bridge_mode=mode,
            bridge_thickness=4.0,
        ),
    )
    assert mesh_component_count(unbridged.gpu) == 2
    assert mesh_component_count(bridged.gpu) == 1
    assert bridged.gpu.volume > unbridged.gpu.volume
    assert union_assembly(bridged).is_watertight


def test_fan_bridge_fuses_plate_bodies(connector) -> None:
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            length=25.0,
            fan_split_distance=15.0,
            radial_segments=64,
            path_segments=24,
        ),
        fan_config=FanConfig(),
        fan_stack_config=FanStackConfig(
            count=4, axis="x", spacing=5.0, bridged=True
        ),
    )
    assert mesh_component_count(parts.fan) == 1
    result = union_assembly(parts)
    assert result.is_watertight
    assert mesh_component_count(result) == 1


def test_imported_fan_can_be_repeated_and_bridged(connector) -> None:
    imported = analyze_fan(FAN)
    parts = build_assembly_parts(
        connector,
        FunnelConfig(
            length=30.0,
            fan_split_distance=20.0,
            radial_segments=64,
            path_segments=24,
        ),
        imported_fan=imported,
        fan_stack_config=FanStackConfig(
            count=2, axis="y", spacing=5.0, bridged=True
        ),
    )
    assert mesh_component_count(parts.fan) == 1
    assert union_assembly(parts).is_watertight


def test_fan_count_is_limited_to_four(connector) -> None:
    with pytest.raises(GeometryError, match="Fan count must be between 1 and 4"):
        build_assembly_parts(
            connector,
            FunnelConfig(length=30.0),
            fan_config=FanConfig(),
            fan_stack_config=FanStackConfig(count=5),
        )
