# Policies

One image, one player entrypoint (`/bin/collab-cooking-player`), switched by environment.

## `PLAYER_PROMPT` -- a prompt policy

Set `PLAYER_PROMPT` to your standing orders (up to 1200 runes). The seat registers as a prompt
seat and the **game** asks the model for one shift order every 50 ticks -- roughly ten seconds of
wall clock -- while a scripted executor in the player container walks the cog tick by tick until
the next order lands. Two clocks: the model picks the job, the executor picks the tile.

The model sees only what that seat can see: its own 13x13 window, its remembered world model
(with staleness -- "pot (3,11) 33 ticks ago"), the counters it knows are holding something, the
team radio (the other seats' `say` lines from the previous turn) and its own private `note`. It
never sees the seed, the ticket schedule, another seat's plan, or any real policy name.

### The reply

One JSON object, nothing else, beginning with `{`. Leading or trailing prose is tolerated
(extraction takes the first balanced `{...}` span); unknown keys are ignored.

| field | type | cap | invalid becomes |
| --- | --- | --- | --- |
| `station` | enum | 12 chars | must be a member of `LEGAL STATIONS` -- anything else is **illegal**: one retry, then the fallback |
| `recipe` | `salad\|soup\|fries\|any` | 6 chars | `any` |
| `zone` | `left\|right\|pass\|any` | 6 chars | `any` |
| `handoff` | an ally alias or `none` | 8 chars | `none` |
| `yield_to` | an ally alias or `none` | 8 chars | `none` |
| `say` | free text | 120 runes | truncated on a rune boundary; broadcast to the team next turn and to spectators |
| `note` | free text | 200 runes | truncated on a rune boundary; private, echoed back only to you, never written to the replay |

`LEGAL STATIONS` is precomputed by the same predicate the validator applies -- the stations
reachable from your current tile in this kitchen -- so in `forced` a left-half cog is never
offered `pot`, `fryer` or `pass`, and can never be told it chose one illegally after being
offered it.

### What the executor does with a plan

1. `station` sets the sub-goal, overriding the brain's own choice until the next plan:
   `veg`, `meat`, `chop`, `pot`, `fryer`, `plate`, `pass`, `sink`, `board`, or `hold` (stand
   still unless carrying a dish, in which case route to the pass).
2. `recipe` pins the recipe unless `any`, in which case the brain keeps deriving it from the
   board.
3. `zone` filters target selection: `left` and `right` are the halves by column, `pass` is the
   counter cells you can reach over. A goal outside the zone is replaced by the nearest in-zone
   counter -- the cog stages the item instead of crossing.
4. `handoff = <alias>`: while carrying, if that ally is within Chebyshev 3 and a free pass
   counter is nearby, deposit there instead of walking the item onward.
5. `yield_to = <alias>`: if that ally is cardinally adjacent and your next step would be onto
   their tile, step to the first free perpendicular tile, else stand still for that tick.

### Degrading

| failure | response |
| --- | --- |
| the call times out (12 s) | **retry once**, that seat only, with a hint |
| the reply is not JSON, or has no balanced object | the same single retry |
| `station` is not in `LEGAL STATIONS` | the same single retry |
| the retry also fails | that seat plays `brigade` until the next plan turn, with a `fallback` event naming the cause |
| the rolling 26 requests/minute budget is exhausted | the turn is skipped for that seat, cause `rate_budget` |
| no credentials at all | the client is **disabled at startup and makes zero network calls**; every prompt seat plays `brigade` all episode, and the episode still finishes `complete` |

## `PLAYER_SCRIPTED` -- the baselines

Four names, all the same brain with one parameter changed.

* **`brigade`** (the default, and the fallback) -- roles: slot 0 prep, 1 cook, 2 server, 3
  all-rounder. Dirty plate to the sink; servable dish to the pass (via the board if that recipe
  has no live ticket); otherwise the role branch, which prioritises a ready-or-burned cooker,
  then the deepest queue, then the next missing ingredient.
* **`runner`** -- every seat all-rounder: no roles, everyone takes the nearest useful job. The
  no-task-allocation control.
* **`passer`** -- `brigade` with the zone pinned to its own half and hand-off always on: it never
  crosses the midline and always stages items on the nearest pass counter.
* **`courier`** -- every seat server: grab a plate, serve whatever is ready, prep only when
  nothing is. The greedy-serve control; it starves the prep chain, which is the point of having
  it on the ladder.
