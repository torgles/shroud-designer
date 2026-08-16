#!/usr/bin/env bash
# Build the Linux Shroud Designer application (PyInstaller onedir package).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

SKIP_TESTS=0
SKIP_PACKAGE=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-package) SKIP_PACKAGE=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./build.sh [--skip-tests] [--skip-package]

  --skip-tests    Skip pytest before packaging
  --skip-package  Only run PyInstaller (skip tarball + portable folder)

Produces:
  dist/ShroudDesigner/ShroudDesigner
  dist/ShroudDesigner-0.4.5.1-linux-x86_64.tar.gz
  Shroud Designer Linux/   (portable app + install scripts)
  public/ShroudDesigner-0.4.5.1-linux-x86_64.tar.gz
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  echo "Creating .venv with Python 3.11 via uv..."
  if ! command -v uv >/dev/null 2>&1; then
    echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  uv python install 3.11
  uv venv --python 3.11 .venv
fi

# shellcheck disable=SC1091
source "$PROJECT_ROOT/.venv/bin/activate"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if command -v uv >/dev/null 2>&1; then
  uv pip install -q -r requirements-dev.txt
elif "$PYTHON" -m pip --version >/dev/null 2>&1; then
  "$PYTHON" -m pip install -q -r requirements-dev.txt
else
  echo "Need uv or pip to install requirements." >&2
  exit 1
fi

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  "$PYTHON" -m pytest -q tests
fi

"$PYTHON" -m PyInstaller --noconfirm --clean ShroudDesigner.spec

APP_DIR="$PROJECT_ROOT/dist/ShroudDesigner"
BIN="$APP_DIR/ShroudDesigner"
if [[ ! -x "$BIN" ]]; then
  echo "Build failed: missing $BIN" >&2
  exit 1
fi

# Smoke-test the packaged binary (no GUI)
"$BIN" --self-test "$PROJECT_ROOT/packaged-self-test-linux.json"
"$PYTHON" - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("packaged-self-test-linux.json").read_text(encoding="utf-8"))
if not report.get("ok"):
    raise SystemExit(f"Packaged self-test failed: {report}")
print("Packaged self-test OK")
PY

VERSION="0.4.5.1"
TARBALL="$PROJECT_ROOT/dist/ShroudDesigner-${VERSION}-linux-x86_64.tar.gz"
if [[ "$SKIP_PACKAGE" -eq 0 ]]; then
  tar \
    --sort=name \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -C "$PROJECT_ROOT/dist" \
    -cf - ShroudDesigner | gzip -n > "$TARBALL"

  PORTABLE="$PROJECT_ROOT/Shroud Designer Linux"
  rm -rf "$PORTABLE"
  mkdir -p "$PORTABLE"
  cp -a "$APP_DIR" "$PORTABLE/ShroudDesigner"
  cp "$PROJECT_ROOT/linux/shroud-designer.desktop" "$PORTABLE/"
  cp "$PROJECT_ROOT/linux/install.sh" "$PORTABLE/"
  cp "$PROJECT_ROOT/linux/uninstall.sh" "$PORTABLE/"
  cp "$PROJECT_ROOT/linux/run.sh" "$PORTABLE/"
  cp "$PROJECT_ROOT/linux/README.md" "$PORTABLE/"
  cp "$PROJECT_ROOT/LICENSE" "$PORTABLE/"
  cp "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" "$PORTABLE/"
  cp -a "$PROJECT_ROOT/licenses" "$PORTABLE/licenses"
  cp "$PROJECT_ROOT/assets/shroud-designer.png" "$PORTABLE/"
  chmod +x \
    "$PORTABLE/install.sh" \
    "$PORTABLE/uninstall.sh" \
    "$PORTABLE/run.sh" \
    "$PORTABLE/ShroudDesigner/ShroudDesigner"

  PUBLIC_ROOT="$PROJECT_ROOT/public"
  PUBLIC_NAME="ShroudDesigner-${VERSION}-linux-x86_64"
  PUBLIC_DIR="$PUBLIC_ROOT/$PUBLIC_NAME"
  PUBLIC_ARCHIVE="$PUBLIC_ROOT/$PUBLIC_NAME.tar.gz"
  mkdir -p "$PUBLIC_ROOT"
  rm -rf "$PUBLIC_DIR"
  rm -f "$PUBLIC_ARCHIVE" "$PUBLIC_ROOT/SHA256SUMS"
  mkdir -p "$PUBLIC_DIR"
  cp -a "$PORTABLE/." "$PUBLIC_DIR/"
  tar \
    --sort=name \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -C "$PUBLIC_ROOT" \
    -cf - "$PUBLIC_NAME" | gzip -n > "$PUBLIC_ARCHIVE"

  (
    cd "$PUBLIC_ROOT"
    sha256sum "$(basename "$PUBLIC_ARCHIVE")" > SHA256SUMS
  )
fi

echo
echo "Build complete."
echo "Application: $BIN"
if [[ "$SKIP_PACKAGE" -eq 0 ]]; then
  echo "Archive:     $TARBALL"
  echo "Portable:    $PROJECT_ROOT/Shroud Designer Linux"
  echo "Public:      $PUBLIC_ARCHIVE"
  echo
  echo "Install for this user:"
  echo "  cd \"Shroud Designer Linux\" && ./install.sh"
fi
