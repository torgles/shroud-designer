from __future__ import annotations

from dataclasses import dataclass, field, replace
from io import BytesIO
from math import atan2, cos, pi, radians, sin, sqrt
from pathlib import Path
from typing import Iterable

import numpy as np
import trimesh
import mapbox_earcut
from manifold3d import Manifold, Mesh
from shapely.affinity import translate as translate_polygon
from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union


class GeometryError(RuntimeError):
    """A user-facing geometry error."""


@dataclass(slots=True)
class OpeningCandidate:
    polygon: Polygon
    area: float
    width: float
    depth: float
    wall_thickness: float

    @property
    def label(self) -> str:
        return f"{self.width:.1f} x {self.depth:.1f} mm ({self.area:.0f} mm²)"


@dataclass(slots=True)
class ConnectorAnalysis:
    path: Path
    mesh: trimesh.Trimesh
    top_z: float
    candidates: list[OpeningCandidate]
    outer_polygon: Polygon
    selected_index: int = 0

    @property
    def opening(self) -> OpeningCandidate:
        return self.candidates[self.selected_index]


@dataclass(slots=True)
class FanAnalysis:
    path: Path
    mesh: trimesh.Trimesh
    hole_polygon: Polygon
    hole_center: np.ndarray
    hole_diameter: float
    z_min: float
    z_max: float


@dataclass(slots=True)
class FunnelConfig:
    wall_thickness: float | None = None
    curved: bool = False
    length: float = 50.0
    rounding_start: float = 8.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    angle_x: float = 0.0
    angle_y: float = 0.0
    lead_in: float = 25.0
    lead_out: float = 25.0
    arc_diameter: float = 60.0
    outlet_diameter: float = 116.0
    radial_segments: int = 128
    path_segments: int = 48
    join_overlap: float = 0.30
    split_distance: float = 20.0
    fan_split_distance: float = 20.0


@dataclass(slots=True)
class ConnectorStackConfig:
    count: int = 1
    axis: str = "y"
    spacing: float = 0.0
    bridge_mode: str = "unbridged"
    bridge_thickness: float = 5.0


@dataclass(slots=True)
class FanStackConfig:
    count: int = 1
    axis: str = "y"
    spacing: float = 0.0
    bridged: bool = False


@dataclass(slots=True)
class FanConfig:
    size: float = 120.0
    hole_diameter: float = 116.0
    screw_hole_diameter: float = 4.6
    thickness: float = 3.0
    corner_radius: float = 5.0

    @property
    def screw_spacing(self) -> float:
        if abs(self.size - 140.0) < 0.01:
            return 124.5
        if abs(self.size - 120.0) < 0.01:
            return 105.0
        # Preserve the 7.5 mm edge inset used by the supplied 120 mm plate.
        return max(1.0, self.size - 15.0)


@dataclass(slots=True)
class FunnelResult:
    mesh: trimesh.Trimesh
    outlet_center: np.ndarray
    outlet_basis: np.ndarray
    centerline_length: float
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AssemblyParts:
    gpu: trimesh.Trimesh
    funnel: trimesh.Trimesh
    fan: trimesh.Trimesh
    funnel_result: FunnelResult

    @property
    def meshes(self) -> list[trimesh.Trimesh]:
        return [self.gpu, self.funnel, self.fan]


def _load_mesh(path: str | Path) -> trimesh.Trimesh:
    source = Path(path)
    if not source.is_file():
        raise GeometryError(f"STL file not found: {source}")
    loaded = trimesh.load(source, force="mesh", process=True)
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise GeometryError(f"No triangle mesh was found in {source.name}.")
    loaded.remove_unreferenced_vertices()
    loaded.merge_vertices()
    if not loaded.is_winding_consistent:
        loaded.fix_normals()
    return loaded


def _slice_polygons(mesh: trimesh.Trimesh, z: float) -> list[Polygon]:
    # Intersect triangles directly instead of using trimesh.Path. The latter pulls
    # in scipy just to walk a tiny edge graph, which is unnecessary for this app.
    segments: list[tuple[np.ndarray, np.ndarray]] = []
    tolerance = 1e-8
    for triangle in np.asarray(mesh.triangles, dtype=float):
        distances = triangle[:, 2] - z
        if np.min(distances) > tolerance or np.max(distances) < -tolerance:
            continue
        intersections: list[np.ndarray] = []
        for first, second in ((0, 1), (1, 2), (2, 0)):
            point_a, point_b = triangle[first], triangle[second]
            distance_a, distance_b = distances[first], distances[second]
            if abs(distance_a) <= tolerance:
                intersections.append(point_a[:2])
            if distance_a * distance_b < -(tolerance**2):
                fraction = distance_a / (distance_a - distance_b)
                intersections.append((point_a + fraction * (point_b - point_a))[:2])
        unique: list[np.ndarray] = []
        for point in intersections:
            if not any(np.linalg.norm(point - existing) <= 1e-7 for existing in unique):
                unique.append(point)
        if len(unique) == 2:
            segments.append((unique[0], unique[1]))

    if not segments:
        return []

    quantization = 1e-5
    coordinates: dict[tuple[int, int], np.ndarray] = {}
    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {}
    unused_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def key(point: np.ndarray) -> tuple[int, int]:
        return tuple(np.round(point / quantization).astype(np.int64))  # type: ignore[return-value]

    for point_a, point_b in segments:
        key_a, key_b = key(point_a), key(point_b)
        if key_a == key_b:
            continue
        coordinates.setdefault(key_a, point_a)
        coordinates.setdefault(key_b, point_b)
        adjacency.setdefault(key_a, set()).add(key_b)
        adjacency.setdefault(key_b, set()).add(key_a)
        unused_edges.add(tuple(sorted((key_a, key_b))))

    loops: list[np.ndarray] = []
    while unused_edges:
        first_edge = next(iter(unused_edges))
        start, current = first_edge
        unused_edges.remove(first_edge)
        loop_keys = [start]
        guard = 0
        while current != start and guard <= len(adjacency) + 1:
            loop_keys.append(current)
            options = [
                neighbor
                for neighbor in adjacency.get(current, ())
                if tuple(sorted((current, neighbor))) in unused_edges
            ]
            if not options:
                break
            next_key = options[0]
            unused_edges.remove(tuple(sorted((current, next_key))))
            current = next_key
            guard += 1
        if current == start and len(loop_keys) >= 3:
            loops.append(np.array([coordinates[item] for item in loop_keys], dtype=float))

    polygons: list[Polygon] = []
    for points in loops:
        if len(points) < 4:
            continue
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if isinstance(polygon, Polygon) and polygon.area > 0.5:
            polygons.append(orient(polygon, sign=1.0))
    return sorted(polygons, key=lambda item: item.area, reverse=True)


def _horizontal_section(mesh: trimesh.Trimesh, near_z: float) -> tuple[float, list[Polygon]]:
    z_extent = float(mesh.extents[2])
    initial = min(0.10, max(0.02, z_extent * 0.001))
    for distance in (initial, 0.01, 0.05, 0.15, 0.5, 1.0):
        z = near_z - distance
        polygons = _slice_polygons(mesh, z)
        if len(polygons) >= 2:
            return z, polygons
    raise GeometryError(
        "Could not find a closed opening just below the model's top layer. "
        "Check that the STL is upright and its duct opening is at maximum Z."
    )


