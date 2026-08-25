# Player protocol -- `collab-cooking.player.v1`

JSON text frames over `WS /player?slot=N&token=T`. A bad slot or token closes with 1008.

## game -> player, on connect: `player_config`

```json
{"type":"player_config","protocol":"collab-cooking.player.v1","slot":2,
 "connection_id":"player-2","num_agents":4,
 "action_names":["noop","move_north","move_south","move_west","move_east"],
 "observation_shape":[500,3],"policy_env":{…},"observation":{…},"control_state":{…},
 "alias":"Cog-C","layout":"forced","max_steps":900}
```

`policy_env` and `observation` carry the mettagrid feature and tag tables, which is everything a
policy needs to decode the token array.

## player -> game, once immediately after connect: `register`

```json
{"type":"register","kind":"prompt","prompt":"<= 1200 runes"}
{"type":"register","kind":"scripted","baseline":"brigade"}
```

An unknown baseline, a malformed frame, or no registration within 5 s of connect is treated as
`{"kind":"scripted","baseline":"brigade"}` -- never a disconnect.

## game -> player, every tick: `observation`

```json
{"type":"observation","protocol":"collab-cooking.player.v1","slot":2,"step":350,
 "observation":[[loc,feature,value], …],"scores":[…],"control_state":{…}}
```

The raw mettagrid token array for that agent: its egocentric window centred on the cog, its own
inventory tokens, and the global `local_position` / `last_action_move` tokens. Terrain, objects
and **each station's inventory** are visible inside the window; everything outside it is not, and
other cogs' inventories are not (mettagrid publishes an agent's inventory tokens only at its own
centre).

## game -> player, at most once per plan turn, prompt seats only: `plan`

```json
{"type":"plan","protocol":"collab-cooking.player.v1","turn":7,"step":350,
 "station":"chop","recipe":"soup","zone":"left","handoff":"Cog-D","yield_to":"none",
 "say":"I'll keep the board fed, D takes the middle counter","note":"","src":"llm"}
```

`src` is `"llm"` or `"fallback:<cause>"`; a fallback carries an empty `station`, which means "drop
the directive and run your baseline". Scripted seats never receive this message.

## player -> game, every tick: `action`

```json
{"type":"action","action_name":"move_north",
 "policy_infos":{"policy_name":"Cog-C","task":"chop veg"},"request_id":"step-350"}
```

An action whose `request_id` is not this step is treated as `noop`.

## game -> player, once at the end: `final`

```json
{"type":"final","done":true,"reason":"complete","scores":[…],"dishes":37,
 "names":[…],"aliases":[…], …}
```

After which the player exits 0. The player's receive loop catches every connection error and
still exits 0: the game's own shutdown can outrun the flushed final frame, and a non-zero player
exit fails the episode.
