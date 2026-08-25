#!/usr/bin/env python3
"""Load `coworld_manifest_template.json` with the pinned coworld's own loader.

`coworld build` validates the template BEFORE it touches docker
(`coworld/bundle.py::_load_template_manifest`), and every level of the model is
`extra="forbid"`. A key in the wrong object therefore fails the FIRST step of
`coworld-release.yml` ("Build the Coworld manifest"), where no other CI job
would see it -- `ci.yml` only calls the build hook directly. Running the real
loader here catches that class of failure before phase 40.

It also asserts the static viewer is declared where the package actually reads
it from: `game.replay_viewer` (`bundle.py:81` gates the build hook,
`upload.py:927` gates the bundle upload). A top-level `replay_viewer` is not
just rejected -- if it were tolerated, the hook would never run and the bundle
would be uploaded to nobody.

Run it in a venv that holds the pinned coworld (see `ci.yml`):

    python tools/ci/check_manifest_loads.py [path/to/coworld_manifest_template.json]
"""

from __future__ import annotations

import json
import pathlib
import sys

from coworld.bundle import _load_template_manifest

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE_PLACEHOLDER = "{{COLLAB_COOKING_IMAGE}}"


def main() -> None:
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "coworld_manifest_template.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "version" in raw.get("game", {}):
        raise SystemExit(
            "::error::game.version is set in the template; coworld build refuses it "
            "(bundle.py: 'Coworld manifest templates must not set game.version') -- "
            "the version comes from the --version flag"
        )
    # The same call `coworld build` makes, with the compose service's image
    # placeholder resolved the way `_compose_image_placeholders` resolves it.
    manifest = _load_template_manifest(raw, "0.1.0", {IMAGE_PLACEHOLDER: "coworld-collab-cooking:latest"})

    viewer = manifest.game.replay_viewer
    if viewer is None or viewer.bundle != "static-replay-viewer":
        raise SystemExit(
            "::error::game.replay_viewer.bundle must be 'static-replay-viewer'; "
            f"got {viewer!r}. Anywhere else in the file and coworld build never "
            "invokes tools/build_replay_viewer.sh and uploads no bundle."
        )
    print(
        f"manifest OK: {path.name} loads with coworld's own template loader; "
        f"game.replay_viewer.bundle={viewer.bundle}, game.owner={manifest.game.owner}"
    )


if __name__ == "__main__":
    main()
