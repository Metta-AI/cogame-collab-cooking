# Numeric training

`tools/training_bridge.py` runs the published kitchen simulator through Metta's
persistent JSONL GameBridge protocol. Each decision uses the hosted player's
`collab-cooking.player.v1` observation and one of its five action names. Four
seats decide from one frozen tick, then the game applies all actions and steps
once. The game owns movement, station rules, ticket timing, scores, and replay.

Create the local environment, then select any of the eight manifest variant IDs:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python tools/training_bridge.py --variant cramped
```

The bridge reads `reset`, `encode`, `teacher`, and `step` JSON lines on stdin.
`reset` requires four players and a string seed. `encode` returns 1,510 numeric
values and five unmasked action objects. Values contain a seat one-hot vector,
tick and horizon, public scores, and the 500 observation triples sent to that
seat by the game. A typed candidate question exposes the same five actions
for external choice policies and includes the player-visible feature
table. The teacher is the bundled `KitchenBrain` player with the
normal brigade, brigade, passer, and courier roster. It sees only its own
player observation. A terminal response returns the game's team-dish score
with the published per-seat delivery tie-break. Use `--steps N` for a shorter
curriculum; the default is the variant's full 900 ticks.

From a Metta checkout, set the GameBridge command to this repository's
`.venv/bin/python` and `tools/training_bridge.py`, plus `--variant` and a
manifest variant ID. The numeric codec uses `observation_size=1510`,
`action_sizes=[5]`, `players=4`, and `max_decisions>=3600` with Metta's
`recipes.external.coworld` recipe. Keep the game package and manifest available
to the bridge subprocess. Native Puffer training requires CUDA.

The local verification command is:

```sh
.venv/bin/python -m pytest tests/test_training_bridge.py -q
```

All eight variants completed a full 900-tick GameBridge episode with 3,600
accepted teacher actions each. A full `DecisionEnvironment` episode also
finished with 1,510 values, five actions, and teacher targets. These checks
verify the training protocol and game score. They do not measure a trained
policy's quality. This bridge trains per-tick numeric actions. The hosted
prompt-plan policies choose shift jobs while their ordinary players
submit per-tick actions through the same seat observation/action protocol.

## Shift-order language collection

`tools/plan_bridge.py --variant cramped` collects the production JSON shift
orders rather than per-tick movement actions. It uses the published kitchen,
900-tick horizon, plan interval, parser, seat-local remembered observations, and
player executors. Four seat views are frozen together. Four parsed plans are
queued, then the executors advance the simulator to the next planning boundary.
The accepted action is the normalized directive, not a guarantee that its job
succeeded. The existing numeric bridge keeps its per-tick contract.

This bridge deliberately synchronizes planning. It does not reproduce hosted
wall-clock inference latency, the rolling request budget, or late plan delivery.
Measure those effects separately through the ordinary native host and players.
Never describe synchronous collector outcomes as proof of native timing parity.
Its `teacher` command cycles through legal stations as a deterministic workflow
fixture. Those actions are not expert labels or evidence of policy quality.

The hosted game accepts `promptVariant` and `promptSeed` in configuration. The
language bridge accepts the same keys on `reset`. Available training wordings are
`canonical`, `plain`, `numbered`, `questions`, and `reordered`. `mixed` requires an
explicit nonnegative prompt seed and selects training wording `seed % 5`. It does
not consume kitchen or alias randomness. The choice stays fixed across all seats,
turns, and retries. The canonical introduction is the default.

`heldout-briefing` and `heldout-reference` are reserved for evaluation. The bridge
rejects them with `split: train`; use `split: validation` or `split: test` after
freezing training choices. Private bridge observations include bank version,
selected wording, selection seed, and hashes of the exact system/user messages.
Standing orders and action requirements stay fixed. All wordings now state the
published score correctly: team dishes plus `0.01 * own deliveries`.

For matched experiments, keep every wording and kitchen variant of a scenario
seed in the same data split. Compare base, canonical-trained, and mixed-trained
checkpoints with identical scenario seeds, seat assignments, partner policies,
kitchens, and inference budgets. Report invalid directives, late plans, budget
skips, fallbacks, incomplete games, team dishes, and per-seat scores separately.

Run `python -m pytest tests/test_prompt_games.py -q` for complete synchronous
shift-order games across all eight kitchens, using the real parser, executors,
and simulator. The wording bank varies the dish chain, handoffs, and work cadence;
recipe values, scoring, and response requirements remain fixed.
Existing hosted transport tests cover request formatting and seat attribution.
These checks do not establish native timing parity or learned robustness.
