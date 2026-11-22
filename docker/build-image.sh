#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
sdk_root=${EBINEX_CHECKOUT:-"$repo_root/../ebinex"}
revision=$(git -C "$sdk_root" rev-parse --verify HEAD^{commit})
python_bin=${PYTHON_BIN:-python3}
"$python_bin" -m pip --version >/dev/null 2>&1 || {
  echo "Use PYTHON_BIN para apontar para um Python com pip." >&2
  exit 2
}
work=$(mktemp -d)
trap 'rm -r -- "$work"' EXIT
mkdir -p "$work/sdk" "$work/wheels"
# Build committed SDK bytes only (no working-tree edits or local credentials).
git -C "$sdk_root" archive "$revision" | tar -x -C "$work/sdk"
"$python_bin" -m pip wheel --no-deps --wheel-dir "$work/wheels" "$work/sdk"
wheels=("$work"/wheels/ebinex_sdk-*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]] || { echo "Esperado um wheel Ebinex." >&2; exit 1; }
wheel=${wheels[0]}
name=$(basename "$wheel")
sha256=$(sha256sum "$wheel" | cut -d ' ' -f1)
cd "$repo_root"
mkdir -p docker/vendor
# Drop stale staged wheels so Docker never installs an ambiguous set.
for old_wheel in docker/vendor/ebinex_sdk-*.whl; do
  if [[ -f "$old_wheel" && "$old_wheel" != "docker/vendor/$name" ]]; then
    unlink "$old_wheel"
  fi
done
install -m 644 "$wheel" "docker/vendor/$name"
printf 'revision=%s\nsha256=%s\nwheel=%s\n' "$revision" "$sha256" "$name" > docker/vendor/ebinex-sdk.lock
printf 'SDK Ebinex: %s (%s)\nSHA256: %s\n' "$name" "$revision" "$sha256"
export APP_UID=${APP_UID:-$(id -u)} APP_GID=${APP_GID:-$(id -g)}
docker compose build tesse
