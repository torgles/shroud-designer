# Shroud Designer 0.4.5.1

Shroud Designer turns an upright GPU connector STL into one print-ready, airtight shroud assembly. It detects the opening at the connector's highest Z layer, generates a straight/offset or compound-curved transition, adds a custom or imported fan connector, then fuses and validates the result before saving it as STL.

## Download — Linux

Download `ShroudDesigner-0.4.5.1-linux-x86_64.tar.gz` from the
[v0.4.5.1 release](https://github.com/torgles/shroud-designer/releases/tag/v0.4.5.1),
extract it, then run:

```bash
./install.sh
```

The installer is per-user and does not require administrator privileges. You
can also run the application portably with `./run.sh`.

## Use

1. Open **Shroud Designer** from the desktop shortcut (or run the Linux binary).
2. Select an upright GPU connector STL. STL units are interpreted as millimetres and the opening must be at maximum Z.
3. Set the connector count from 1–10. Multiple connectors are identical copies and can be stacked along X or Y with clear spacing between their bodies. They may remain unbridged or use a full, front, or back bridge.
4. Choose a custom 120/140 mm fan plate or import a finished fan connector STL, then select 1–4 identical fans along X or Y. For imported brackets, the funnel snaps to the full bracket outline by default so multi-opening plates become a common plenum; uncheck that option to use only the largest detected opening. Imported brackets can also be rotated from -180° to 180°. Fan plates may remain unbridged or use a full-depth bridge.
5. Adjust the funnel. **Rounding starts at** preserves the connector's exact top profile for the chosen height before smoothing it toward the fan—use this to retain clearance around power-cable openings. Separate GPU and fan split distances control how far each individual duct travels before entering the shared transition.
6. Select **Save print-ready STL…**. Export only succeeds when the result is one connected, watertight solid.

Preview controls:

- Mouse wheel: zoom
- Left drag: rotate
- Right drag: move the model
- Double-click or **Fit view**: frame the full assembly

## Geometry notes

- Straight offsets move the fan in X/Y without rotating it.
- **Rounding starts at** defaults to 8 mm. Set it to 0 mm for immediate smoothing, or increase it to carry cable-access and other sharp connector features farther into the funnel. A longer preserved section may require a longer funnel to avoid an abrupt fan transition.
- After rounding starts, recessed features are eased into the fan opening over the remaining funnel length. Where a recess closes, the outside leads the airway by at least the detected wall thickness so the resulting roof is printable instead of a zero-thickness seam.
- **Use original 0.3 funnel loft** restores the direct point-to-point contour transition from commit `324ca88`, immediately before the 0.4 funnel work began. In this compatibility mode, **Rounding starts at** and 0.4 roof reinforcement are intentionally disabled.
- Connector spacing is the clear edge-to-edge gap between the connector STL bounding boxes, not center-to-center spacing.
- For multiple GPUs, import a single-card connector and use **Connector count** instead of a combined multi-card STL. This leaves spacing and bridge choices adjustable for power-cable access.
- GPU split distance is measured from the GPU openings toward the shared transition. Fan split distance is measured back from the fan openings. Short collector chambers merge each set of airtight ducts into the main funnel.
- GPU bridges can span the full connector Z length or only a configurable thickness at the front (maximum-Z funnel end) or back. Fan bridges span the full plate depth.
- Fan spacing is also a clear edge-to-edge gap. Fan arrays support up to four identical custom or imported plates.
- **Snap to bracket outline (not opening)** is enabled by default for imported fan STLs. The funnel seals around the bracket's outside perimeter while the original airflow and screw holes remain open in the final plate. Rotation is applied to both the bracket and its matching funnel outlet.
- Compound curves combine the X and Y bend values into one smooth centerline bend. The fan is rotated so its plate remains perpendicular to the outlet.
- **Arc diameter** is the free inside diameter of the elbow. A larger value produces a wider, gentler curve without allowing the inside wall to fold through itself.
- The supplied 120 mm reference measures 116 mm at the airflow opening, about 4.6 mm at the screw holes, and uses a 105 mm mounting pattern.
- The generated 140 mm option uses a 136 mm airflow opening and a 124.5 mm mounting pattern. The plate is regenerated at its true dimensions; screw holes are not scaled.

## Development

Requires **Python 3.11** (recommended). On Linux, [uv](https://github.com/astral-sh/uv) is the easiest way to get it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python app.py
```

Windows (PowerShell):

```powershell
python -m pip install -r requirements-dev.txt
python app.py
```

Test:

```bash
python -m pytest -q
```

### Build — Linux

```bash
./build.sh
```

Produces:

| Output | Description |
|--------|-------------|
| `dist/ShroudDesigner/ShroudDesigner` | Runnable app directory |
| `dist/ShroudDesigner-0.4.5.1-linux-x86_64.tar.gz` | Distributable archive |
| `Shroud Designer Linux/` | Portable folder with `install.sh` |
| `public/ShroudDesigner-0.4.5.1-linux-x86_64.tar.gz` | Public release archive |

Install for the current user:

```bash
cd "Shroud Designer Linux"
./install.sh
```

See `linux/README.md` for portable-folder details.

### Build — Windows

```powershell
.\build.ps1
```

Creates `dist\ShroudDesigner\ShroudDesigner.exe`, `dist\ShroudDesigner-0.4.5.1-Setup.exe`, `dist\ShroudDesigner-0.4.5.1-windows-x86_64.zip`, and Windows checksums.

## License

Shroud Designer is licensed under the [MIT License](LICENSE). Packaged builds
contain open-source dependencies under their own licenses; see
[Third-party notices](THIRD_PARTY_NOTICES.md).
