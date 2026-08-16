# Shroud Designer for Linux

Portable Linux build of **Shroud Designer 0.4.5.1** (x86_64).

## Quick install (current user)

From this folder:

```bash
./install.sh
```

That copies the app into `~/.local/share/shroud-designer`, puts a launcher on
`~/.local/bin/ShroudDesigner`, and installs a desktop menu entry.

Launch:

```bash
ShroudDesigner
```

Or open **Shroud Designer** from your application menu.

Uninstall:

```bash
./uninstall.sh
```

## Run without installing

```bash
./run.sh
```

Or directly:

```bash
./ShroudDesigner/ShroudDesigner
```

## Requirements

- 64-bit Linux (glibc)
- OpenGL-capable display (the 3D preview uses OpenGL)
- Typical desktop libraries already present on Ubuntu/Fedora/etc.

If the binary fails to start with a missing library error, install the matching
system package (for example Qt/OpenGL runtime packages on minimal installs).

## License

Shroud Designer is licensed under the MIT License. Bundled runtime components
retain their own licenses; see `THIRD_PARTY_NOTICES.md` and the `licenses`
directory included with this package.

## Use

1. Select an upright GPU connector STL (units = millimetres; opening at max Z).
2. Configure 1–10 GPU copies, their X/Y spacing, and optional bridge style.
3. Choose a custom 120/140 mm fan plate or import a fan connector STL, then
   configure 1–4 fan copies, X/Y spacing, and optional full-depth bridges.
4. Adjust the main funnel and GPU/fan split distances; the preview updates
   after each change.
5. **Save print-ready STL…** — export only succeeds when the result is one
   connected, watertight solid.

Preview controls: mouse wheel zoom · left-drag rotate · right-drag pan ·
double-click or **Fit view** to frame the assembly.
