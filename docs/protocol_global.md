# Global protocol -- `collab-cooking.global.v1`

`WS /global` is the spectator state stream and the certification runner's ping target. It keeps
answering for a 20 s shutdown grace after the artifacts are written.

The server sends a coalesced snapshot every `step_seconds`:

```json
{"protocol":"collab-cooking.global.v1","type":"state","game":"collab_cooking",
 "step":350,"max_steps":900,"layout":"forced","dishes":9,
 "scores":[9.04,9.03,9.01,9.01],"delivered":[4,3,1,1],
 "aliases":["Cog-A","Cog-B","Cog-C","Cog-D"],
 "player_names":["collab-cooking-expo", …],
 "connected":[true,true,true,true],"paused":false,"done":false,"reason":"",
 "stations":{"chop":{"veg":2,"meat":0},"pot":{"state":"cooking","timer":4},
             "fryer":{"state":"idle","timer":0},"sink":{"wash":1},
             "board":{"salad":1,"soup":2,"fries":0,"tickets":[…]},
             "counters":[[6,4,"chopped_meat"]]},
 "cogs":[{"slot":0,"alias":"Cog-A","name":"…","kind":"prompt","x":5,"y":3,
          "carrying":"chopped_veg","say":"…","connected":true}, …],
 "feed":[{"t":349,"kind":"serve","text":"Cog-C serves soup - dish 9"}]}
```

A `final` message of the same shape is sent once when the episode settles.

The socket accepts control messages:

```json
{"type":"control","command":"pause"}
{"type":"control","command":"play"}
{"type":"control","command":"speed","speed":4}
```

`GET /client/global` and `GET /client/player` serve static pages for these routes. Neither opens a
player socket: the certification runner probes both **before** starting the player pods.

There is no replay route. Replays are a static WebAssembly bundle served by the platform, never a
pod.
