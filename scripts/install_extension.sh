#!/usr/bin/env bash
# Install the packaged Blendermp extension into Blender's user extensions.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c 'import json;print(json.load(open("version.json"))["version"])' 2>/dev/null || true)"
ZIP="${1:-$ROOT/dist/blender_multiplayer-${VERSION}.zip}"
BLENDER="${BLENDER:-blender}"

if [[ ! -f "$ZIP" ]]; then
  echo "extension zip not found: $ZIP" >&2
  echo "run the package build first, or pass the zip path" >&2
  exit 1
fi

"$BLENDER" --command extension install-file -r user_default --enable "$ZIP"
echo "installed $ZIP"
