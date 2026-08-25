#!/usr/bin/env bash
# Replay-viewer bundle build hook for `coworld build`.
#
# coworld.bundle._build_replay_viewer_bundle invokes this with one argument:
# the absolute bundle output directory (named after
# game.replay_viewer.bundle, "static-replay-viewer"). It must produce
# index.html there. This is coworld-ctf's hook with the file list retargeted:
# it reuses the Dockerfile's wasm-builder stage (ctf's Dockerfile.replay-viewer
# recipe -- emsdk 4.0.15 + nimby 0.1.27 + Nim 2.2.4 + `nimby --global sync
# nimby.lock`) and copies out replay-viewer/dist.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 /absolute/path/to/static-replay-viewer" >&2
  exit 1
fi

requested_output="$1"

if [[ "${requested_output}" != /* || "$(basename "${requested_output}")" != "static-replay-viewer" ]]; then
  echo "unsafe bundle output: ${requested_output}" >&2
  exit 1
fi

# mkdir BEFORE the containment check: `coworld build` pre-creates the parent,
# a fresh CI checkout does not, and `cd` into a directory that does not exist
# exits 1 with no useful message (ecos, 2026-08-23).
mkdir -p "$(dirname "${requested_output}")"
output_parent="$(cd "$(dirname "${requested_output}")" && pwd -P)"
output_dir="${output_parent}/static-replay-viewer"
if [[ "${output_dir}" != "${repo_dir}"/* || -L "${output_dir}" ]]; then
  echo "unsafe bundle output: ${requested_output}" >&2
  exit 1
fi

rm -rf "${output_dir}"
mkdir -p "${output_dir}"

image_tag="coworld-collab-cooking-viewer-build:$$"
container_id=""
cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker rm "${container_id}" >/dev/null 2>&1 || true
  fi
  docker image rm "${image_tag}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# The wasm-builder stage is pinned to linux/amd64 (the nimby release binary is
# x64); the wasm output is architecture-independent.
build_args=(
  --platform linux/amd64
  --file "${repo_dir}/Dockerfile"
  --target wasm-builder
  --tag "${image_tag}"
  "${repo_dir}"
)
if docker buildx version >/dev/null 2>&1; then
  docker buildx build --load "${build_args[@]}"
else
  docker build "${build_args[@]}"
fi
container_id="$(docker create --platform linux/amd64 "${image_tag}")"
docker cp "${container_id}:/workspace/replay-viewer/dist/." "${output_dir}"

# Every file the page references must be in the bundle -- assert ALL of them,
# not a sample: a missing collab_cooking_replay.wasm renders as a blank board
# with every other asset 200.
expected=(index.html chrome_common.js broadcast_core.js static_replay.js
          static_replay_worker.js collab_cooking_replay.js
          collab_cooking_replay.wasm collab_cooking_replay.data)
missing=()
for f in "${expected[@]}"; do
  [[ -f "${output_dir}/${f}" ]] || missing+=("${f}")
done
if (( ${#missing[@]} )); then
  echo "bundle incomplete: missing ${missing[*]} in ${output_dir}" >&2
  ls -la "${output_dir}" >&2 || true
  exit 1
fi
echo "static-replay-viewer bundle: ${output_dir} (${expected[*]})"
