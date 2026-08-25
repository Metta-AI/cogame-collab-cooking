# Rules

## The dish chain

1. **Fetch** a `veg` or a `meat` from its station, or a `clean_plate` from the plate stack.
2. **Chop** it at the board: three uses turn `veg` into `chopped_veg` (same for meat). Chopped
   items can be stashed on the board and taken back off it.
3. **Cook** it:
   * **soup** -- `chopped_veg` + `chopped_meat` in the pot (either order, or one from your hands
     and one already loaded). 10 ticks to ready; 14 more and it burns and must be cleared.
   * **fries** -- `chopped_veg` in the fryer. 8 ticks to ready; burns at 11.
   * **salad** -- a `clean_plate` used on a board holding `chopped_veg`.
4. **Plate** it: a cog holding a `clean_plate` at a ready pot or fryer gets the dish.
5. **Serve** it at the pass against a **live ticket** for that recipe. With no live ticket for
   that recipe, nothing happens. The dish becomes a `dirty_plate` in your hands.
6. **Wash** it: three uses at the sink turn a `dirty_plate` into a `clean_plate` for the third
   user.

**You can carry exactly one thing.** A counter -- every wall in the kitchen -- also holds exactly
one item: walk into it holding something and you put it down; walk into it empty-handed and you
pick up whatever is there.

## Tickets

The whole episode's ticket schedule is laid down at config time: the first arrives at tick 0,
then every 18 ticks, recipes cycling `soup, salad, soup, fries, salad`, each expiring 50 ticks
after it arrives. At 900 ticks that is 50 tickets, of which at most 8 can be live at once; an
arrival is skipped if 8 are already live.

## The tick

One tick is one simultaneous action from every seat. Ties everywhere resolve by ascending slot.

1. **Ingest** -- a seat whose latest action does not carry `request_id == "step-<t>"` contributes
   `noop`; so does a disconnected seat.
2. **Apply** every seat's action, ascending slot.
3. **Step the engine** -- movement resolves (a move into a wall, a station or an occupied tile
   fails and you stay put), then the object a blocked mover walked into runs its handlers, then
   the timestep's events fire: ticket arrivals and expiries, the cook/burn timers, queue pressure.
4. **Read state** -- every object's position and inventory, each cog's carried item, whether its
   last action succeeded, and the per-seat dish counts.
5. **Derive events** by diffing against the previous tick.
6. **Record** the tick into the replay.
7. **Plan boundary** -- every 50 ticks, one parallel batch of LLM calls goes out. The tick loop
   never waits for it.
8. **Deliver** any plan that landed since the last tick.
9. **Observe** -- send every connected seat its observation, wait up to 0.30 s for this step's
   actions, then sleep the rest of the tick.
10. **Deadline guard** -- settle at 60 % of the episode budget, measured from process start.
11. **End** at 900 ticks.

## Scoring

```
delivered[i]      = dishes seat i carried to the pass
dishes            = sum(delivered)
results.scores[i] = dishes + 0.01 * delivered[i]
```

Higher is better; no term is ever negative. Expired orders and burned pots subtract nothing --
they cost dishes, which is the only currency. The epsilon is bounded by `0.01 * 50 = 0.5`,
strictly less than one dish, so a cog that hogs the walk to the pass to farm it loses whole
dishes to gain hundredths.

## How an episode ends

* `complete` -- 900 ticks ran. The normal path.
* `deadline` -- the wall-clock guard fired. Scored exactly as it stands, artifacts written, exit
  0. It should never fire.
* `no_players` -- zero seats connected within 120 s. All-zero scores, artifacts written, exit 0.

If *some* seats connect the episode runs with the seats it has: absent seats noop every tick and
score 0. The game never exits non-zero on a player-side problem and never waits on a player
socket without a bound.
