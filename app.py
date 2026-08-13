from __future__ import annotations

import json
import sys
from pathlib import Path


def resource_path(relative: str) -> Path:
    """Resolve bundled data for frozen builds and source trees."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parent / relative


def self_test(report_path: Path) -> int:
    """Exercise packaged STL loading, generation, booleans, and native extensions."""
    report: dict[str, object]
    try:
        # Keep this path free of PySide6/OpenGL so frozen --self-test can run headless.
        # Import inside the guarded block so frozen-build import failures reach the
        # JSON report instead of disappearing behind a windowed executable.
        from shroud_designer.geometry import (
            ConnectorStackConfig,
            FanConfig,
            FanStackConfig,
            FunnelConfig,
            analyze_connector,
            analyze_fan,
            build_assembly_parts,
            mesh_component_count,
            union_assembly,
        )

        connector = analyze_connector(
            resource_path("GPU Connectors/cmp front.stl")
        )
        reference_fan = analyze_fan(
            resource_path("Fans/default 120mm fan for shroud.stl")
        )
        straight = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(length=50.0, offset_x=12.0, offset_y=-7.0),
                fan_config=FanConfig(),
            )
        )
        reference_fan.rotation_angle = 15.0
        curved = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(
                    curved=True,
                    angle_x=30.0,
                    angle_y=-20.0,
                    lead_in=25.0,
                    lead_out=25.0,
                    arc_diameter=60.0,
                ),
                imported_fan=reference_fan,
            )
        )
        v03_compatibility = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(
                    length=40.0,
                    offset_x=8.0,
                    legacy_v03=True,
                ),
                fan_config=FanConfig(),
            )
        )
        stacked = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(length=40.0, split_distance=20.0),
                fan_config=FanConfig(),
                stack_config=ConnectorStackConfig(count=4, axis="y", spacing=5.0),
            )
        )
        multi_fan = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(
                    length=35.0,
                    fan_split_distance=20.0,
                    radial_segments=64,
                    path_segments=24,
                ),
                fan_config=FanConfig(),
                fan_stack_config=FanStackConfig(
                    count=4, axis="x", spacing=5.0, bridged=True
                ),
            )
        )
        bridged_arrays = union_assembly(
            build_assembly_parts(
                connector,
                FunnelConfig(
                    length=30.0,
                    split_distance=15.0,
                    fan_split_distance=20.0,
                    radial_segments=64,
                    path_segments=24,
                ),
                fan_config=FanConfig(),
                stack_config=ConnectorStackConfig(
                    count=3,
                    axis="y",
                    spacing=5.0,
                    bridge_mode="front",
                    bridge_thickness=5.0,
                ),
                fan_stack_config=FanStackConfig(
                    count=2, axis="x", spacing=5.0, bridged=True
                ),
            )
        )
        report = {
            "ok": True,
            "connector_opening_mm": [
                round(connector.opening.width, 3),
                round(connector.opening.depth, 3),
            ],
            "fan_opening_mm": round(reference_fan.hole_diameter, 3),
            "straight": {
                "watertight": bool(straight.is_watertight),
                "components": mesh_component_count(straight),
                "triangles": len(straight.faces),
            },
            "curved": {
                "imported_fan_outline": bool(reference_fan.use_outer_boundary),
                "imported_fan_rotation_degrees": reference_fan.rotation_angle,
                "watertight": bool(curved.is_watertight),
                "components": mesh_component_count(curved),
                "triangles": len(curved.faces),
            },
            "v03_compatibility": {
                "watertight": bool(v03_compatibility.is_watertight),
                "components": mesh_component_count(v03_compatibility),
                "triangles": len(v03_compatibility.faces),
            },
            "stacked": {
                "connector_count": 4,
                "watertight": bool(stacked.is_watertight),
                "components": mesh_component_count(stacked),
                "triangles": len(stacked.faces),
            },
            "multi_fan": {
                "fan_count": 4,
                "watertight": bool(multi_fan.is_watertight),
                "components": mesh_component_count(multi_fan),
                "triangles": len(multi_fan.faces),
            },
            "bridged_arrays": {
                "connector_count": 3,
                "fan_count": 2,
                "watertight": bool(bridged_arrays.is_watertight),
                "components": mesh_component_count(bridged_arrays),
                "triangles": len(bridged_arrays.faces),
            },
        }
        code = 0
    except Exception as exc:  # pragma: no cover - only used against packaged build
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        code = 1
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return code


def _prepare_linux_gl() -> None:
    """Prefer desktop GLX over EGL so QOpenGLWidget works with NVIDIA/X11."""
    import os

    if sys.platform != "linux":
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("QT_XCB_GL_INTEGRATION", "xcb_glx")
    os.environ.setdefault("QT_OPENGL", "desktop")
    os.environ.setdefault("PYOPENGL_PLATFORM", "glx")


def main() -> int:
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        output = (
            Path(sys.argv[index + 1])
            if index + 1 < len(sys.argv)
            else Path.cwd() / "shroud-designer-self-test.json"
        )
        return self_test(output)

    _prepare_linux_gl()

    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    from shroud_designer.ui import MainWindow, app_icon_path, app_stylesheet

    surface = QSurfaceFormat()
    surface.setVersion(2, 1)
    surface.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    surface.setSamples(4)
    surface.setDepthBufferSize(24)
    QSurfaceFormat.setDefaultFormat(surface)

    app = QApplication(sys.argv)
    app.setApplicationName("Shroud Designer")
    app.setOrganizationName("ShroudDesigner")
    app.setStyle("Fusion")
    app.setStyleSheet(app_stylesheet())
    icon = app_icon_path()
    if icon.exists():
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
