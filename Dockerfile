# cogame-collab-cooking Coworld image: the game server, the bundled player and
# the static replay viewer in ONE image, env-switched by entrypoint.
#
# Stage 1 (wasm-builder) compiles the static replay viewer (Nim -> emscripten)
# with coworld-ctf's pinned toolchain from its Dockerfile.replay-viewer --
# emsdk 4.0.15 + nimby 0.1.27 + Nim 2.2.4 + `nimby --global sync nimby.lock`.
# It runs as linux/amd64 (the nimby release binary is x64) and its wasm output
# is architecture-independent. tools/build_replay_viewer.sh builds THIS target
# and copies /workspace/replay-viewer/dist out as the bundle.
#
# Stage 2 is the runtime and the DEFAULT target: python:3.12-slim +
# `pip install .`, plus the two shims tools/ci/docker_smoke.sh drives
# unmodified. It does NOT depend on the wasm stage -- the replay is a static
# bundle served by the platform, never a route on this container, so the game
# image never carries the viewer and `docker build .` never pays for emsdk:
#   /bin/collab-cooking          the game server
#   /bin/collab-cooking-player   the bundled player (PLAYER_PROMPT / PLAYER_SCRIPTED)
#
# Build: docker build --platform=linux/amd64 -t coworld-collab-cooking:latest .

FROM emscripten/emsdk:4.0.15 AS wasm-builder

RUN apt-get update && \
  apt-get install -y --no-install-recommends ca-certificates curl git && \
  rm -rf /var/lib/apt/lists/* && \
  curl -fsSL \
    -o /usr/local/bin/nimby \
    https://github.com/treeform/nimby/releases/download/0.1.27/nimby-Linux-X64 && \
  echo "3b3084394bd26b09f84a3f82389f075221c8784893238390939d71dd66ac9e8b  /usr/local/bin/nimby" | sha256sum -c - && \
  chmod +x /usr/local/bin/nimby && \
  nimby use 2.2.4

ENV PATH="/root/.nimby/nim/bin:$PATH"

WORKDIR /workspace
COPY nimby.lock .
# nimby installs into ~/.nimby/pkgs and writes no global config, so the search
# path is generated from the synced tree -- the same recipe coworld-builder's
# ci.yml uses, and the reason no machine-specific nim.cfg is committed.
RUN nimby --global sync nimby.lock && \
  for pkg in /root/.nimby/pkgs/*; do \
    if [ -d "$pkg/src" ]; then echo "--path:\"$pkg/src\"" >> /workspace/nim.cfg; \
    else echo "--path:\"$pkg\"" >> /workspace/nim.cfg; fi; \
  done && cat /workspace/nim.cfg

COPY replay-viewer/ replay-viewer/
COPY client/ client/
COPY data/ data/
RUN nim c --hints:off -d:emscripten replay-viewer/collab_cooking_replay.nim && \
  cp client/broadcast_core.js replay-viewer/dist/broadcast_core.js && \
  cp client/chrome_common.js replay-viewer/dist/chrome_common.js && \
  cp replay-viewer/static_replay.js replay-viewer/dist/static_replay.js && \
  cp replay-viewer/static_replay_worker.js replay-viewer/dist/static_replay_worker.js && \
  cp data/font.ttf replay-viewer/dist/font.ttf && \
  sed -e 's|<!-- CHROME_COMMON -->|<script src="./chrome_common.js"></script>|' \
      -e 's|<!-- BROADCAST_CORE -->|<script src="./static_replay.js"></script>|' \
    client/replay_broadcast.html > replay-viewer/dist/index.html && \
  rm -rf replay-viewer/dist/nimcache && \
  test -f replay-viewer/dist/collab_cooking_replay.wasm && \
  test -f replay-viewer/dist/collab_cooking_replay.js && \
  test -f replay-viewer/dist/collab_cooking_replay.data && \
  test -f replay-viewer/dist/static_replay_worker.js && \
  test -f replay-viewer/dist/index.html && \
  test -s replay-viewer/dist/font.ttf && \
  test -s replay-viewer/dist/chrome_common.js && \
  grep -q 'window.ChromeCommon' replay-viewer/dist/chrome_common.js && \
  grep -q 'chrome_common.js' replay-viewer/dist/index.html && \
  test -s replay-viewer/dist/broadcast_core.js && \
  grep -q 'window.BroadcastCore' replay-viewer/dist/broadcast_core.js && \
  grep -q 'static_replay.js' replay-viewer/dist/index.html && \
  grep -q 'static_replay_worker.js' replay-viewer/dist/static_replay.js && \
  grep -q "importScripts('./broadcast_core.js', './collab_cooking_replay.js')" \
    replay-viewer/dist/static_replay_worker.js && \
  grep -q '_cc_load_replay' replay-viewer/dist/collab_cooking_replay.js && \
  grep -q '_cc_stage_ptr' replay-viewer/dist/collab_cooking_replay.js && \
  ! grep -q '<script src="./broadcast_core.js"></script>' replay-viewer/dist/index.html && \
  ! grep -q '<script src="./collab_cooking_replay.js"></script>' replay-viewer/dist/index.html && \
  ! grep -Eq 'src="/[^/]' replay-viewer/dist/index.html


# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.12-slim AS game

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

# The shims tools/ci/docker_smoke.sh and the manifest runnables invoke. Two
# lines each, so `/bin/<slug>` and `/bin/<slug>-player` work unmodified.
RUN printf '#!/bin/sh\nexec python -m collab_cooking.coworld.server "$@"\n' > /bin/collab-cooking && \
    chmod +x /bin/collab-cooking && \
    printf '#!/bin/sh\nexec python -m collab_cooking.coworld.player "$@"\n' > /bin/collab-cooking-player && \
    chmod +x /bin/collab-cooking-player && \
    python -c "import collab_cooking.coworld.server, collab_cooking.coworld.player"

EXPOSE 8080
CMD ["/bin/collab-cooking"]
