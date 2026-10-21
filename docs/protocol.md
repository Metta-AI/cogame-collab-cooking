# Player protocol — `collab-cooking.player.v2`

JSON frames use the assigned `COWORLD_PLAYER_WS_URL`: `/player?slot=N&token=T`.
Authentication binds one connection to its assigned slot. Frames are bounded to 16 MiB.
The source types in `plan_protocol.py` define the exact protocol; unknown fields are rejected.

The game sends `player_config` with the slot, anonymous alias, layout, and actual
`policy_env` feature/action tables. Register once, before any observation:

```json
{"type":"register","protocol":"collab-cooking.player.v2","kind":"native","slot":2,
 "prompt":"raw standing orders","profile":{"model":"anthropic/claude-haiku-4.5",
 "max_tokens":900,"temperature":1.0,"top_p":1}}
{"type":"register","protocol":"collab-cooking.player.v2","kind":"scripted","slot":2,"baseline":"brigade"}
{"type":"register","protocol":"collab-cooking.player.v2","kind":"external","slot":2}
```

Strategy whitespace is preserved. Profiles freeze before the first private window.
Duplicate, late, and wrong-seat registrations are rejected. Registration records
player inputs; it does not establish model or checkpoint identity.

Native players call only their injected `COWORLD_LLM_ENDPOINT`, with authenticated
player-slot attribution. A missing endpoint is an error, with no provider credential
fallback. Temperature 1/top-p 1 is an explicit native profile; previous calls omitted
these controls. Profiles may declare temperature 0–1 and an output budget up to 4096.

Every physical tick carries an engine-issued observation UUID:

```json
{"type":"observation","protocol":"collab-cooking.player.v2","request_id":"<uuid>",
 "slot":2,"step":350,"observation":[[0,1,2]]}
{"type":"action","protocol":"collab-cooking.player.v2","request_id":"<same uuid>",
 "slot":2,"action_name":"move_north"}
```

These are actual private engine tokens, including visible teammate inventory.
Unseen inventories, spectator heat, and unseen board updates are unavailable.
The game independently runs the shipped controller on the same issued tokens.
For native/scripted containers, a different submitted control becomes `noop`.
External research controllers may submit any legal physical action; they receive
no native macro or teacher authority. Missing and stale actions become `noop`.

Native plan turns retain the simultaneous batch, 50-tick and 10-second minimum
cadence, shared 26-request/minute budget, 12-second batch deadline, and one retry.
`plan_grant` contains the immutable private `window`, attempt UUID/index,
source-rendered user message, and remaining timeout. Each retry requires a
source-observed rejection and consumes the same shared budget.

The player sends coalesced `plan_progress` with actual received native facts:
original request/endpoint, raw body bytes, normalized headers, HTTP status,
completion and reader-join state, served model, and sampling evidence when present.
A `plan_reply` separately carries the received text and submitted parsed directive.
The game checks the actual response against these facts and independently applies
its ordinary parser. The player cannot invent a teacher origin or serving receipt.

Only the game sends `plan_installed`, naming the window, seat, turn, parsed plan,
and rejection cause. Installation affects subsequent controller ticks; it is not
an immediate physical effect. `note` remains private; `say` enters team radio
and the public replay. Physical application is read back from actual engine buffers.

At termination, the game closes call/action admission and sends `stop` with a
fresh UUID. Players join their native readers, publishers, and writers under
one absolute cleanup deadline before sending `stopped` with the same UUID and
`joined:true`. Explicitly unresolved evidence denies that acknowledgement.
The game returns `evidence_received` for the same UUID. The bundled player then
closes its owned socket; EOF before that receipt is an error.

Unsettled ownership cannot produce a completed private episode or public result.
Historical v1 archives remain stored artifacts; v1 is no longer a live wire.