def _contained_holes(outer: Polygon, polygons: Iterable[Polygon]) -> list[Polygon]:
    holes = [
        polygon
        for polygon in polygons
        if outer.buffer(0.05).contains(polygon.representative_point())
        and polygon.area < outer.area * 0.995
    ]
    return sorted(holes, key=lambda item: item.area, reverse=True)


def _infer_wall_thickness(opening: Polygon, outer: Polygon) -> float:
    """Estimate the nominal top-rim width without being skewed by tabs or corners."""
    ring = opening.exterior
    sample_count = max(256, min(2048, len(ring.coords) * 16))
    distances = np.asarray(
        [
            outer.exterior.distance(
                ring.interpolate(ring.length * index / sample_count)
            )
            for index in range(sample_count)
        ],
        dtype=float,
    )
    distances = distances[np.isfinite(distances) & (distances > 0.05)]
    if len(distances) < sample_count // 4:
        raise GeometryError(
            "Could not determine the wall thickness around the selected top opening."
        )
    thickness = float(np.median(distances))
    if thickness <= 0.05:
        raise GeometryError(
            "The selected top opening does not have a measurable surrounding wall."
        )
    return thickness


def analyze_connector(path: str | Path) -> ConnectorAnalysis:
    mesh = _load_mesh(path)
    if not mesh.is_watertight:
        raise GeometryError(
            "The GPU connector STL is not watertight. Repair the STL before using it "
            "so the final assembly can be airtight."
        )

    top_z = float(mesh.bounds[1, 2])
    _, polygons = _horizontal_section(mesh, top_z)
    outer = polygons[0]
    openings = _contained_holes(outer, polygons[1:])
    if not openings:
        raise GeometryError("No opening was detected inside the topmost perimeter.")

    candidates = []
    for polygon in openings:
        min_x, min_y, max_x, max_y = polygon.bounds
        candidates.append(
            OpeningCandidate(
                polygon=polygon,
                area=float(polygon.area),
                width=float(max_x - min_x),
                depth=float(max_y - min_y),
                wall_thickness=_infer_wall_thickness(polygon, outer),
            )
        )
    return ConnectorAnalysis(Path(path), mesh, top_z, candidates, outer)


def analyze_fan(path: str | Path) -> FanAnalysis:
    mesh = _load_mesh(path)
    if not mesh.is_watertight:
        raise GeometryError("The imported fan connector STL is not watertight.")

    z_min, z_max = (float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2]))
    polygons = _slice_polygons(mesh, (z_min + z_max) / 2.0)
    if len(polygons) < 2:
        raise GeometryError("No central fan opening was detected in this STL.")
    outer = polygons[0]
    holes = _contained_holes(outer, polygons[1:])
    if not holes:
        raise GeometryError("No central fan opening was detected in this STL.")

    # The airflow opening is the largest hole. The smaller holes are fasteners.
    hole = holes[0]
    center = hole.centroid
    min_x, min_y, max_x, max_y = hole.bounds
    diameter = ((max_x - min_x) + (max_y - min_y)) / 2.0
    return FanAnalysis(
        Path(path),
        mesh,
        hole,
        np.array([center.x, center.y], dtype=float),
        float(diameter),
        z_min,
        z_max,
    )


def _resample_boundary(
    polygon: Polygon,
    count: int,
    start_near: np.ndarray | None = None,
    preserve_corners: bool = True,
) -> np.ndarray:
    polygon = orient(polygon, sign=1.0)
    ring = polygon.exterior
    length = ring.length
    points = np.array(
        [ring.interpolate(length * index / count).coords[0] for index in range(count)],
        dtype=float,
    )
    coordinates = np.asarray(ring.coords[:-1], dtype=float)

    # Uniform perimeter samples normally miss polygon vertices. Connecting the
    # samples then replaces every missed vertex with a chord, visibly clipping
    # square and stepped connector openings. Snap nearby samples onto meaningful
    # direction changes while leaving the rest of the uniform parameterization
    # intact; the latter keeps inner/outer and inlet/outlet rings aligned.
    previous = np.roll(coordinates, 1, axis=0)
    following = np.roll(coordinates, -1, axis=0)
    incoming = coordinates - previous
    outgoing = following - coordinates
    incoming_length = np.linalg.norm(incoming, axis=1)
    outgoing_length = np.linalg.norm(outgoing, axis=1)
    valid = (incoming_length > 1e-7) & (outgoing_length > 1e-7)
    cosine = np.ones(len(coordinates), dtype=float)
    cosine[valid] = np.sum(incoming[valid] * outgoing[valid], axis=1) / (
        incoming_length[valid] * outgoing_length[valid]
    )
    turns = np.arccos(np.clip(cosine, -1.0, 1.0))
    corner_indices = np.flatnonzero(valid & (turns >= radians(15.0)))

    if not preserve_corners:
        corner_indices = np.empty(0, dtype=int)
    elif len(corner_indices) > count:
        strongest = np.argsort(-turns[corner_indices])[:count]
        corner_indices = np.sort(corner_indices[strongest])

    if len(corner_indices):
        segment_lengths = np.linalg.norm(following - coordinates, axis=1)
        cumulative = np.concatenate(([0.0], np.cumsum(segment_lengths)))
        corner_distances = cumulative[corner_indices]
        ideal_slots = corner_distances / length * count

        # Assign strictly increasing slots so clustered CAD vertices cannot
        # reorder the ring. A corner near the closing point belongs in the last
        # slot, not slot zero, unless it is genuinely the first coordinate.
        assigned_slots: list[int] = []
        previous_slot = -1
        corner_total = len(corner_indices)
        for position, ideal_slot in enumerate(ideal_slots):
            slot = max(int(round(float(ideal_slot))), previous_slot + 1)
            slot = min(slot, count - (corner_total - position))
            assigned_slots.append(slot)
            previous_slot = slot
        points[np.asarray(assigned_slots, dtype=int)] = coordinates[corner_indices]
    if start_near is None:
        # A stable start point on the right side minimizes visible loft twist.
        center = np.asarray(polygon.centroid.coords[0])
        scores = points[:, 0] - 0.001 * np.abs(points[:, 1] - center[1])
        start = int(np.argmax(scores))
    else:
        start = int(np.argmin(np.linalg.norm(points - start_near, axis=1)))
    return np.roll(points, -start, axis=0)


