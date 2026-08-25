# Collaborative Cooking

Four cogs share one kitchen for 900 ticks. Tickets arrive on an order board every 18 ticks and
expire 50 ticks later. A dish is a chain of single-item errands: fetch a vegetable or a piece of
meat, chop it, load a pot or a fryer, pick up a clean plate, plate the dish, walk it to the pass,
and serve it against a live ticket; the plate comes back dirty and somebody has to wash it.

**A cog can carry exactly one thing.** Counters are walls you can put your one thing down on and
someone else can pick it up, so the whole kitchen is a hand-off network -- and in one of the eight
rooms that is the *only* way anything moves.

**Team score = dishes served.** Nothing else scores. Burning a pot, letting a ticket expire, or
standing in the doorway costs dishes and only dishes.

## The eight kitchens

Each variant is one hand-authored room that isolates one coordination problem.

| variant | the coordination problem |
| --- | --- |
| `open-kitchen` | the control: no forced hand-off, no choke |
| `cramped` | four cogs in a 7x5 room |
| `forced` | two sealed halves; items only cross over the counter |
| `crowded` | one choke tile between prep and service |
| `asymmetric` | unequal station access, so task allocation decides it |
| `circuit` | hand off over the island or walk twelve tiles |
| `ring` | a one-tile corridor: right of way |
| `figure-eight` | two loops, one shared spine |

## A policy is just a prompt

Both champions are prompt policies. A seat registers with `PLAYER_PROMPT=<your standing orders>`
and the game asks an LLM for one **shift order** every 50 ticks:

```json
{"station":"chop","recipe":"soup","zone":"left","handoff":"Cog-D","yield_to":"none",
 "say":"I'll keep the board fed, D takes the middle counter","note":"A keeps forgetting plates."}
```

A scripted executor in the player container walks the cog there tick by tick until the next
order. The plan chooses *which job, which recipe, which half of the room, who to hand to, who to
yield to*; the executor chooses *which tile next*. `say` is heard by your team-mates next turn
and shown to spectators; `note` is private and comes back only to you.

The same image also ships four scripted baselines, selected with
`PLAYER_SCRIPTED=brigade|runner|passer|courier`. Every prompt seat falls back to `brigade` when a
plan turn produces nothing usable, so an episode always finishes.

## Scoring

```
delivered[i]      = dishes seat i carried to the pass
dishes            = sum(delivered)            # the team score
results.scores[i] = dishes + 0.01 * delivered[i]
```

Higher is better and no term is ever negative. The epsilon exists so the ladder is not a draw
machine, and it is bounded by half a dish, so the ordering is lexicographic: team dishes first,
own deliveries only as a tie-break.

## Watching

Replays are a static WebAssembly bundle, never a pod. The board shows the kitchen, the four cogs
with their carried items and alias letters, every station's live state, a dish ticker in serve
order, and a collision heat-map you can toggle -- on `crowded` and `ring` it paints the choke
bright red, which is exactly the finding those rooms exist to produce.

Cogs see each other only as `Cog-A` ... `Cog-D`, assigned by a seeded permutation. Real policy
names exist spectator-side only.

## Repo

```
src/collab_cooking/kitchens/   the eight ASCII kitchens
src/collab_cooking/game/       the mettagrid kitchen: stations, tickets, burn timers
src/collab_cooking/agent/      the scripted brain and the plan executor
src/collab_cooking/coworld/    server, player, protocol, LLM, replay, results
replay-viewer/                 the Nim -> wasm replay renderer
client/                        the broadcast page and its shared chrome
tests/                         kitchens, rules, scoring, baselines, episode, replay, LLM, viewer, manifest
```

Built on [mettagrid](https://github.com/Metta-AI/mettagrid). Forked from
`Metta-AI/coworld-overcogged` (the kitchen) and `Metta-AI/coworld-ctf` (the viewer chrome).
