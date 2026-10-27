# Private decision trajectories

Set `COGAME_SAVE_TRAJECTORY_URI` to a private `file://` or signed upload URI,
separate from `COGAME_SAVE_REPLAY_URI`. Supply `COWORLD_EPISODE_ID`,
`COWORLD_GAME_VERSION`, and the exact published `COWORLD_SOURCE_REVISION`.
The runtime retains the registered `COWORLD_GAME_NAME` and immutable
`COWORLD_GAME_IMAGE_DIGEST` when provided. No trajectory is written unless
its private destination is configured.

The game emits the canonical Coworld decision/episode JSONL contract:
seat-visible inputs, actual provider request/response bodies and returned native
call IDs, accepted and rejected attempts, the applied macro action, explicit
fallback provenance, and the terminal outcome. Scripted policies are `unknown`
unless independently qualified as teachers. Missing call IDs remain missing.
These records do not contain sampled token IDs or behavior log probabilities.
Public replay artifacts never contain the private prompts or provider receipts.
Local files use exclusive creation with mode 0600.

Read the artifact with the maintained SDK:

```python
from pathlib import Path
from coworld.decision_trajectory import complete_episodes, read_trajectory_jsonl

episodes = list(complete_episodes(read_trajectory_jsonl(Path("trajectory.jsonl"))))
```

Completed episodes may be exported; deadline and fault summaries remain
truncated or failed and are excluded by the SDK's complete-episode reader.
A complete record establishes execution and provenance, not demonstration
quality or permission to train. The applied macro action is the game decision;
the public replay retains the deterministic physical control bytes.

Cooking plans now execute their prerequisite chain through the existing recipe
executor. A `pot` or `fryer` job fetches and chops missing ingredients, plates
ready food, and serves carried dishes. Chopping and washing continue for their
required uses after the carried raw item has moved into the station. This
keeps the chosen job consequential across the whole shift. Tickets, scores,
recipes, station rules, plan cadence, and inference budgets are unchanged.
The private receipt reader joins bounded in-flight plan calls before writing;
a remaining reader prevents completion qualification.
