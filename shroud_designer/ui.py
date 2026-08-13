from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QIcon, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .geometry import (
    AssemblyParts,
    ConnectorAnalysis,
    ConnectorStackConfig,
    FanAnalysis,
    FanConfig,
    FanStackConfig,
    FunnelConfig,
    GeometryError,
    analyze_connector,
    analyze_fan,
    build_assembly_parts,
    export_stl,
)
from .preview import ModelPreview, RenderMesh


def resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parents[1] / relative


def app_icon_path() -> Path:
    """Prefer PNG on non-Windows platforms; fall back to ICO."""
    png = resource_path("assets/shroud-designer.png")
    ico = resource_path("assets/shroud-designer.ico")
    if sys.platform != "win32" and png.exists():
        return png
    if ico.exists():
        return ico
    return png


class _PanelComboBox(QComboBox):
    """Let the control-panel scroll area own the wheel unless the popup is open."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _PanelSpinBox(QSpinBox):
    """Prevent accidental integer changes while scrolling the control panel."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _PanelDoubleSpinBox(QDoubleSpinBox):
    """Prevent accidental decimal changes while scrolling the control panel."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


def _spin(
    minimum: float,
    maximum: float,
    value: float,
    suffix: str = " mm",
    decimals: int = 1,
    step: float = 1.0,
) -> QDoubleSpinBox:
    spin = _PanelDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setSuffix(suffix)
    spin.setKeyboardTracking(False)
    return spin


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Shroud Designer 0.3")
        icon = app_icon_path()
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1380, 840)
        self.setMinimumSize(1000, 680)

        self.settings = QSettings("ShroudDesigner", "ShroudDesigner")
        self.connector: ConnectorAnalysis | None = None
        self.imported_fan: FanAnalysis | None = None
        self.last_parts: AssemblyParts | None = None
        self._has_fitted_assembly = False
        self._building = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(240)
        self._preview_timer.timeout.connect(self.rebuild_preview)

        self._build_ui()
        self._connect_signals()
        self._update_mode_controls()
        QTimer.singleShot(0, self._load_initial_connector)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Shroud Designer")
        title.setObjectName("appTitle")
        subtitle = QLabel("GPU connector array → airtight transition → quiet fan array")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_row.addLayout(title_box)
        header_row.addStretch()
        version = QLabel("VERSION 0.3")
        version.setObjectName("versionBadge")
        header_row.addWidget(version, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(header_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_control_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 950])
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_control_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(360)
        scroll.setMaximumWidth(480)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_gpu_group())
        layout.addWidget(self._build_fan_group())
        layout.addWidget(self._build_funnel_group())

        self.export_button = QPushButton("Save print-ready STL…")
        self.export_button.setObjectName("primaryButton")
        self.export_button.setMinimumHeight(46)
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)
        export_note = QLabel("Fuses every connector, branch, funnel, and fan plate into one verified solid.")
        export_note.setWordWrap(True)
        export_note.setObjectName("helpText")
        layout.addWidget(export_note)
        layout.addStretch()
        scroll.setWidget(panel)
        return scroll

    def _build_gpu_group(self) -> QGroupBox:
        group = QGroupBox("1   GPU CONNECTOR")
        layout = QVBoxLayout(group)
        file_row = QHBoxLayout()
        self.gpu_path = QLineEdit()
        self.gpu_path.setReadOnly(True)
        self.gpu_path.setPlaceholderText("Choose an upright STL…")
        self.gpu_browse = QPushButton("Browse…")
        file_row.addWidget(self.gpu_path, 1)
        file_row.addWidget(self.gpu_browse)
        layout.addLayout(file_row)

        self.gpu_info = QLabel("No connector loaded")
        self.gpu_info.setObjectName("infoPill")
        self.gpu_info.setWordWrap(True)
        layout.addWidget(self.gpu_info)

        opening_form = QFormLayout()
        self.opening_combo = _PanelComboBox()
        self.opening_combo.setEnabled(False)
        self.opening_combo.setToolTip(
            "Auto selects the largest enclosed loop just below the model's highest Z layer."
        )
        opening_form.addRow("Top opening", self.opening_combo)
        self.connector_count = _PanelSpinBox()
        self.connector_count.setRange(1, 10)
        self.connector_count.setValue(1)
        self.connector_count.setKeyboardTracking(False)
        self.stack_axis = _PanelComboBox()
        self.stack_axis.addItem("Y axis", "y")
        self.stack_axis.addItem("X axis", "x")
        self.connector_spacing = _spin(0.0, 500.0, 10.0, decimals=1, step=5.0)
        self.connector_spacing.setToolTip(
            "Clear gap between the outside bounding boxes of adjacent connector copies."
        )
        self.gpu_bridge = _PanelComboBox()
        self.gpu_bridge.addItem("Unbridged", "unbridged")
        self.gpu_bridge.addItem("Full length", "full")
        self.gpu_bridge.addItem("Front (funnel end)", "front")
        self.gpu_bridge.addItem("Back", "back")
        self.gpu_bridge_thickness = _spin(
            0.5, 500.0, 5.0, decimals=1, step=1.0
        )
        self.gpu_bridge_thickness.setToolTip(
            "Z depth of a front or back bridge. Front is the maximum-Z funnel end."
        )
        opening_form.addRow("Connector count", self.connector_count)
        opening_form.addRow("Stack along", self.stack_axis)
        opening_form.addRow("Clear spacing", self.connector_spacing)
        opening_form.addRow("Bridge", self.gpu_bridge)
        opening_form.addRow("Bridge thickness", self.gpu_bridge_thickness)
        layout.addLayout(opening_form)
        return group

    def _build_fan_group(self) -> QGroupBox:
        group = QGroupBox("2   FAN CONNECTOR")
        layout = QVBoxLayout(group)
        mode_form = QFormLayout()
        self.fan_mode = _PanelComboBox()
        self.fan_mode.addItem("Custom fan plate", "custom")
        self.fan_mode.addItem("Import fan STL", "import")
        mode_form.addRow("Source", self.fan_mode)
        self.fan_count = _PanelSpinBox()
        self.fan_count.setRange(1, 4)
        self.fan_count.setValue(1)
        self.fan_count.setKeyboardTracking(False)
        self.fan_stack_axis = _PanelComboBox()
        self.fan_stack_axis.addItem("Y axis", "y")
        self.fan_stack_axis.addItem("X axis", "x")
        self.fan_spacing = _spin(0.0, 500.0, 10.0, decimals=1, step=5.0)
        self.fan_spacing.setToolTip(
            "Clear gap between the outside bounding boxes of adjacent fan plates."
        )
        self.fan_bridge = _PanelComboBox()
        self.fan_bridge.addItem("Unbridged", False)
        self.fan_bridge.addItem("Fully bridged", True)
        mode_form.addRow("Fan count", self.fan_count)
        mode_form.addRow("Stack along", self.fan_stack_axis)
        mode_form.addRow("Clear spacing", self.fan_spacing)
        mode_form.addRow("Bridge", self.fan_bridge)
        layout.addLayout(mode_form)

        self.fan_file_widget = QWidget()
        fan_file_row = QHBoxLayout(self.fan_file_widget)
        fan_file_row.setContentsMargins(0, 0, 0, 0)
        self.fan_path = QLineEdit()
        self.fan_path.setReadOnly(True)
        self.fan_path.setPlaceholderText("Choose a fan connector STL…")
        self.fan_browse = QPushButton("Browse…")
        fan_file_row.addWidget(self.fan_path, 1)
        fan_file_row.addWidget(self.fan_browse)
        layout.addWidget(self.fan_file_widget)

        self.custom_fan_widget = QWidget()
        custom_form = QFormLayout(self.custom_fan_widget)
        custom_form.setContentsMargins(0, 0, 0, 0)
        self.fan_size = _PanelComboBox()
        self.fan_size.addItem("120 mm", 120.0)
        self.fan_size.addItem("140 mm", 140.0)
        self.fan_hole = _spin(20.0, 200.0, 116.0, decimals=1, step=1.0)
        self.screw_hole = _spin(1.0, 12.0, 4.6, decimals=1, step=0.1)
        custom_form.addRow("Fan size", self.fan_size)
        custom_form.addRow("Air opening", self.fan_hole)
        custom_form.addRow("Screw holes", self.screw_hole)
        layout.addWidget(self.custom_fan_widget)

        self.fan_info = QLabel("105 mm mounting pattern • 3 mm plate")
        self.fan_info.setObjectName("infoPill")
        self.fan_info.setWordWrap(True)
        layout.addWidget(self.fan_info)
        return group

    def _build_funnel_group(self) -> QGroupBox:
        group = QGroupBox("3   FUNNEL")
        layout = QVBoxLayout(group)
        common = QFormLayout()
        self.wall_info = QLabel("Detected from the GPU connector top rim")
        self.wall_info.setObjectName("infoPill")
        self.wall_info.setWordWrap(True)
        self.wall_info.setToolTip(
            "The funnel traces the connector's inner and outer top perimeters, then continues at the detected rim thickness."
        )
        self.funnel_mode = _PanelComboBox()
        self.funnel_mode.addItem("Straight / offset", False)
        self.funnel_mode.addItem("Compound curve", True)
        self.rounding_start = _spin(0.0, 500.0, 8.0, decimals=1, step=1.0)
        self.rounding_start.setToolTip(
            "Height above the GPU connector that retains its exact top profile before smoothing begins. Use more clearance around power-cable openings; 0 mm rounds immediately."
        )
        common.addRow("Wall thickness", self.wall_info)
        common.addRow("Path", self.funnel_mode)
        common.addRow("Rounding starts at", self.rounding_start)
        layout.addLayout(common)

        self.split_widget = QGroupBox("Split settings")
        split_form = QFormLayout(self.split_widget)
        self.split_distance = _spin(1.0, 500.0, 20.0, decimals=1, step=5.0)
        self.split_distance.setToolTip(
            "Distance from the GPU openings to the shared collector. Each connector keeps a separate duct up to this height."
        )
        self.gpu_split_label = QLabel("GPU split distance")
        split_form.addRow(self.gpu_split_label, self.split_distance)
        self.fan_split_distance = _spin(1.0, 500.0, 20.0, decimals=1, step=5.0)
        self.fan_split_distance.setToolTip(
            "Distance from the shared funnel to the fan openings. Each fan keeps a separate duct over this distance."
        )
        self.fan_split_label = QLabel("Fan split distance")
        split_form.addRow(self.fan_split_label, self.fan_split_distance)
        layout.addWidget(self.split_widget)

        self.straight_widget = QGroupBox("Straight settings")
        straight = QFormLayout(self.straight_widget)
        self.length = _spin(1.0, 1000.0, 50.0, decimals=1, step=5.0)
        self.offset_x = _spin(-500.0, 500.0, 0.0, decimals=1, step=5.0)
        self.offset_y = _spin(-500.0, 500.0, 0.0, decimals=1, step=5.0)
        straight.addRow("Length", self.length)
        straight.addRow("X offset", self.offset_x)
        straight.addRow("Y offset", self.offset_y)
        layout.addWidget(self.straight_widget)

        self.curve_widget = QGroupBox("Curve settings")
        curve = QFormLayout(self.curve_widget)
        self.angle_x = _spin(-120.0, 120.0, 0.0, "°", decimals=1, step=5.0)
        self.angle_y = _spin(-120.0, 120.0, 0.0, "°", decimals=1, step=5.0)
        self.lead_in = _spin(0.0, 500.0, 25.0, decimals=1, step=5.0)
        self.lead_out = _spin(0.0, 500.0, 25.0, decimals=1, step=5.0)
        self.arc_diameter = _spin(0.0, 500.0, 60.0, decimals=1, step=5.0)
        self.angle_x.setToolTip("Positive bends toward +X; negative bends toward -X.")
        self.angle_y.setToolTip("Positive bends toward +Y; negative bends toward -Y.")
        self.arc_diameter.setToolTip(
            "Free diameter on the inside of the elbow. Larger values make a gentler, wider curve."
        )
        curve.addRow("X bend", self.angle_x)
        curve.addRow("Y bend", self.angle_y)
        curve.addRow("Lead in", self.lead_in)
        curve.addRow("Lead out", self.lead_out)
        curve.addRow("Arc diameter", self.arc_diameter)
        layout.addWidget(self.curve_widget)
        return group

    def _build_preview_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("previewFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        preview_header = QWidget()
        preview_header.setObjectName("previewHeader")
        header_layout = QHBoxLayout(preview_header)
        header_layout.setContentsMargins(14, 9, 12, 9)
        heading = QLabel("3D PREVIEW")
        heading.setObjectName("previewTitle")
        controls = QLabel("Wheel: zoom   •   Left drag: rotate   •   Right drag: move   •   Double-click: fit")
        controls.setObjectName("previewHelp")
        self.fit_button = QPushButton("Fit view")
        self.fit_button.setObjectName("smallButton")
        header_layout.addWidget(heading)
        header_layout.addStretch()
        header_layout.addWidget(controls)
        header_layout.addSpacing(8)
        header_layout.addWidget(self.fit_button)
        layout.addWidget(preview_header)

        self.preview = ModelPreview()
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.preview, 1)
        self.preview_status = QLabel("Load a GPU connector STL to begin.")
        self.preview_status.setObjectName("previewStatus")
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        return frame

    def _connect_signals(self) -> None:
        self.gpu_browse.clicked.connect(self.choose_gpu)
        self.fan_browse.clicked.connect(self.choose_fan)
        self.export_button.clicked.connect(self.save_stl)
        self.fit_button.clicked.connect(self.preview.fit_view)
        self.opening_combo.currentIndexChanged.connect(self._opening_changed)
        self.fan_mode.currentIndexChanged.connect(self._fan_mode_changed)
        self.fan_size.currentIndexChanged.connect(self._fan_size_changed)
        self.funnel_mode.currentIndexChanged.connect(self._funnel_mode_changed)
        self.connector_count.valueChanged.connect(self._connector_layout_changed)
        self.stack_axis.currentIndexChanged.connect(self._connector_layout_changed)
        self.connector_spacing.valueChanged.connect(self._connector_layout_changed)
        self.gpu_bridge.currentIndexChanged.connect(self._connector_layout_changed)
        self.fan_count.valueChanged.connect(self._fan_layout_changed)
        self.fan_stack_axis.currentIndexChanged.connect(self._fan_layout_changed)
        self.fan_spacing.valueChanged.connect(self._fan_layout_changed)
        self.fan_bridge.currentIndexChanged.connect(self._fan_layout_changed)
        for control in (
            self.fan_hole,
            self.screw_hole,
            self.rounding_start,
            self.length,
            self.offset_x,
            self.offset_y,
            self.angle_x,
            self.angle_y,
            self.lead_in,
            self.lead_out,
            self.arc_diameter,
            self.gpu_bridge_thickness,
            self.split_distance,
            self.fan_split_distance,
        ):
            control.valueChanged.connect(self.schedule_preview)

    def _load_initial_connector(self) -> None:
        saved = Path(str(self.settings.value("gpu_path", "")))
        default = resource_path("GPU Connectors/cmp front.stl")
        self.load_connector(saved if saved.is_file() else default)

    def _last_folder(self) -> str:
        value = Path(str(self.settings.value("last_folder", Path.home())))
        return str(value if value.exists() else Path.home())

    def choose_gpu(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GPU connector", self._last_folder(), "STL models (*.stl)"
        )
        if path:
            self.load_connector(Path(path))

    def load_connector(self, path: Path) -> None:
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            connector = analyze_connector(path)
        except GeometryError as exc:
            QMessageBox.critical(self, "GPU connector could not be loaded", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.connector = connector
        self._has_fitted_assembly = False
        self.gpu_path.setText(str(path))
        self.gpu_path.setToolTip(str(path))
        opening = connector.opening
        self._update_gpu_info()
        self.opening_combo.blockSignals(True)
        self.opening_combo.clear()
        self.opening_combo.addItem(f"Auto — {connector.candidates[0].label}", 0)
        if len(connector.candidates) > 1:
            for index, candidate in enumerate(connector.candidates):
                self.opening_combo.addItem(f"Opening {index + 1} — {candidate.label}", index)
        self.opening_combo.setEnabled(len(connector.candidates) > 1)
        self.opening_combo.blockSignals(False)
        self.settings.setValue("gpu_path", str(path))
        self.settings.setValue("last_folder", str(path.parent))
        self.preview.set_meshes(
            [RenderMesh(connector.mesh, (0.28, 0.43, 0.62, 1.0))], fit=True
        )
        self.schedule_preview()

    def choose_fan(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select fan connector", self._last_folder(), "STL models (*.stl)"
        )
        if path:
            self.load_fan(Path(path))

    def load_fan(self, path: Path) -> None:
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            analysis = analyze_fan(path)
        except GeometryError as exc:
            QMessageBox.critical(self, "Fan connector could not be loaded", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.imported_fan = analysis
        self.fan_path.setText(str(path))
        self.fan_path.setToolTip(str(path))
        self._update_fan_info()
        self.settings.setValue("fan_path", str(path))
        self.settings.setValue("last_folder", str(path.parent))
        self.schedule_preview()

    def _opening_changed(self) -> None:
        if self.connector is None:
            return
        index = int(self.opening_combo.currentData() or 0)
        self.connector.selected_index = index
        opening = self.connector.opening
        self._update_gpu_info()
        self.schedule_preview()

    def _update_gpu_info(self) -> None:
        if self.connector is None:
            return
        opening = self.connector.opening
        count = self.connector_count.value()
        copies = "" if count == 1 else f"  •  {count} copies along {str(self.stack_axis.currentData()).upper()}"
        self.gpu_info.setText(
            f"Top Z {self.connector.top_z:.2f} mm  •  opening {opening.width:.1f} x {opening.depth:.1f} mm"
            f"  •  rim {opening.wall_thickness:.2f} mm{copies}"
        )
        self.wall_info.setText(f"Auto — {opening.wall_thickness:.2f} mm from top rim")

    def _connector_layout_changed(self) -> None:
        self._has_fitted_assembly = False
        self._update_gpu_info()
        self._update_mode_controls()
        self.schedule_preview()

    def _fan_layout_changed(self) -> None:
        self._has_fitted_assembly = False
        self._update_fan_info()
        self._update_mode_controls()
        self.schedule_preview()

    def _fan_mode_changed(self) -> None:
        self._update_mode_controls()
        if self.fan_mode.currentData() == "import" and self.imported_fan is None:
            saved = Path(str(self.settings.value("fan_path", "")))
            default = resource_path("Fans/default 120mm fan for shroud.stl")
            self.load_fan(saved if saved.is_file() else default)
        self._update_fan_info()
        self.schedule_preview()

    def _fan_size_changed(self) -> None:
        size = float(self.fan_size.currentData())
        self.fan_hole.setValue(136.0 if size == 140.0 else 116.0)
        self._update_fan_info()
        self.schedule_preview()

    def _update_fan_info(self) -> None:
        count = self.fan_count.value()
        copies = (
            ""
            if count == 1
            else f" • {count} copies along {str(self.fan_stack_axis.currentData()).upper()}"
        )
        if self.fan_mode.currentData() == "import" and self.imported_fan is not None:
            analysis = self.imported_fan
            description = (
                f"Detected {analysis.hole_diameter:.2f} mm opening • "
                f"{analysis.z_max - analysis.z_min:.2f} mm thick"
            )
        else:
            size = float(self.fan_size.currentData())
            spacing = 124.5 if size == 140.0 else 105.0
            description = f"{spacing:g} mm mounting pattern • 3 mm plate"
        self.fan_info.setText(description + copies)

    def _funnel_mode_changed(self) -> None:
        self._update_mode_controls()
        self.schedule_preview()

    def _update_mode_controls(self) -> None:
        imported = self.fan_mode.currentData() == "import"
        self.fan_file_widget.setVisible(imported)
        self.custom_fan_widget.setEnabled(not imported)
        curved = bool(self.funnel_mode.currentData())
        self.straight_widget.setEnabled(not curved)
        self.curve_widget.setEnabled(curved)
        gpu_multiple = self.connector_count.value() > 1
        fan_multiple = self.fan_count.value() > 1
        self.stack_axis.setEnabled(gpu_multiple)
        self.connector_spacing.setEnabled(gpu_multiple)
        self.gpu_bridge.setEnabled(gpu_multiple)
        self.gpu_bridge_thickness.setEnabled(
            gpu_multiple and self.gpu_bridge.currentData() in {"front", "back"}
        )
        self.fan_stack_axis.setEnabled(fan_multiple)
        self.fan_spacing.setEnabled(fan_multiple)
        self.fan_bridge.setEnabled(fan_multiple)
        self.split_widget.setVisible(gpu_multiple or fan_multiple)
        self.gpu_split_label.setVisible(gpu_multiple)
        self.split_distance.setVisible(gpu_multiple)
        self.fan_split_label.setVisible(fan_multiple)
        self.fan_split_distance.setVisible(fan_multiple)

    def schedule_preview(self) -> None:
        if self._building:
            return
        self._preview_timer.start()

    def _current_parts(self) -> AssemblyParts:
        if self.connector is None:
            raise GeometryError("Choose a GPU connector STL first.")
        funnel = FunnelConfig(
            curved=bool(self.funnel_mode.currentData()),
            length=self.length.value(),
            rounding_start=self.rounding_start.value(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            angle_x=self.angle_x.value(),
            angle_y=self.angle_y.value(),
            lead_in=self.lead_in.value(),
            lead_out=self.lead_out.value(),
            arc_diameter=self.arc_diameter.value(),
            outlet_diameter=self.fan_hole.value(),
            split_distance=self.split_distance.value(),
            fan_split_distance=self.fan_split_distance.value(),
        )
        stack = ConnectorStackConfig(
            count=self.connector_count.value(),
            axis=str(self.stack_axis.currentData()),
            spacing=self.connector_spacing.value(),
            bridge_mode=str(self.gpu_bridge.currentData()),
            bridge_thickness=self.gpu_bridge_thickness.value(),
        )
        fan_stack = FanStackConfig(
            count=self.fan_count.value(),
            axis=str(self.fan_stack_axis.currentData()),
            spacing=self.fan_spacing.value(),
            bridged=bool(self.fan_bridge.currentData()),
        )
        if self.fan_mode.currentData() == "import":
            if self.imported_fan is None:
                raise GeometryError("Choose a fan connector STL.")
            return build_assembly_parts(
                self.connector,
                funnel,
                imported_fan=self.imported_fan,
                stack_config=stack,
                fan_stack_config=fan_stack,
            )
        fan = FanConfig(
            size=float(self.fan_size.currentData()),
            hole_diameter=self.fan_hole.value(),
            screw_hole_diameter=self.screw_hole.value(),
        )
        return build_assembly_parts(
            self.connector,
            funnel,
            fan_config=fan,
            stack_config=stack,
            fan_stack_config=fan_stack,
        )

    def rebuild_preview(self) -> None:
        if self._building or self.connector is None:
            return
        self._building = True
        self.preview_status.setText("Updating preview…")
        QApplication.processEvents()
        try:
            parts = self._current_parts()
            self.last_parts = parts
            self.preview.set_meshes(
                [
                    RenderMesh(parts.gpu, (0.28, 0.43, 0.62, 1.0)),
                    RenderMesh(parts.funnel, (0.12, 0.66, 0.65, 1.0)),
                    RenderMesh(parts.fan, (0.94, 0.57, 0.20, 1.0)),
                ],
                fit=not self._has_fitted_assembly,
            )
            self._has_fitted_assembly = True
            message = (
                f"Preview ready • {parts.funnel_result.centerline_length:.1f} mm centerline "
                f"• {self.connector_count.value()} GPU connector(s) "
                f"• {self.fan_count.value()} fan(s) "
                f"• {self.connector.opening.wall_thickness:.2f} mm auto wall "
                f"• {len(parts.funnel.faces):,} funnel triangles"
            )
            if parts.funnel_result.warnings:
                message += " • " + " ".join(parts.funnel_result.warnings)
            self.preview_status.setText(message)
            self.preview_status.setProperty("error", False)
            self.export_button.setEnabled(True)
        except (GeometryError, ValueError) as exc:
            self.last_parts = None
            self.preview_status.setText(f"Cannot build preview: {exc}")
            self.preview_status.setProperty("error", True)
            self.export_button.setEnabled(False)
        finally:
            self.preview_status.style().unpolish(self.preview_status)
            self.preview_status.style().polish(self.preview_status)
            self._building = False

    def save_stl(self) -> None:
        try:
            parts = self._current_parts()
        except GeometryError as exc:
            QMessageBox.warning(self, "Assembly is not ready", str(exc))
            return
        default_folder = Path(str(self.settings.value("export_folder", Path.home())))
        default_path = default_folder / "gpu-shroud.stl"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save print-ready STL", str(default_path), "STL models (*.stl)"
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".stl":
            target = target.with_suffix(".stl")
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.preview_status.setText("Fusing and checking the final solid…")
            QApplication.processEvents()
            result = export_stl(parts, target)
        except GeometryError as exc:
            QMessageBox.critical(self, "STL could not be saved", str(exc))
            self.preview_status.setText(f"Export failed: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.settings.setValue("export_folder", str(target.parent))
        self.preview_status.setText(
            f"Saved {target.name} • one watertight solid • {len(result.faces):,} triangles"
        )
        QMessageBox.information(
            self,
            "Print-ready STL saved",
            f"Saved one connected, watertight solid:\n\n{target}\n\n"
            f"{len(result.faces):,} triangles",
        )


def app_stylesheet() -> str:
    up_arrow = resource_path("assets/spin-up.svg").as_posix()
    down_arrow = resource_path("assets/spin-down.svg").as_posix()
    return APP_STYLESHEET.replace("__SPIN_UP__", up_arrow).replace(
        "__SPIN_DOWN__", down_arrow
    )


APP_STYLESHEET = """
QMainWindow, QWidget {
    background: #111827;
    color: #dce7f3;
    font-family: "Segoe UI", "Ubuntu", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 10pt;
}
QLabel#appTitle { font-size: 22pt; font-weight: 650; color: #f4f8fc; }
QLabel#subtitle { color: #8fa4ba; font-size: 10.5pt; }
QLabel#versionBadge {
    color: #73e0d4; background: #123b40; border: 1px solid #22656a;
    border-radius: 9px; padding: 5px 10px; font-size: 8.5pt; font-weight: 700;
}
QGroupBox {
    border: 1px solid #2a3a4e; border-radius: 9px; margin-top: 12px;
    padding: 12px 9px 9px 9px; background: #172131; font-weight: 650;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #9fb3c8;
    font-size: 8.5pt; letter-spacing: 1px;
}
QGroupBox QGroupBox { background: #141e2c; border-color: #26364a; font-weight: 500; }
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background: #0d1522; border: 1px solid #33465c; border-radius: 5px;
    padding: 6px 7px; min-height: 20px; selection-background-color: #1c8b8b;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus { border-color: #38b9b2; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right; width: 24px;
    background: #29465f; border-left: 1px solid #466784;
    border-top-right-radius: 4px; border-bottom-right-radius: 4px;
}
QComboBox::down-arrow {
    image: url("__SPIN_DOWN__"); width: 12px; height: 8px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 24px;
    background: #29465f; border-left: 1px solid #466784;
    border-bottom: 1px solid #1d3043; border-top-right-radius: 4px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 24px;
    background: #29465f; border-left: 1px solid #466784;
    border-top: 1px solid #1d3043; border-bottom-right-radius: 4px;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover,
QComboBox::drop-down:hover { background: #386581; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("__SPIN_UP__"); width: 12px; height: 8px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("__SPIN_DOWN__"); width: 12px; height: 8px;
}
QPushButton {
    background: #26384d; border: 1px solid #38516d; border-radius: 5px;
    padding: 6px 10px; color: #e6eef7;
}
QPushButton:hover { background: #314861; border-color: #4c6988; }
QPushButton:pressed { background: #1c2b3d; }
QPushButton:disabled, QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {
    color: #637287; background: #141c27; border-color: #263140;
}
QPushButton#primaryButton {
    background: #178c87; border-color: #2fc1b8; color: white; font-weight: 700;
    font-size: 11pt;
}
QPushButton#primaryButton:hover { background: #1aa29c; }
QLabel#infoPill {
    background: #102c37; color: #83d6d0; border-radius: 5px; padding: 7px;
    font-size: 9pt;
}
QLabel#helpText { color: #75899f; font-size: 8.5pt; padding: 0 4px; }
QFrame#previewFrame { border: 1px solid #2a3a4e; border-radius: 9px; background: #09101b; }
QWidget#previewHeader { background: #172131; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QLabel#previewTitle { color: #a9bdd0; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }
QLabel#previewHelp { color: #6f849b; font-size: 8.5pt; }
QPushButton#smallButton { padding: 4px 9px; font-size: 8.5pt; }
QLabel#previewStatus {
    background: #121d2a; color: #84a0b9; border-top: 1px solid #26364a;
    padding: 9px 13px; font-size: 9pt;
}
QLabel#previewStatus[error="true"] { color: #ff9e9e; background: #301b24; }
QScrollArea { background: transparent; }
QScrollBar:vertical { background: #111827; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #34485f; min-height: 28px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: transparent; width: 10px; }
QToolTip { background: #26384d; color: #f2f6fa; border: 1px solid #4a637e; padding: 5px; }
"""