def _rodrigues(vector: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 1e-12:
        return vector.copy()
    return (
        vector * cos(angle)
        + np.cross(axis, vector) * sin(angle)
        + axis * np.dot(axis, vector) * (1.0 - cos(angle))
    )


def _mesh_from_rings(outer_rings: list[np.ndarray], inner_rings: list[np.ndarray]) -> trimesh.Trimesh:
    if len(outer_rings) != len(inner_rings) or len(outer_rings) < 2:
        raise GeometryError("The funnel needs at least two matching cross-sections.")
    ring_count = len(outer_rings)
    point_count = len(outer_rings[0])
    outer_vertices = np.concatenate(outer_rings, axis=0)
    inner_vertices = np.concatenate(inner_rings, axis=0)
    vertices = np.vstack((outer_vertices, inner_vertices))
    inner_base = ring_count * point_count

    def outer_index(section: int, point: int) -> int:
        return section * point_count + point % point_count

    def inner_index(section: int, point: int) -> int:
        return inner_base + section * point_count + point % point_count

    faces: list[list[int]] = []
    for section in range(ring_count - 1):
        for point in range(point_count):
            next_point = (point + 1) % point_count
            faces.extend(
                [
                    [
                        outer_index(section, point),
                        outer_index(section, next_point),
                        outer_index(section + 1, next_point),
                    ],
                    [
                        outer_index(section, point),
                        outer_index(section + 1, next_point),
                        outer_index(section + 1, point),
                    ],
                    [
                        inner_index(section, point),
                        inner_index(section + 1, next_point),
                        inner_index(section, next_point),
                    ],
                    [
                        inner_index(section, point),
                        inner_index(section + 1, point),
                        inner_index(section + 1, next_point),
                    ],
                ]
            )

    last = ring_count - 1
    for point in range(point_count):
        next_point = (point + 1) % point_count
        faces.extend(
            [
                [outer_index(0, point), inner_index(0, point), inner_index(0, next_point)],
                [outer_index(0, point), inner_index(0, next_point), outer_index(0, next_point)],
                [outer_index(last, point), outer_index(last, next_point), inner_index(last, next_point)],
                [outer_index(last, point), inner_index(last, next_point), inner_index(last, point)],
            ]
        )

    mesh = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent:
        mesh.fix_normals()
    if not mesh.is_watertight:
        raise GeometryError("The generated funnel was not watertight. Try gentler settings.")
    return mesh


def _stable_polygon_points(polygon: Polygon) -> np.ndarray:
    """Return a CCW contour with a repeatable seam on its right side."""
    polygon = orient(polygon, sign=1.0)
    points = np.asarray(polygon.exterior.coords[:-1], dtype=float)
    center = np.asarray(polygon.centroid.coords[0], dtype=float)
    scores = points[:, 0] - 0.001 * np.abs(points[:, 1] - center[1])
    return np.roll(points, -int(np.argmax(scores)), axis=0)


def _greedy_ring_strip(lower: np.ndarray, upper: np.ndarray) -> np.ndarray:
    """Triangulate between ordered contours without forcing point-to-point pairs.

    Concave features disappear while the GPU opening rounds. Pairing contour
    point 37 to point 37 after that topology change can draw a face across the
    duct. This zipper advances around whichever contour has the nearer next
    point, preserving cyclic order even when the contours have different sizes.
    """
    lower_count, upper_count = len(lower), len(upper)
    lower_index = upper_index = 0
    faces: list[tuple[int, int, int]] = []
    while lower_index < lower_count or upper_index < upper_count:
        lower_point = lower[lower_index % lower_count]
        upper_point = upper[upper_index % upper_count]
        lower_cost = (
            float(
                np.sum(
                    (lower[(lower_index + 1) % lower_count] - upper_point) ** 2
                )
            )
            if lower_index < lower_count
            else float("inf")
        )
        upper_cost = (
            float(
                np.sum(
                    (lower_point - upper[(upper_index + 1) % upper_count]) ** 2
                )
            )
            if upper_index < upper_count
            else float("inf")
        )
        if lower_cost <= upper_cost:
            faces.append(
                (
                    lower_index % lower_count,
                    (lower_index + 1) % lower_count,
                    lower_count + upper_index % upper_count,
                )
            )
            lower_index += 1
        else:
            faces.append(
                (
                    lower_index % lower_count,
                    lower_count + (upper_index + 1) % upper_count,
                    lower_count + upper_index % upper_count,
                )
            )
            upper_index += 1
    return np.asarray(faces, dtype=np.int64)


def _solid_from_cross_sections(
    polygons: list[Polygon],
    centers: list[np.ndarray],
    frames: list[np.ndarray],
    description: str,
) -> trimesh.Trimesh:
    """Create a filled, smoothly lofted solid through arbitrary simple contours."""
    if not (len(polygons) == len(centers) == len(frames) and len(polygons) >= 2):
        raise GeometryError(f"{description} needs matching cross-sections and path frames.")

    local_rings: list[np.ndarray] = []
    rings: list[np.ndarray] = []
    for polygon, center, basis in zip(polygons, centers, frames, strict=True):
        points = _stable_polygon_points(polygon)
        local_rings.append(points)
        rings.append(
            center
            + points[:, 0, None] * basis[:, 0]
            + points[:, 1, None] * basis[:, 1]
        )

    offsets: list[int] = []
    vertex_count = 0
    for ring in rings:
        offsets.append(vertex_count)
        vertex_count += len(ring)

    faces: list[list[int]] = []
    for section in range(len(rings) - 1):
        lower = rings[section]
        upper = rings[section + 1]
        lower_local = local_rings[section]
        upper_local = local_rings[section + 1]
        if len(lower_local) == len(upper_local) and np.allclose(
            lower_local, upper_local, atol=1e-9
        ):
            point_count = len(lower_local)
            strip_faces: list[list[int]] = []
            for point in range(point_count):
                next_point = (point + 1) % point_count
                strip_faces.extend(
                    [
                        [point, next_point, point_count + next_point],
                        [point, point_count + next_point, point_count + point],
                    ]
                )
            strip = np.asarray(strip_faces, dtype=np.int64)
        else:
            strip = _greedy_ring_strip(lower, upper)
        lower_count = len(lower)
        upper_mask = strip >= lower_count
        strip[~upper_mask] += offsets[section]
        strip[upper_mask] = (
            strip[upper_mask] - lower_count + offsets[section + 1]
        )
        faces.extend(strip.tolist())

    for section, reverse in ((0, True), (len(rings) - 1, False)):
        # Earcut only needs coordinates in the section's own 2D frame. Use the
        # original polygon points so curved/rotated world coordinates do not
        # collapse when projected onto global XY.
        local_points = _stable_polygon_points(polygons[section])
        cap = mapbox_earcut.triangulate_float64(
            np.asarray(local_points, dtype=np.float64),
            np.asarray([len(local_points)], dtype=np.uint32),
        ).reshape((-1, 3))
        if reverse:
            cap = cap[:, ::-1]
        faces.extend((cap + offsets[section]).tolist())

    mesh = trimesh.Trimesh(
        vertices=np.concatenate(rings, axis=0),
        faces=np.asarray(faces, dtype=np.int64),
        process=False,
    )
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent:
        mesh.fix_normals()
    if not mesh.is_watertight or mesh_component_count(mesh) != 1:
        edge_uses = np.bincount(mesh.edges_unique_inverse)
        bad_edges = int(np.count_nonzero(edge_uses != 2))
        raise GeometryError(
            f"{description} could not be lofted as one watertight solid "
            f"({bad_edges} unmatched edges, {len(mesh.faces)} faces)."
        )
    return mesh


def _solid_loft(
    start: Polygon,
    start_z: float,
    end: Polygon,
    end_z: float,
    point_count: int,
    sections: int,
    convergence_fraction: float,
    preserve_corners: bool = True,
) -> trimesh.Trimesh:
    start = orient(start, sign=1.0)
    end = orient(end, sign=1.0)
    start_points = _resample_boundary(
        start, point_count, preserve_corners=preserve_corners
    )
    end_points = _resample_boundary(end, point_count, preserve_corners=preserve_corners)
    rings: list[np.ndarray] = []
    for fraction in np.linspace(0.0, 1.0, max(2, sections) + 1):
        shape_fraction = min(fraction / convergence_fraction, 1.0)
        z = start_z * (1.0 - fraction) + end_z * fraction
        points = start_points * (1.0 - shape_fraction) + end_points * shape_fraction
        rings.append(np.column_stack((points, np.full(point_count, z))))

    ring_count = len(rings)
    vertices = np.concatenate(rings, axis=0)
    faces: list[list[int]] = []
    for section in range(ring_count - 1):
        lower = section * point_count
        upper = (section + 1) * point_count
        for point in range(point_count):
            next_point = (point + 1) % point_count
            faces.extend(
                [
                    [lower + point, lower + next_point, upper + next_point],
                    [lower + point, upper + next_point, upper + point],
                ]
            )
    cap_indices = mapbox_earcut.triangulate_float64(
        np.asarray(rings[0][:, :2], dtype=np.float64),
        np.asarray([point_count], dtype=np.uint32),
    ).reshape((-1, 3))
    last = (ring_count - 1) * point_count
    for triangle in cap_indices:
        a, b, c = (int(value) for value in triangle)
        faces.extend([[c, b, a], [last + a, last + b, last + c]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent:
        mesh.fix_normals()
    if not mesh.is_watertight:
        raise GeometryError("A generated split volume was not watertight.")
    return mesh


def _boolean_union(meshes: list[trimesh.Trimesh], description: str) -> trimesh.Trimesh:
    try:
        result = trimesh.boolean.union(meshes, engine="manifold", check_volume=False)
    except Exception as exc:
        raise GeometryError(f"Could not merge {description}: {exc}") from exc
    if result is None:
        raise GeometryError(f"Could not merge {description}.")
    if isinstance(result, trimesh.Scene):
        result = result.to_mesh()
    return result


def _prepare_for_stl(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Retriangulate Boolean seams so float32 STL vertices remain closed."""
    manifold = Manifold(
        Mesh(
            vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
            tri_verts=np.asarray(mesh.faces, dtype=np.uint32),
        )
    )
    cleaned: trimesh.Trimesh | None = None
    for tolerance in (1e-6, 1e-4, 1e-3, 3e-3, 1e-2):
        manifold_mesh = manifold.simplify(tolerance).to_mesh()
        candidate = trimesh.Trimesh(
            vertices=manifold_mesh.vert_properties,
            faces=manifold_mesh.tri_verts,
            process=False,
        )
        candidate.remove_unreferenced_vertices()
        cleaned = candidate
        if not np.any(candidate.area_faces < 1e-10):
            break
    assert cleaned is not None
    return cleaned


def _validate_stack(
    count: int,
    maximum: int,
    axis: str,
    spacing: float,
    description: str,
) -> None:
    if not 1 <= count <= maximum:
        raise GeometryError(f"{description} count must be between 1 and {maximum}.")
    if axis not in {"x", "y"}:
        raise GeometryError(f"{description} can only be stacked along X or Y.")
    if spacing < 0:
        raise GeometryError(f"{description} spacing cannot be negative.")


def _stack_translations(
    mesh: trimesh.Trimesh,
    count: int,
    axis: str,
    spacing: float,
) -> list[np.ndarray]:
    axis_index = 0 if axis == "x" else 1
    step = float(mesh.extents[axis_index]) + spacing
    offsets = (np.arange(count, dtype=float) - (count - 1) / 2.0) * step
    return [
        np.array(
            [offset if axis == "x" else 0.0, offset if axis == "y" else 0.0, 0.0],
            dtype=float,
        )
        for offset in offsets
    ]


def _bridge_stacked_meshes(
    meshes: list[trimesh.Trimesh],
    base_bounds: np.ndarray,
    translations: list[np.ndarray],
    axis: str,
    mode: str,
    thickness: float,
    overlap: float,
    description: str,
) -> trimesh.Trimesh:
    if len(meshes) == 1 or mode == "unbridged":
        return trimesh.util.concatenate(meshes)
    if mode not in {"full", "front", "back"}:
        raise GeometryError(f"Unknown {description.lower()} bridge mode: {mode}.")
    if mode in {"front", "back"} and thickness <= 0:
        raise GeometryError(f"{description} bridge thickness must be greater than zero.")

    axis_index = 0 if axis == "x" else 1
    other_index = 1 - axis_index
    z_min, z_max = float(base_bounds[0, 2]), float(base_bounds[1, 2])
    if mode == "front":
        z_min = max(z_min, z_max - thickness)
    elif mode == "back":
        z_max = min(z_max, z_min + thickness)

    contact = max(0.5, overlap)
    bridges: list[trimesh.Trimesh] = []
    for first, second in zip(translations, translations[1:]):
        low = np.array(base_bounds[0], dtype=float)
        high = np.array(base_bounds[1], dtype=float)
        low[axis_index] = base_bounds[1, axis_index] + first[axis_index] - contact
        high[axis_index] = base_bounds[0, axis_index] + second[axis_index] + contact
        low[other_index] = base_bounds[0, other_index]
        high[other_index] = base_bounds[1, other_index]
        low[2], high[2] = z_min, z_max
        extents = high - low
        if np.any(extents <= 0):
            raise GeometryError(f"The {description.lower()} bridge has no printable volume.")
        transform = trimesh.transformations.translation_matrix((low + high) / 2.0)
        bridges.append(trimesh.creation.box(extents=extents, transform=transform))

    merged = _boolean_union([*meshes, *bridges], f"the {description.lower()} bridges")
    merged.remove_unreferenced_vertices()
    if not merged.is_winding_consistent:
        merged.fix_normals()
    if not merged.is_watertight or mesh_component_count(merged) != 1:
        raise GeometryError(
            f"The {description.lower()} bridges did not fuse every stacked copy."
        )
    return merged


def _combined_opening(openings: list[Polygon], description: str) -> Polygon:
    combined = unary_union(openings).convex_hull
    if not isinstance(combined, Polygon):
        raise GeometryError(f"The {description} openings could not be combined.")
    return orient(combined, sign=1.0)


def _reverse_branch(mesh: trimesh.Trimesh, distance: float) -> trimesh.Trimesh:
    reversed_mesh = mesh.copy()
    vertices = reversed_mesh.vertices.copy()
    vertices[:, 2] = distance - vertices[:, 2]
    reversed_mesh.vertices = vertices
    reversed_mesh.invert()
    reversed_mesh.remove_unreferenced_vertices()
    if not reversed_mesh.is_winding_consistent:
        reversed_mesh.fix_normals()
    return reversed_mesh


def _place_local_mesh(
    mesh: trimesh.Trimesh,
    origin: np.ndarray,
    basis: np.ndarray,
    offset: np.ndarray | None = None,
) -> trimesh.Trimesh:
    placed = mesh.copy()
    local = placed.vertices.copy()
    if offset is not None:
        local += offset
    placed.vertices = origin + local @ basis.T
    placed.remove_unreferenced_vertices()
    return placed


def _make_branch_manifold(
    openings: list[Polygon],
    top_z: float,
    split_z: float,
    wall_thickness: float,
    point_count: int,
    path_segments: int,
    overlap: float,
    inlet_outer_polygons: list[Polygon] | None = None,
) -> tuple[trimesh.Trimesh, Polygon]:
    combined = unary_union(openings).convex_hull
    if not isinstance(combined, Polygon):
        raise GeometryError("The stacked openings could not be combined into a manifold.")
    combined = orient(combined, sign=1.0)
    combined_outer = combined.buffer(wall_thickness, join_style="round")
    if not isinstance(combined_outer, Polygon):
        raise GeometryError("The merged opening could not be offset into a wall.")
    sections = max(2, min(12, int(path_segments / 4)))
    split_distance = split_z - top_z
    chamber_height = max(0.1, min(8.0, split_distance * 0.5))
    chamber_bottom = split_z - chamber_height

    # Separate straight ducts enter a short collector chamber. Building the
    # material as outer solid volumes minus overlapping airflow volumes keeps
    # the manifold reliable from two connectors all the way through ten.
    trace_inlet_corners = inlet_outer_polygons is not None
    if inlet_outer_polygons is None:
        inlet_outer_polygons = [
            opening.buffer(wall_thickness, join_style="round")
            for opening in openings
        ]
    if len(inlet_outer_polygons) != len(openings):
        raise GeometryError("Each opening needs a matching outside top perimeter.")

    outer_volumes = [
        _solid_loft(
            inlet_outer,
            top_z - overlap,
            opening.buffer(wall_thickness, join_style="round"),
            chamber_bottom + overlap,
            point_count,
            sections,
            1.0,
            preserve_corners=trace_inlet_corners,
        )
        for opening, inlet_outer in zip(
            openings, inlet_outer_polygons, strict=True
        )
    ]
    outer_volumes.append(
        _solid_loft(
            combined_outer,
            chamber_bottom,
            combined_outer,
            split_z + overlap,
            point_count,
            4,
            1.0,
            preserve_corners=False,
        )
    )
    inner_volumes = [
        _solid_loft(
            opening,
            top_z - overlap * 2.0,
            opening,
            chamber_bottom + chamber_height * 0.75,
            point_count,
            sections,
            1.0,
        )
        for opening in openings
    ]
    inner_volumes.append(
        _solid_loft(
            combined,
            chamber_bottom + chamber_height * 0.25,
            combined,
            split_z + overlap * 2.0,
            point_count,
            4,
            1.0,
            preserve_corners=False,
        )
    )
    outer = _boolean_union(outer_volumes, "the outside of the connector branches")
    inner = _boolean_union(inner_volumes, "the airflow passages")
    try:
        shell = trimesh.boolean.difference(
            [outer, inner], engine="manifold", check_volume=False
        )
    except Exception as exc:
        raise GeometryError(f"Could not hollow the connector branches: {exc}") from exc
    if shell is None:
        raise GeometryError("Could not hollow the connector branches.")
    if isinstance(shell, trimesh.Scene):
        shell = shell.to_mesh()
    shell.remove_unreferenced_vertices()
    if not shell.is_winding_consistent:
        shell.fix_normals()
    if not shell.is_watertight or mesh_component_count(shell) != 1:
        raise GeometryError("The connector branch manifold is not one watertight solid.")
    return shell, combined


def make_funnel(
    opening: Polygon,
    top_z: float,
    config: FunnelConfig,
    outlet_polygon: Polygon | None = None,
    inlet_outer_polygon: Polygon | None = None,
    preserve_opening_corners: bool = True,
    preserve_outlet_corners: bool = True,
) -> FunnelResult:
    if config.wall_thickness is None or config.wall_thickness <= 0:
        raise GeometryError("Wall thickness must be greater than zero.")
    if config.outlet_diameter <= 0:
        raise GeometryError("Fan hole diameter must be greater than zero.")
    if config.rounding_start < 0:
        raise GeometryError("Rounding start cannot be negative.")

    opening = orient(opening, sign=1.0)
    buffered = (
        orient(inlet_outer_polygon, sign=1.0)
        if inlet_outer_polygon is not None
        else opening.buffer(config.wall_thickness, join_style="round")
    )
    if not isinstance(buffered, Polygon):
        raise GeometryError("The selected opening cannot be offset into a funnel wall.")
    if not buffered.buffer(1e-5).contains(opening):
        raise GeometryError("The outside top perimeter does not contain the selected opening.")

    count = max(32, int(config.radial_segments))
    center_2d = np.asarray(opening.centroid.coords[0], dtype=float)
    centered_opening = translate_polygon(
        opening,
        xoff=-float(center_2d[0]),
        yoff=-float(center_2d[1]),
    )
    centered_outer = translate_polygon(
        buffered,
        xoff=-float(center_2d[0]),
        yoff=-float(center_2d[1]),
    )
    base_inner = _resample_boundary(
        centered_opening, count, preserve_corners=preserve_opening_corners
    )

    exact_outlet: Polygon | None = None
    if outlet_polygon is None:
        start_angle = atan2(base_inner[0, 1], base_inner[0, 0])
        angles = start_angle + np.arange(count, dtype=float) * 2.0 * pi / count
        outlet_inner = np.column_stack((np.cos(angles), np.sin(angles))) * (
            config.outlet_diameter / 2.0
        )
        outlet_radius = config.outlet_diameter / 2.0 + config.wall_thickness
    else:
        outlet_polygon = orient(outlet_polygon, sign=1.0)
        outlet_center_2d = np.asarray(outlet_polygon.centroid.coords[0], dtype=float)
        centered_outlet = translate_polygon(
            outlet_polygon,
            xoff=-float(outlet_center_2d[0]),
            yoff=-float(outlet_center_2d[1]),
        )
        exact_outlet = centered_outlet
        buffered_outlet = centered_outlet.buffer(
            config.wall_thickness, join_style="round"
        )
        if not isinstance(buffered_outlet, Polygon):
            raise GeometryError("The fan openings cannot be offset into a funnel wall.")
        outlet_inner = _resample_boundary(
            centered_outlet, count, preserve_corners=preserve_outlet_corners
        )
        outlet_outer_points = _resample_boundary(
            buffered_outlet, count, outlet_inner[0], preserve_corners=False
        )
        outlet_radius = float(np.max(np.linalg.norm(outlet_outer_points, axis=1)))

    base_center = np.array([center_2d[0], center_2d[1], top_z], dtype=float)
    identity = np.eye(3, dtype=float)
    warnings: list[str] = []

    if not config.curved:
        if config.length <= 0:
            raise GeometryError("Funnel length must be greater than zero.")
        segment_count = max(2, int(config.path_segments))
        centerline_length = sqrt(
            config.length**2 + config.offset_x**2 + config.offset_y**2
        )
        concave_inlet = centered_opening.area < centered_opening.convex_hull.area * 0.995
        rounding_start = config.rounding_start if concave_inlet else 0.0
        if concave_inlet and rounding_start >= centerline_length - 1.0:
            raise GeometryError(
                "Rounding must start at least 1 mm before the fan. Increase the funnel length or reduce Rounding starts at."
            )
        available_after_start = centerline_length - rounding_start
        smoothing_distance = (
            min(8.0, available_after_start * 0.5) if concave_inlet else 0.0
        )
        start_distance = rounding_start * config.length / centerline_length
        smoothing_end_distance = (
            (rounding_start + smoothing_distance)
            * config.length
            / centerline_length
        )
        distances = np.linspace(-config.join_overlap, config.length, segment_count + 1)
        if smoothing_distance > 1e-6:
            smoothing_steps = max(2, int(np.ceil(smoothing_distance / 0.2)))
            distances = np.unique(
                np.concatenate(
                    (
                        distances,
                        np.asarray([start_distance]),
                        np.linspace(
                            start_distance,
                            smoothing_end_distance,
                            smoothing_steps + 1,
                        ),
                    )
                )
            )
        shape_fractions = np.clip(distances / config.length, 0.0, 1.0)
        centers = [
            base_center
            + np.array(
                [
                    config.offset_x * fraction,
                    config.offset_y * fraction,
                    distance,
                ]
            )
            for distance, fraction in zip(distances, shape_fractions, strict=True)
        ]
        frames = [identity] * len(centers)
        outlet_center = base_center + np.array(
            [config.offset_x, config.offset_y, config.length], dtype=float
        )
        outlet_basis = identity
    else:
        if config.lead_in < 0 or config.lead_out < 0:
            raise GeometryError("Curve lead lengths cannot be negative.")
        component_magnitude = sqrt(config.angle_x**2 + config.angle_y**2)
        if component_magnitude > 165.0:
            raise GeometryError("The combined X/Y bend angle must be 165° or less.")
        theta = radians(component_magnitude)
        if component_magnitude < 1e-9:
            horizontal = np.array([1.0, 0.0, 0.0])
        else:
            horizontal = np.array(
                [config.angle_x / component_magnitude, config.angle_y / component_magnitude, 0.0]
            )
        axis = np.array([-horizontal[1], horizontal[0], 0.0], dtype=float)
        # "Arc diameter" is the free diameter on the inside of the elbow. This keeps
        # a value of zero physically printable and makes larger values progressively gentler.
        outer_radius = outlet_radius
        bend_radius = outer_radius + max(0.0, config.arc_diameter) / 2.0
        arc_length = bend_radius * theta
        total_length = config.lead_in + arc_length + config.lead_out
        if total_length <= 0:
            raise GeometryError("A curved funnel needs a bend angle or a lead length.")

        segment_count = max(8, int(config.path_segments))
        concave_inlet = centered_opening.area < centered_opening.convex_hull.area * 0.995
        rounding_start = config.rounding_start if concave_inlet else 0.0
        if concave_inlet and rounding_start >= total_length - 1.0:
            raise GeometryError(
                "Rounding must start at least 1 mm before the fan. Increase the curved path length or reduce Rounding starts at."
            )
        available_after_start = total_length - rounding_start
        smoothing_distance = (
            min(8.0, available_after_start * 0.5) if concave_inlet else 0.0
        )
        distances = np.linspace(-config.join_overlap, total_length, segment_count + 1)
        if smoothing_distance > 1e-6:
            smoothing_steps = max(2, int(np.ceil(smoothing_distance / 0.2)))
            distances = np.unique(
                np.concatenate(
                    (
                        distances,
                        np.asarray([rounding_start]),
                        np.linspace(
                            rounding_start,
                            rounding_start + smoothing_distance,
                            smoothing_steps + 1,
                        ),
                    )
                )
            )
        bend_start = base_center + np.array([0.0, 0.0, config.lead_in])
        bend_end = bend_start + bend_radius * (
            sin(theta) * np.array([0.0, 0.0, 1.0])
            + (1.0 - cos(theta)) * horizontal
        )
        final_direction = (
            cos(theta) * np.array([0.0, 0.0, 1.0]) + sin(theta) * horizontal
        )
        centers = []
        frames = []
        for distance in distances:
            if distance <= config.lead_in:
                center = base_center + np.array([0.0, 0.0, distance])
                local_angle = 0.0
            elif distance < config.lead_in + arc_length and theta > 0:
                local_angle = (distance - config.lead_in) / bend_radius
                center = bend_start + bend_radius * (
                    sin(local_angle) * np.array([0.0, 0.0, 1.0])
                    + (1.0 - cos(local_angle)) * horizontal
                )
            else:
                local_angle = theta
                center = bend_end + final_direction * (
                    distance - config.lead_in - arc_length
                )
            x_axis = _rodrigues(identity[:, 0], axis, local_angle)
            y_axis = _rodrigues(identity[:, 1], axis, local_angle)
            z_axis = _rodrigues(identity[:, 2], axis, local_angle)
            centers.append(center)
            frames.append(np.column_stack((x_axis, y_axis, z_axis)))
        shape_fractions = np.clip(distances / total_length, 0.0, 1.0)
        outlet_center = bend_end + final_direction * config.lead_out
        outlet_basis = frames[-1]
        centerline_length = total_length
        if component_magnitude > 120:
            warnings.append("Very large bend angles can be difficult to print without support.")

    path_length = centerline_length if not config.curved else total_length
    rounding_start_fraction = (
        rounding_start / centerline_length
        if not config.curved
        else rounding_start / total_length
    )
    smoothing_end_fraction = (
        (rounding_start + smoothing_distance)
        / (centerline_length if not config.curved else total_length)
        if smoothing_distance > 1e-9
        else rounding_start_fraction
    )
    min_x, min_y, max_x, max_y = centered_opening.bounds
    closing_radius = max(max_x - min_x, max_y - min_y)
    rounded_inlet = centered_opening.buffer(
        closing_radius, join_style="round"
    ).buffer(-closing_radius, join_style="round")
    if not isinstance(rounded_inlet, Polygon) or rounded_inlet.is_empty:
        raise GeometryError("The connector opening could not be rounded safely.")
    rounded_inlet = orient(rounded_inlet, sign=1.0)
    rounded_inlet_points = _resample_boundary(
        rounded_inlet,
        count,
        base_inner[0],
        preserve_corners=True,
    )
    inner_sections: list[Polygon] = []
    outer_sections: list[Polygon] = []
    for fraction in shape_fractions:
        fraction = float(np.clip(fraction, 0.0, 1.0))
        if exact_outlet is not None and fraction >= 1.0 - 1e-12:
            # Preserve the fan's exact hole contour at the join. Resampling that
            # last ring makes almost-coincident chords which Boolean into tiny
            # slivers and can become slicer warnings after STL conversion.
            inner_section = exact_outlet
        elif fraction <= rounding_start_fraction + 1e-12:
            inner_section = centered_opening
        elif smoothing_distance > 0.0 and fraction < smoothing_end_fraction:
            progress = (fraction - rounding_start_fraction) / (
                smoothing_end_fraction - rounding_start_fraction
            )
            if progress <= 1e-9:
                inner_section = centered_opening
            else:
                # Begin rounding on the first section, but ramp the offset so a
                # narrow slot closes across several nearby contours instead of
                # disappearing in one large jump.
                distance = closing_radius * progress**2
                inner_section = centered_opening.buffer(
                    distance, join_style="round"
                ).buffer(-distance, join_style="round")
                if not isinstance(inner_section, Polygon) or inner_section.is_empty:
                    raise GeometryError("The connector opening could not be rounded safely.")
        else:
            outlet_progress = (
                (fraction - smoothing_end_fraction) / (1.0 - smoothing_end_fraction)
                if smoothing_distance > 0.0
                else fraction
            )
            outlet_progress = float(np.clip(outlet_progress, 0.0, 1.0))
            outlet_progress = 1.0 - (1.0 - outlet_progress) ** 2
            inlet_points = rounded_inlet_points if smoothing_distance > 0.0 else base_inner
            inner_section = Polygon(
                inlet_points * (1.0 - outlet_progress)
                + outlet_inner * outlet_progress
            )
        inner_section = orient(inner_section, sign=1.0)
        if not inner_section.is_valid:
            raise GeometryError(
                "The funnel airflow path intersects itself. Increase the funnel length or use gentler settings."
            )
        outer_section = (
            centered_outer
            if fraction <= rounding_start_fraction + 1e-12
            else inner_section.buffer(config.wall_thickness, join_style="round")
        )
        if not isinstance(outer_section, Polygon) or not outer_section.is_valid:
            raise GeometryError("The funnel wall could not be offset from its airflow path.")
        if not outer_section.buffer(1e-7).covers(inner_section):
            raise GeometryError("The funnel wall does not contain its airflow path.")
        inner_sections.append(inner_section)
        outer_sections.append(orient(outer_section, sign=1.0))

    remaining_after_rounding = path_length * (1.0 - smoothing_end_fraction)
    if concave_inlet and remaining_after_rounding < 8.0:
        warnings.append(
            f"Only {remaining_after_rounding:.1f} mm remains after inlet rounding; increase the path length for a gentler fan transition."
        )

    outer_solid = _solid_from_cross_sections(
        outer_sections, centers, frames, "The outside of the funnel"
    )
    airway_extension = max(1.0, config.wall_thickness)
    inner_centers = [
        centers[0] - frames[0][:, 2] * airway_extension,
        *centers,
        centers[-1] + frames[-1][:, 2] * airway_extension,
    ]
    inner_frames = [frames[0], *frames, frames[-1]]
    inner_solid = _solid_from_cross_sections(
        [inner_sections[0], *inner_sections, inner_sections[-1]],
        inner_centers,
        inner_frames,
        "The funnel airflow passage",
    )
    try:
        mesh = trimesh.boolean.difference(
            [outer_solid, inner_solid], engine="manifold", check_volume=False
        )
    except Exception as exc:
        raise GeometryError(f"Could not hollow the funnel: {exc}") from exc
    if mesh is None:
        raise GeometryError("Could not hollow the funnel.")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.to_mesh()
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent:
        mesh.fix_normals()
    if not mesh.is_watertight or mesh_component_count(mesh) != 1:
        raise GeometryError("The generated funnel is not one watertight solid.")
    return FunnelResult(mesh, outlet_center, outlet_basis, centerline_length, warnings)


def make_custom_fan(config: FanConfig) -> trimesh.Trimesh:
    if config.size <= 0 or config.thickness <= 0:
        raise GeometryError("Fan size and plate thickness must be greater than zero.")
    if config.hole_diameter <= 0 or config.hole_diameter >= config.size:
        raise GeometryError("The fan opening must be smaller than the fan plate.")
    if config.screw_hole_diameter <= 0:
        raise GeometryError("Screw hole diameter must be greater than zero.")

    half = config.size / 2.0
    radius = min(max(config.corner_radius, 0.0), half - 0.1)
    if radius > 0:
        outer = box(-half + radius, -half + radius, half - radius, half - radius).buffer(
            radius, quad_segs=12
        )
    else:
        outer = box(-half, -half, half, half)

    cuts = [Point(0.0, 0.0).buffer(config.hole_diameter / 2.0, quad_segs=48)]
    mount = config.screw_spacing / 2.0
    for x in (-mount, mount):
        for y in (-mount, mount):
            cuts.append(Point(x, y).buffer(config.screw_hole_diameter / 2.0, quad_segs=16))
    plate = outer
    for cut in cuts:
        plate = plate.difference(cut)
    if not isinstance(plate, Polygon):
        raise GeometryError("Those fan settings do not leave a connected mounting plate.")

    mesh = trimesh.creation.extrude_polygon(plate, height=config.thickness, engine="earcut")
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent:
        mesh.fix_normals()
    if not mesh.is_watertight:
        raise GeometryError("The custom fan connector could not be made watertight.")
    return mesh


def place_fan(
    mesh: trimesh.Trimesh,
    local_hole_center: np.ndarray,
    local_z_min: float,
    funnel: FunnelResult,
    overlap: float,
) -> trimesh.Trimesh:
    placed = mesh.copy()
    local = placed.vertices.copy()
    local[:, 0] -= float(local_hole_center[0])
    local[:, 1] -= float(local_hole_center[1])
    local[:, 2] -= float(local_z_min) + overlap
    placed.vertices = funnel.outlet_center + local @ funnel.outlet_basis.T
    placed.remove_unreferenced_vertices()
    return placed


def build_assembly_parts(
    connector: ConnectorAnalysis,
    funnel_config: FunnelConfig,
    fan_config: FanConfig | None = None,
    imported_fan: FanAnalysis | None = None,
    stack_config: ConnectorStackConfig | None = None,
    fan_stack_config: FanStackConfig | None = None,
) -> AssemblyParts:
    if (fan_config is None) == (imported_fan is None):
        raise GeometryError("Choose either a custom fan connector or an imported fan STL.")

    if imported_fan is not None:
        outlet_diameter = imported_fan.hole_diameter
        local_fan = imported_fan.mesh.copy()
        local_hole_center = imported_fan.hole_center
        local_z_min = imported_fan.z_min
        fan_opening = translate_polygon(
            imported_fan.hole_polygon,
            xoff=-float(local_hole_center[0]),
            yoff=-float(local_hole_center[1]),
        )
    else:
        assert fan_config is not None
        outlet_diameter = fan_config.hole_diameter
        local_fan = make_custom_fan(fan_config)
        local_hole_center = np.array([0.0, 0.0], dtype=float)
        local_z_min = 0.0
        fan_opening = Point(0.0, 0.0).buffer(
            fan_config.hole_diameter / 2.0, quad_segs=48
        )

    local_vertices = local_fan.vertices.copy()
    local_vertices[:, 0] -= float(local_hole_center[0])
    local_vertices[:, 1] -= float(local_hole_center[1])
    local_vertices[:, 2] -= float(local_z_min)
    local_fan.vertices = local_vertices
    local_fan.remove_unreferenced_vertices()

    wall_thickness = (
        connector.opening.wall_thickness
        if funnel_config.wall_thickness is None
        else funnel_config.wall_thickness
    )
    working_config = replace(
        funnel_config,
        outlet_diameter=outlet_diameter,
        wall_thickness=wall_thickness,
    )

    stack = stack_config or ConnectorStackConfig()
    _validate_stack(stack.count, 10, stack.axis, stack.spacing, "GPU connector")
    if stack.bridge_mode not in {"unbridged", "full", "front", "back"}:
        raise GeometryError(f"Unknown GPU connector bridge mode: {stack.bridge_mode}.")
    if stack.bridge_mode in {"front", "back"} and stack.bridge_thickness <= 0:
        raise GeometryError("GPU bridge thickness must be greater than zero.")

    fan_stack = fan_stack_config or FanStackConfig()
    _validate_stack(fan_stack.count, 4, fan_stack.axis, fan_stack.spacing, "Fan")

    effective_overlap = (
        max(1.0, working_config.join_overlap)
        if stack.count > 1 or fan_stack.count > 1
        else working_config.join_overlap
    )
    working_config = replace(working_config, join_overlap=effective_overlap)

    translations = _stack_translations(
        connector.mesh, stack.count, stack.axis, stack.spacing
    )
    gpu_meshes = []
    openings = []
    outer_openings = []
    for translation in translations:
        gpu = connector.mesh.copy()
        gpu.apply_translation(translation)
        gpu_meshes.append(gpu)
        openings.append(
            translate_polygon(
                connector.opening.polygon,
                xoff=float(translation[0]),
                yoff=float(translation[1]),
            )
        )
        outer_openings.append(
            translate_polygon(
                connector.outer_polygon,
                xoff=float(translation[0]),
                yoff=float(translation[1]),
            )
        )
    gpu_mesh = _bridge_stacked_meshes(
        gpu_meshes,
        connector.mesh.bounds,
        translations,
        stack.axis,
        stack.bridge_mode,
        stack.bridge_thickness,
        effective_overlap,
        "GPU connector",
    )

    fan_translations = _stack_translations(
        local_fan, fan_stack.count, fan_stack.axis, fan_stack.spacing
    )
    local_fan_meshes: list[trimesh.Trimesh] = []
    fan_openings: list[Polygon] = []
    for translation in fan_translations:
        fan_copy = local_fan.copy()
        fan_copy.apply_translation(translation)
        local_fan_meshes.append(fan_copy)
        fan_openings.append(
            translate_polygon(
                fan_opening,
                xoff=float(translation[0]),
                yoff=float(translation[1]),
            )
        )
    local_fan_assembly = _bridge_stacked_meshes(
        local_fan_meshes,
        local_fan.bounds,
        fan_translations,
        fan_stack.axis,
        "full" if fan_stack.bridged else "unbridged",
        float(local_fan.extents[2]),
        effective_overlap,
        "Fan connector",
    )
    outlet_opening = (
        fan_openings[0]
        if fan_stack.count == 1
        else _combined_opening(fan_openings, "fan")
    )

    funnel_meshes: list[trimesh.Trimesh] = []
    main_opening = openings[0]
    main_start_z = connector.top_z
    gpu_split_length = 0.0
    if stack.count == 1:
        pass
    else:
        if working_config.split_distance <= 0:
            raise GeometryError("GPU split distance must be greater than zero.")
        gpu_split_length = working_config.split_distance
        main_start_z = connector.top_z + gpu_split_length
        gpu_branches, main_opening = _make_branch_manifold(
            openings,
            connector.top_z,
            main_start_z,
            working_config.wall_thickness,
            max(32, int(working_config.radial_segments)),
            max(8, int(working_config.path_segments)),
            effective_overlap,
            inlet_outer_polygons=outer_openings,
        )
        funnel_meshes.append(gpu_branches)

    main_funnel = make_funnel(
        main_opening,
        main_start_z,
        working_config,
        outlet_polygon=outlet_opening,
        inlet_outer_polygon=outer_openings[0] if stack.count == 1 else None,
        preserve_opening_corners=stack.count == 1,
        preserve_outlet_corners=fan_stack.count == 1,
    )
    funnel_meshes.append(main_funnel.mesh)

    fan_split_length = 0.0
    if fan_stack.count > 1:
        if working_config.fan_split_distance <= 0:
            raise GeometryError("Fan split distance must be greater than zero.")
        fan_split_length = working_config.fan_split_distance
        fan_branches_local, _ = _make_branch_manifold(
            fan_openings,
            0.0,
            fan_split_length,
            working_config.wall_thickness,
            max(32, int(working_config.radial_segments)),
            max(8, int(working_config.path_segments)),
            effective_overlap,
        )
        fan_branches_local = _reverse_branch(fan_branches_local, fan_split_length)
        fan_branches = _place_local_mesh(
            fan_branches_local,
            main_funnel.outlet_center,
            main_funnel.outlet_basis,
        )
        funnel_meshes.append(fan_branches)

    funnel_mesh = (
        funnel_meshes[0]
        if len(funnel_meshes) == 1
        else _boolean_union(
            funnel_meshes, "the GPU branches, main funnel, and fan branches"
        )
    )
    funnel_mesh.remove_unreferenced_vertices()
    if not funnel_mesh.is_winding_consistent:
        funnel_mesh.fix_normals()
    if not funnel_mesh.is_watertight or mesh_component_count(funnel_mesh) != 1:
        raise GeometryError("The complete funnel is not one watertight solid.")

    fan_mesh = _place_local_mesh(
        local_fan_assembly,
        main_funnel.outlet_center,
        main_funnel.outlet_basis,
        np.array(
            [0.0, 0.0, fan_split_length - effective_overlap], dtype=float
        ),
    )
    final_outlet_center = main_funnel.outlet_center + (
        main_funnel.outlet_basis
        @ np.array([0.0, 0.0, fan_split_length], dtype=float)
    )
    funnel = FunnelResult(
        funnel_mesh,
        final_outlet_center,
        main_funnel.outlet_basis,
        gpu_split_length + main_funnel.centerline_length + fan_split_length,
        main_funnel.warnings,
    )
    return AssemblyParts(gpu_mesh, funnel_mesh, fan_mesh, funnel)


def union_assembly(parts: AssemblyParts) -> trimesh.Trimesh:
    try:
        result = trimesh.boolean.union(parts.meshes, engine="manifold", check_volume=False)
    except Exception as exc:  # pragma: no cover - backend messages vary
        raise GeometryError(f"The assembly parts could not be fused: {exc}") from exc
    if result is None:
        raise GeometryError("The assembly parts could not be fused into one solid.")
    if isinstance(result, trimesh.Scene):
        result = result.to_mesh()
    result = _prepare_for_stl(result)
    if not result.is_winding_consistent:
        result.fix_normals()
    if not result.is_watertight:
        raise GeometryError("The fused assembly is not watertight and was not exported.")
    if mesh_component_count(result) != 1:
        raise GeometryError(
            "The assembly contains disconnected solids. Increase contact or check the input STLs."
        )
    return result


def mesh_component_count(mesh: trimesh.Trimesh) -> int:
    """Count face-connected bodies without optional scipy/networkx dependencies."""
    face_count = len(mesh.faces)
    if face_count == 0:
        return 0
    parents = list(range(face_count))
    ranks = [0] * face_count

    def find(item: int) -> int:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if ranks[left_root] < ranks[right_root]:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        if ranks[left_root] == ranks[right_root]:
            ranks[left_root] += 1

    first_face_for_vertex: dict[int, int] = {}
    for face_index, face in enumerate(mesh.faces):
        for vertex in face:
            vertex_index = int(vertex)
            other = first_face_for_vertex.setdefault(vertex_index, face_index)
            union(face_index, other)
    return len({find(index) for index in range(face_count)})


def export_stl(parts: AssemblyParts, path: str | Path) -> trimesh.Trimesh:
    result = union_assembly(parts)
    payload = result.export(file_type="stl")
    verified = trimesh.load(
        file_obj=BytesIO(payload),
        file_type="stl",
        force="mesh",
        process=True,
    )
    if isinstance(verified, trimesh.Scene):
        verified = verified.to_mesh()
    if (
        not isinstance(verified, trimesh.Trimesh)
        or not verified.is_watertight
        or mesh_component_count(verified) != 1
        or np.any(verified.area_faces < 1e-10)
    ):
        raise GeometryError("The STL changed during serialization and was not saved.")
    Path(path).write_bytes(payload)
    return verified
