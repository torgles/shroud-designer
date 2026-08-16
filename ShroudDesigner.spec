# -*- mode: python ; coding: utf-8 -*-
import sys
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project = Path(SPEC).resolve().parent
is_windows = sys.platform == "win32"
sysconfig_data_module = (
    sysconfig._get_sysconfigdata_name() if not is_windows else None
)
icon_path = project / "assets" / (
    "shroud-designer.ico" if is_windows else "shroud-designer.png"
)

# PyOpenGL loads platform backends dynamically; collect the full package on Linux.
opengl_datas, opengl_binaries, opengl_hidden = collect_all("OpenGL")
hidden = [
    "manifold3d",
    "mapbox_earcut",
    "trimesh.boolean",
    "shapely",
    "OpenGL",
    "OpenGL.GL",
    "OpenGL.GLU",
    "OpenGL.platform.glx",
    "OpenGL.platform.egl",
    "OpenGL.platform.osmesa",
    *collect_submodules("OpenGL"),
    *opengl_hidden,
]

a = Analysis(
    [str(project / "app.py")],
    pathex=[str(project)],
    binaries=opengl_binaries,
    datas=[
        (str(project / "GPU Connectors" / "cmp front.stl"), "GPU Connectors"),
        (str(project / "GPU Connectors" / "V100 Front.stl"), "GPU Connectors"),
        (str(project / "Fans" / "default 120mm fan for shroud.stl"), "Fans"),
        (str(project / "assets" / "shroud-designer.ico"), "assets"),
        (str(project / "assets" / "shroud-designer.png"), "assets"),
        (str(project / "assets" / "shroud-designer.svg"), "assets"),
        (str(project / "assets" / "spin-up.svg"), "assets"),
        (str(project / "assets" / "spin-down.svg"), "assets"),
        *opengl_datas,
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
        "skimage",
        "lxml",
        "PIL",
        "IPython",
        "win32com",
        "pythoncom",
        "yaml",
        "tkinter",
        # Build configuration is not needed by the packaged application.
        *([sysconfig_data_module] if sysconfig_data_module else []),
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe_kwargs = dict(
    exclude_binaries=True,
    name="ShroudDesigner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=not is_windows,
    upx=is_windows,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    icon=str(icon_path) if icon_path.exists() else None,
)
if is_windows:
    exe_kwargs["version"] = str(project / "version_info.txt")

exe = EXE(
    pyz,
    a.scripts,
    [],
    **exe_kwargs,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=not is_windows,
    upx=is_windows,
    upx_exclude=[],
    name="ShroudDesigner",
)
