## collab-cooking static replay renderer (Nim -> wasm).
##
## The sim is Python on a C++ mettagrid core and does not compile to wasm --
## and it must not be reimplemented here, which would be a second source of
## truth for the rules. It does not need to be: the replay records every
## tick's fully settled state, so **every frame is recorded, not derived**.
##
## `cc_load_replay` parses the replay JSON, validates the required keys and
## the event vocabulary, and builds the frame table; `cc_frame` composes tick
## i into a Bitworld sprite_v1 packet (bitworld/spriteprotocol) -- the kitchen
## tiles from `kitchen.rows`, one sprite per station with its state, one per
## counter holding an item, one per cog with its carried item and its alias
## letter, the heat overlay when it is on -- which coworld-ctf's
## client/broadcast_core.js draws unchanged. Chrome JSON rides the reserved
## sprite 4090's label, ctf's convention, which broadcast_core.js already
## routes to onText without registering it as drawable. A malformed replay
## sets `lastError`, returns 0, and the shell turns that into
## `data-replay-error`.
##
## Export surface (the same shape as ctf_replay.nim): cc_load_replay,
## cc_frame, cc_input, cc_packet_ptr/len, cc_error_ptr/len, cc_stage_ptr/len.

import
  std/[json, tables, strutils, math, sets, unicode],
  bitworld/spriteprotocol, pixie

const
  ChromeSpriteId = 4090          # label carries chrome JSON (ctf convention)
  MapLayerId = 0
  MapLayerType = 0
  ZoomableFlag = 1
  BandObjectBase = 40            # static terrain bands: ids 40..99 in the client
  MaxBands = 60
  StaticBandZ = -32768
  Tile = 24                      # px per kitchen tile
  GenSpriteBase = 3000
  StationObjectBase = 200
  CounterObjectBase = 400
  CogObjectBase = 900
  HeatObjectBase = 1000
  MaxHeatTiles = 400
  FeedLines = 6
  TickerMax = 40
  BeatsMax = 64
  # The hard ceiling on the state JSON, enforced below. The design note calls
  # the object "<= 4 KB", which is its NOMINAL size: with MaxHeatTiles = 400
  # the heat array alone approaches 5 KB, so 4 KB is not a limit anything can
  # be held to. The guard is the number it actually fires at (r2 review
  # R2-O6); broadcast_core reads the label length as a u16, so 16 KB
  # transports fine.
  ChromeCap = 16000
  SayRunes = 120
  AliasRunes = 8                 # ALIAS_CAP in coworld/plans.py
  # A feed line carrying a say is "<alias>: <say>": the cap has to cover the
  # prefix too, or the last runes of every full-cap remark are dropped before
  # the CSS ever sees the line (r2 review R2-O4).
  FeedRunes = SayRunes + AliasRunes + 2

  EventNames = [
    "episode_start", "order_arrive", "order_expire", "pickup", "deposit",
    "chop_start", "chop_done", "pot_load", "pot_start", "pot_ready",
    "pot_burn", "pot_clear", "fry_start", "fry_ready", "fry_burn",
    "fry_clear", "plate_up", "serve", "wash_start", "wash_done", "blocked",
    "plan", "fallback", "deadline", "episode_end"
  ]
  RequiredKeys = ["format", "protocol", "config", "seed", "kitchen", "seats",
                  "ticks", "heat", "results"]
  StationKinds = ["veg_station", "meat_station", "plate_station",
                  "chopping_station", "cooking_station", "fryer_station",
                  "serving_station", "wash_station", "order_board"]
  SeatColours = [
    (232'u8, 163'u8, 61'u8),     # amber
    (63'u8, 124'u8, 196'u8),     # blue
    (69'u8, 168'u8, 94'u8),      # green
    (224'u8, 82'u8, 58'u8)       # red
  ]
  Facings = ["south", "east", "north", "west"]

type
  Rgba = object
    ## Straight-alpha RGBA pixel buffer -- what the wire wants.
    w, h: int
    data: seq[uint8]

  Cog = object
    x, y: int
    carrying: string
    action: string
    flags: int

  Tick = object
    t: int
    cogs: seq[Cog]
    stations: JsonNode           # resolved: `st` carried forward when absent
    scores: seq[int]
    events: seq[JsonNode]

  Seat = object
    slot: int
    alias, name, kind, baseline: string
    colour: int
    disconnected: bool

  Beat = object
    t: int
    kind, label: string

  Serve = object
    t: int
    recipe, alias: string

  FeedLine = object
    t: int
    kind, text: string

var
  runtimeLoaded = false
  packet: seq[uint8]
  lastError: string

  # replay
  rows: seq[string]
  boardTilesW, boardTilesH: int
  boardW, boardH: int
  stationAt: Table[string, (int, int)]
  ticks: seq[Tick]
  seats: seq[Seat]
  beats: seq[Beat]
  layoutName: string
  resultsNode: JsonNode
  finalHeat: seq[(int, int, int)]

  # playback
  playhead = 0
  playing = true
  speed = 1
  looping = false
  heatOn = false
  lastFrameTick = -1
  bandsEmitted = false

  # accumulated to the playhead
  serves: seq[Serve]
  feedAll: seq[FeedLine]
  feedSent = 0
  heatCount: Table[(int, int), int]
  heatPeak = 1
  dishes = 0
  expired = 0
  burnedPot = 0
  burnedFryer = 0
  seatSay: seq[string]
  seatJob: seq[string]
  seatPending: seq[bool]

  # sprite/object bookkeeping
  cogArt: array[4, Rgba]
  artLoaded = false
  genSprites: Table[string, int]
  nextGenSprite = GenSpriteBase
  liveObjects: HashSet[int]

# --- Progress stage note ---
## wasm32 has no memory protection: when emscripten's malloc fails a write
## through the nil pointer lands at address 0 and silently corrupts the
## module's own globals. The bundle is linked with -s ABORTING_MALLOC=1 and
## this fixed buffer, stamped BEFORE each risky phase, stays readable from JS
## after the abort (aborting kills the call stack, not the linear memory).
var
  stageNote: array[192, char]
  stageNoteLen: int
  currentStage: string

proc stampStage(stage: string) =
  currentStage = stage
  stageNoteLen = min(stage.len, stageNote.len)
  if stageNoteLen > 0:
    copyMem(stageNote[0].addr, stage[0].unsafeAddr, stageNoteLen)

proc bytesFromPointer(data: ptr uint8, length: int): string =
  result = newString(length)
  if length > 0:
    copyMem(result[0].addr, data, length)

# ---------------------------------------------------------------------------
# Pixel buffers

proc newRgba(w, h: int): Rgba =
  Rgba(w: w, h: h, data: newSeq[uint8](w * h * 4))

proc px(dst: var Rgba, x, y: int, r, g, b, a: uint8) =
  if x < 0 or y < 0 or x >= dst.w or y >= dst.h or a == 0: return
  let i = (y * dst.w + x) * 4
  if a == 255:
    dst.data[i] = r
    dst.data[i + 1] = g
    dst.data[i + 2] = b
    dst.data[i + 3] = 255
    return
  let sa = int(a)
  let da = int(dst.data[i + 3])
  let outA = sa + da * (255 - sa) div 255
  if outA == 0: return
  dst.data[i] = uint8((int(r) * sa + int(dst.data[i]) * da * (255 - sa) div 255) div outA)
  dst.data[i + 1] = uint8((int(g) * sa + int(dst.data[i + 1]) * da * (255 - sa) div 255) div outA)
  dst.data[i + 2] = uint8((int(b) * sa + int(dst.data[i + 2]) * da * (255 - sa) div 255) div outA)
  dst.data[i + 3] = uint8(outA)

proc rect(dst: var Rgba, x, y, w, h: int, r, g, b: uint8, a: uint8 = 255) =
  for yy in y ..< y + h:
    for xx in x ..< x + w:
      dst.px(xx, yy, r, g, b, a)

proc disc(dst: var Rgba, cx, cy, radius: int, r, g, b: uint8, a: uint8 = 255) =
  for yy in cy - radius .. cy + radius:
    for xx in cx - radius .. cx + radius:
      let dx = xx - cx
      let dy = yy - cy
      if dx * dx + dy * dy <= radius * radius:
        dst.px(xx, yy, r, g, b, a)

proc blit(dst: var Rgba, src: Rgba, dx, dy: int) =
  for y in 0 ..< src.h:
    for x in 0 ..< src.w:
      let si = (y * src.w + x) * 4
      dst.px(dx + x, dy + y, src.data[si], src.data[si + 1], src.data[si + 2],
             src.data[si + 3])

proc scaledTo(src: Rgba, size: int): Rgba =
  ## Nearest-neighbour box average: the 96 px render down to the 24 px tile.
  result = newRgba(size, size)
  if src.w == 0 or src.h == 0: return
  for y in 0 ..< size:
    for x in 0 ..< size:
      let x0 = x * src.w div size
      let x1 = max(x0 + 1, (x + 1) * src.w div size)
      let y0 = y * src.h div size
      let y1 = max(y0 + 1, (y + 1) * src.h div size)
      var r, g, b, a = 0
      var n = 0
      for sy in y0 ..< y1:
        for sx in x0 ..< x1:
          let si = (sy * src.w + sx) * 4
          let pa = int(src.data[si + 3])
          r += int(src.data[si]) * pa
          g += int(src.data[si + 1]) * pa
          b += int(src.data[si + 2]) * pa
          a += pa
          inc n
      if n == 0 or a == 0: continue
      let di = (y * size + x) * 4
      result.data[di] = uint8(r div a)
      result.data[di + 1] = uint8(g div a)
      result.data[di + 2] = uint8(b div a)
      result.data[di + 3] = uint8(a div n)

proc tinted(src: Rgba, r, g, b: uint8, strength: int): Rgba =
  ## Seat colour wash over the neutral steel render; alpha is untouched.
  result = newRgba(src.w, src.h)
  for i in 0 ..< src.w * src.h:
    let o = i * 4
    let a = int(src.data[o + 3])
    result.data[o + 3] = uint8(a)
    if a == 0: continue
    for c in 0 .. 2:
      let base = int(src.data[o + c])
      let tint = int(if c == 0: r elif c == 1: g else: b)
      result.data[o + c] = uint8((base * (100 - strength) + tint * strength) div 100)

proc imageToStraightRgba(image: Image): Rgba =
  ## pixie images are premultiplied RGBX; the wire is straight RGBA.
  result = newRgba(image.width, image.height)
  for i in 0 ..< image.width * image.height:
    let p = image.data[i]
    let a = int(p.a)
    let o = i * 4
    if a == 0: continue
    result.data[o] = uint8(min(255, int(p.r) * 255 div a))
    result.data[o + 1] = uint8(min(255, int(p.g) * 255 div a))
    result.data[o + 2] = uint8(min(255, int(p.b) * 255 div a))
    result.data[o + 3] = uint8(a)

# ---------------------------------------------------------------------------
# A 3x5 bitmap font, just wide enough for an alias letter and a count. Baking
# the glyph into the sprite keeps EVERY string off the 2D canvas, which is why
# viewer_smoke's --strict-text-bounds can hold canvas_text.never_inside == 0.

const Glyphs: array[14, (char, array[5, uint8])] = [
  ('A', [0b010'u8, 0b101, 0b111, 0b101, 0b101]),
  ('B', [0b110'u8, 0b101, 0b110, 0b101, 0b110]),
  ('C', [0b011'u8, 0b100, 0b100, 0b100, 0b011]),
  ('D', [0b110'u8, 0b101, 0b101, 0b101, 0b110]),
  ('0', [0b111'u8, 0b101, 0b101, 0b101, 0b111]),
  ('1', [0b010'u8, 0b110, 0b010, 0b010, 0b111]),
  ('2', [0b111'u8, 0b001, 0b111, 0b100, 0b111]),
  ('3', [0b111'u8, 0b001, 0b111, 0b001, 0b111]),
  ('4', [0b101'u8, 0b101, 0b111, 0b001, 0b001]),
  ('5', [0b111'u8, 0b100, 0b111, 0b001, 0b111]),
  ('6', [0b111'u8, 0b100, 0b111, 0b101, 0b111]),
  ('7', [0b111'u8, 0b001, 0b010, 0b010, 0b010]),
  ('8', [0b111'u8, 0b101, 0b111, 0b101, 0b111]),
  ('9', [0b111'u8, 0b101, 0b111, 0b001, 0b111])
]

proc drawGlyph(dst: var Rgba, ch: char, x, y: int, r, g, b: uint8) =
  for entry in Glyphs:
    if entry[0] != ch: continue
    for row in 0 .. 4:
      let bits = entry[1][row]
      for col in 0 .. 2:
        if (bits shr (2 - col) and 1'u8) != 0'u8:
          dst.px(x + col, y + row, r, g, b, 255)
    return

# ---------------------------------------------------------------------------
# Kitchen art. Station, prop and tile art are pixel patterns baked here; the
# cogs are the nano-banana renders preloaded at data/art/cog_<facing>.png.

proc loadCogArt() =
  if artLoaded: return
  stampStage("decode cog art")
  for i, facing in Facings:
    cogArt[i] = imageToStraightRgba(decodeImage(readFile("data/art/cog_" & facing & ".png")))
  artLoaded = true

proc floorTile(x, y: int): Rgba =
  ## Cool quarry tile with a checker, so movement reads at a glance and the
  ## warm butcher-block counters stay obviously different from the floor.
  result = newRgba(Tile, Tile)
  let dark = ((x + y) and 1) == 1
  let base: uint8 = if dark: 96 else: 108
  result.rect(0, 0, Tile, Tile, base - 14, base - 6, base)
  result.rect(0, 0, Tile, 1, base, base + 8, base + 14)
  result.rect(0, Tile - 1, Tile, 1, base - 30, base - 24, base - 16)

proc counterTile(): Rgba =
  ## Butcher block with an edge highlight -- the counters ARE the walls.
  result = newRgba(Tile, Tile)
  result.rect(0, 0, Tile, Tile, 122, 84, 48)
  for i in 0 ..< 4:
    result.rect(0, i * 6 + 2, Tile, 1, 100, 66, 36)
  result.rect(0, 0, Tile, 2, 158, 116, 72)
  result.rect(0, Tile - 2, Tile, 2, 78, 50, 26)

proc itemArt(name: string): Rgba =
  ## The nine carryables, at tile scale.
  result = newRgba(Tile, Tile)
  case name
  of "veg":
    result.disc(12, 13, 6, 92, 172, 78)
    result.rect(11, 4, 2, 5, 70, 132, 58)
  of "meat":
    result.disc(12, 13, 6, 190, 84, 84)
    result.rect(9, 15, 7, 3, 240, 226, 210)
  of "chopped_veg":
    for i in 0 .. 2:
      result.rect(7 + i * 4, 11, 3, 3, 120, 200, 96)
      result.rect(8 + i * 4, 16, 3, 3, 104, 180, 84)
  of "chopped_meat":
    for i in 0 .. 2:
      result.rect(7 + i * 4, 11, 3, 3, 208, 108, 104)
      result.rect(8 + i * 4, 16, 3, 3, 186, 88, 88)
  of "clean_plate":
    result.disc(12, 13, 8, 236, 232, 222)
    result.disc(12, 13, 5, 210, 206, 196)
  of "dirty_plate":
    result.disc(12, 13, 8, 168, 160, 142)
    result.disc(12, 13, 5, 132, 120, 96)
    result.disc(10, 12, 2, 96, 82, 60)
  of "dish_salad":
    result.disc(12, 13, 8, 236, 232, 222)
    result.disc(12, 13, 5, 120, 200, 96)
  of "dish_soup":
    result.disc(12, 13, 8, 236, 232, 222)
    result.disc(12, 13, 5, 214, 150, 72)
  of "dish_fries":
    result.disc(12, 13, 8, 236, 232, 222)
    for i in 0 .. 3:
      result.rect(8 + i * 2, 9, 1, 8, 232, 196, 86)
  else:
    result.disc(12, 13, 5, 200, 200, 200)

proc stationArt(kind, state: string, progress: int): Rgba =
  ## One 24 px sprite per station per state. No placeholder box is used.
  result = counterTile()
  case kind
  of "veg_station":
    result.rect(3, 6, 18, 15, 96, 72, 44)
    result.disc(9, 12, 4, 92, 172, 78)
    result.disc(15, 15, 4, 120, 200, 96)
  of "meat_station":
    result.rect(2, 5, 20, 3, 150, 150, 156)
    result.disc(8, 14, 5, 190, 84, 84)
    result.disc(16, 13, 4, 172, 70, 70)
  of "plate_station":
    for i in 0 .. 3:
      result.disc(12, 18 - i * 3, 7, uint8(236 - i * 6), uint8(232 - i * 6),
                  uint8(222 - i * 6))
  of "chopping_station":
    result.rect(3, 8, 18, 12, 196, 164, 112)
    result.rect(4, 9, 16, 1, 172, 140, 92)
    result.rect(14, 4, 2, 9, 190, 190, 198)      # knife
    result.rect(13, 12, 4, 2, 70, 54, 38)
    for i in 0 ..< min(progress, 3):
      result.rect(4 + i * 4, 18, 3, 2, 232, 196, 86)
  of "cooking_station":
    result.disc(12, 14, 8, 74, 74, 82)
    result.disc(12, 14, 6, 44, 44, 50)
    case state
    of "cooking":
      result.disc(12, 14, 5, 200, 132, 60)
      result.disc(9, 12, 2, 232, 176, 96)
    of "ready":
      result.disc(12, 14, 5, 236, 176, 76)
      result.rect(8, 4, 2, 5, 226, 226, 226, 160)
      result.rect(14, 3, 2, 6, 226, 226, 226, 130)
    of "burned":
      result.disc(12, 14, 5, 34, 30, 28)
      result.rect(10, 3, 2, 6, 90, 90, 90, 150)
    of "loaded":
      result.disc(12, 14, 5, 120, 150, 96)
    else:
      result.disc(12, 14, 5, 60, 60, 66)
    result.rect(2, 13, 3, 2, 90, 90, 98)
    result.rect(19, 13, 3, 2, 90, 90, 98)
  of "fryer_station":
    result.rect(3, 7, 18, 14, 78, 78, 86)
    result.rect(5, 9, 14, 10, 46, 46, 52)
    case state
    of "cooking": result.rect(5, 12, 14, 7, 190, 150, 70)
    of "ready":
      result.rect(5, 11, 14, 8, 232, 196, 86)
      for i in 0 .. 3: result.rect(6 + i * 3, 8, 2, 5, 240, 214, 120)
    of "burned": result.rect(5, 11, 14, 8, 40, 34, 30)
    else: result.rect(5, 14, 14, 5, 60, 60, 68)
  of "serving_station":
    result.rect(2, 4, 20, 10, 46, 40, 34)        # hatch
    result.rect(3, 5, 18, 8, 214, 200, 176)
    result.rect(2, 15, 20, 2, 150, 150, 156)     # ticket rail
    for i in 0 .. 2:
      result.rect(4 + i * 6, 17, 4, 4, 240, 236, 224)
  of "wash_station":
    result.rect(3, 7, 18, 13, 150, 156, 162)
    result.rect(5, 9, 14, 9, 92, 108, 120)
    result.rect(11, 3, 2, 5, 176, 180, 186)      # tap
    for i in 0 ..< min(progress, 3):
      result.disc(8 + i * 4, 12, 2, 236, 240, 246, 200)
  of "order_board":
    result.rect(2, 3, 20, 18, 92, 62, 36)
    result.rect(3, 4, 18, 16, 232, 226, 208)
    for i in 0 .. 2:
      result.rect(5, 6 + i * 5, 14, 3, 200, 190, 170)
  else:
    result.rect(6, 6, 12, 12, 180, 180, 180)

proc cogSprite(colour: int, facing: int, carrying, alias: string,
               pending: bool, disconnected: bool): Rgba =
  let tint = SeatColours[colour mod 4]
  let strength = if disconnected: 12 else: 42
  let body = cogArt[facing mod 4].scaledTo(Tile).tinted(
    tint[0], tint[1], tint[2], strength)
  # Ground pip in the seat colour goes UNDER the body: a ring around it would
  # hide the apron and the carried item.
  result = newRgba(Tile, Tile)
  result.disc(Tile div 2, Tile - 3, 5, tint[0], tint[1], tint[2], 150)
  result.blit(body, 0, 0)
  if carrying.len > 0:
    let item = itemArt(carrying).scaledTo(14)
    result.blit(item, Tile div 2 - 7, Tile - 15)
  # Alias letter above the head, in the seat colour on an ink plate.
  if alias.len > 0:
    result.rect(Tile div 2 - 3, 0, 7, 7, 20, 15, 10, 205)
    result.drawGlyph(alias[alias.len - 1], Tile div 2 - 1, 1,
                     tint[0], tint[1], tint[2])
  if pending:
    result.rect(Tile - 5, 1, 4, 4, 232, 226, 208, 230)

proc heatSprite(level: int): Rgba =
  result = newRgba(Tile, Tile)
  let a = uint8(min(200, 30 + level * 34))
  result.rect(1, 1, Tile - 2, Tile - 2, 226, 74, 48, a)

# ---------------------------------------------------------------------------
# Sprite registry

proc knownSprite(key: string): int =
  ## The client caches sprites by id, so a sprite is defined exactly once and
  # every later frame just references it.
  if genSprites.hasKey(key): genSprites[key] else: -1

proc registerSprite(key: string, pixels: Rgba): int =
  result = nextGenSprite
  inc nextGenSprite
  genSprites[key] = result
  packet.addSprite(result, pixels.w, pixels.h, pixels.data, key)

# ---------------------------------------------------------------------------
# Replay parsing

proc fail(message: string) =
  raise newException(ValueError, message)

proc parseReplay(raw: string) =
  stampStage("parse replay json")
  let doc = parseJson(raw)
  if doc.kind != JObject: fail("replay is not a JSON object")
  for key in RequiredKeys:
    if not doc.hasKey(key): fail("replay is missing the required key '" & key & "'")
  if not doc["format"].getStr.startsWith("collab-cooking/"):
    fail("replay format '" & doc["format"].getStr & "' is not collab-cooking/1")

  stampStage("read kitchen")
  layoutName = doc{"layout"}.getStr("")
  let kitchen = doc["kitchen"]
  rows = @[]
  for row in kitchen["rows"].getElems():
    rows.add row.getStr
  if rows.len == 0: fail("replay kitchen has no rows")
  boardTilesW = kitchen{"w"}.getInt(rows[0].len)
  boardTilesH = kitchen{"h"}.getInt(rows.len)
  boardW = boardTilesW * Tile
  boardH = boardTilesH * Tile
  stationAt = initTable[string, (int, int)]()
  for entry in kitchen["stations"].getElems():
    stationAt[entry{"kind"}.getStr("")] = (entry{"x"}.getInt(0), entry{"y"}.getInt(0))

  stampStage("read seats")
  seats = @[]
  for entry in doc["seats"].getElems():
    seats.add Seat(
      slot: entry{"slot"}.getInt(seats.len),
      alias: entry{"alias"}.getStr("Cog-?"),
      name: entry{"name"}.getStr(""),
      kind: entry{"kind"}.getStr("scripted"),
      baseline: entry{"baseline"}.getStr(""),
      colour: entry{"color"}.getInt(seats.len),
      disconnected: entry{"disconnected"}.getBool(false))
  if seats.len == 0: fail("replay has no seats")

  stampStage("read ticks")
  ticks = @[]
  var carried = newJObject()
  for record in doc["ticks"]:
    var tick = Tick(t: record{"t"}.getInt(ticks.len))
    let cogsNode = record{"c"}
    if cogsNode != nil and cogsNode.kind == JArray:
      for entry in cogsNode.getElems():
        var cog = Cog()
        let items = entry.getElems()
        if items.len >= 5:
          cog.x = items[0].getInt
          cog.y = items[1].getInt
          cog.carrying = items[2].getStr("")
          cog.action = items[3].getStr("noop")
          cog.flags = items[4].getInt
        tick.cogs.add cog
    if record.hasKey("st"):
      carried = record["st"]
    tick.stations = carried
    let scoresNode = record{"sc"}
    if scoresNode != nil and scoresNode.kind == JArray:
      for entry in scoresNode.getElems():
        tick.scores.add entry.getInt
    let eventsNode = record{"ev"}
    if eventsNode != nil and eventsNode.kind == JArray:
      for entry in eventsNode.getElems():
        let name = entry{"ev"}.getStr("")
        if name notin EventNames:
          fail("replay carries an unknown event '" & name & "'")
        tick.events.add entry
    ticks.add tick
  if ticks.len == 0: fail("replay has no ticks")

  stampStage("read heat")
  finalHeat = @[]
  for entry in doc["heat"].getElems():
    let items = entry.getElems()
    if items.len >= 3:
      finalHeat.add (items[0].getInt, items[1].getInt, items[2].getInt)
  resultsNode = doc["results"]

  stampStage("build beats")
  # `beats` ships COMPLETE on the first frame (ctf's ingestBeats pattern) so
  # the scrubber tells the story before playback reaches it.
  beats = @[]
  var dish = 0
  for tick in ticks:
    for event in tick.events:
      case event{"ev"}.getStr("")
      of "serve":
        inc dish
        beats.add Beat(t: tick.t, kind: "serve",
          label: "Dish " & $dish & " - " & event{"alias"}.getStr("a cog") &
                 " serves " & event{"recipe"}.getStr("a dish"))
      of "pot_burn":
        beats.add Beat(t: tick.t, kind: "burn", label: "Pot burns")
      of "fry_burn":
        beats.add Beat(t: tick.t, kind: "burn", label: "Fryer burns")
      of "order_expire":
        beats.add Beat(t: tick.t, kind: "expire",
          label: "Ticket " & event{"recipe"}.getStr("") & " expires")
      of "plan":
        beats.add Beat(t: tick.t, kind: "plan",
          label: event{"alias"}.getStr("a cog") & " takes " &
                 event{"station"}.getStr("a station"))
      of "episode_end":
        beats.add Beat(t: tick.t, kind: "end", label: "Service closes")
      else: discard
  # A jam beat marks the busiest doorway once, at the tick that doorway has
  # taken half of all the jams it takes in the episode.
  if finalHeat.len > 0:
    var bx, by, bn = 0
    for entry in finalHeat:
      if entry[2] > bn:
        bx = entry[0]; by = entry[1]; bn = entry[2]
    if bn >= 8:
      # Counted at the busiest tile ONLY: accumulating every blocked event
      # anywhere put the beat at the first tick where the whole kitchen's
      # jam total passed half of one doorway's -- tick 36 of 480 on the CI
      # replay, nowhere near that doorway's peak (r2 review R2-O7).
      var running = 0
      for tick in ticks:
        for event in tick.events:
          if event{"ev"}.getStr("") == "blocked" and
             event{"x"}.getInt == bx and event{"y"}.getInt == by:
            inc running
        if running * 2 >= bn:
          beats.add Beat(t: tick.t, kind: "jam", label: "Jam at the doorway")
          break
  if beats.len > BeatsMax:
    # Keep the story, drop the middle noise: serves and the ending always win.
    var kept: seq[Beat]
    for beat in beats:
      if beat.kind == "serve" or beat.kind == "end": kept.add beat
    for beat in beats:
      if kept.len >= BeatsMax: break
      if beat.kind != "serve" and beat.kind != "end": kept.add beat
    beats = kept

# ---------------------------------------------------------------------------
# Playhead accumulation

proc resetAccumulators() =
  serves = @[]
  feedAll = @[]
  feedSent = 0
  heatCount = initTable[(int, int), int]()
  heatPeak = 1
  dishes = 0
  expired = 0
  burnedPot = 0
  burnedFryer = 0
  seatSay = newSeq[string](seats.len)
  seatJob = newSeq[string](seats.len)
  seatPending = newSeq[bool](seats.len)

proc truncRunes(text: string, cap: int): string =
  ## Rune-boundary truncation: a byte cut mid-rune is exactly what makes a
  # JSON string that renders in a browser fail a strict parser.
  if text.runeLen <= cap: return text
  result = ""
  var taken = 0
  for r in text.runes:
    if taken >= cap: break
    result.add($r)
    inc taken

proc absorb(tick: Tick) =
  for event in tick.events:
    let name = event{"ev"}.getStr("")
    case name
    of "serve":
      inc dishes
      serves.add Serve(t: tick.t, recipe: event{"recipe"}.getStr("salad"),
                       alias: event{"alias"}.getStr("a cog"))
      if serves.len > TickerMax: serves.delete(0)
      feedAll.add FeedLine(t: tick.t, kind: "serve",
        text: event{"alias"}.getStr("a cog") & " serves " &
              event{"recipe"}.getStr("a dish") & " - dish " & $dishes)
    of "order_expire":
      inc expired
      feedAll.add FeedLine(t: tick.t, kind: "expire",
        text: "a " & event{"recipe"}.getStr("") & " ticket expires - nobody served it")
    of "pot_burn":
      inc burnedPot
      feedAll.add FeedLine(t: tick.t, kind: "burn", text: "the pot burns - nobody plated it")
    of "fry_burn":
      inc burnedFryer
      feedAll.add FeedLine(t: tick.t, kind: "burn", text: "the fryer burns - nobody plated it")
    of "deposit":
      feedAll.add FeedLine(t: tick.t, kind: "handoff",
        text: event{"alias"}.getStr("a cog") & " leaves " &
              event{"item"}.getStr("an item").replace("_", " ") & " on a counter")
    of "blocked":
      let key = (event{"x"}.getInt, event{"y"}.getInt)
      let now = heatCount.getOrDefault(key, 0) + 1
      heatCount[key] = now
      if now > heatPeak: heatPeak = now
    of "plan":
      let slot = event{"slot"}.getInt(0)
      if slot >= 0 and slot < seatSay.len:
        seatSay[slot] = event{"say"}.getStr("")
        seatJob[slot] = event{"station"}.getStr("")
        seatPending[slot] = false
      if event{"say"}.getStr("").len > 0:
        feedAll.add FeedLine(t: tick.t, kind: "say",
          text: truncRunes(event{"alias"}.getStr("a cog"), AliasRunes) & ": " &
                truncRunes(event{"say"}.getStr(""), SayRunes))
    of "fallback":
      let slot = event{"slot"}.getInt(0)
      if slot >= 0 and slot < seatPending.len: seatPending[slot] = false
      feedAll.add FeedLine(t: tick.t, kind: "fallback",
        text: event{"alias"}.getStr("a cog") & " fell back to brigade - " &
              event{"cause"}.getStr("unknown"))
    else: discard
  if feedAll.len > 400:
    feedAll.delete(0)
    if feedSent > 0: dec feedSent

proc rebuildTo(target: int) =
  resetAccumulators()
  for i in 0 .. min(target, ticks.len - 1):
    absorb(ticks[i])
  feedSent = max(0, feedAll.len - FeedLines)

# ---------------------------------------------------------------------------
# Board rendering

proc bakeTerrain(): Rgba =
  stampStage("bake kitchen floor")
  result = newRgba(boardW, boardH)
  for y in 0 ..< boardTilesH:
    let row = if y < rows.len: rows[y] else: ""
    for x in 0 ..< boardTilesW:
      let ch = if x < row.len: row[x] else: '#'
      var tile = if ch == '#': counterTile() else: floorTile(x, y)
      result.blit(tile, x * Tile, y * Tile)

proc emitBands() =
  let terrain = bakeTerrain()
  stampStage("emit kitchen bands")
  var bandH = max(Tile, (boardH + MaxBands - 1) div MaxBands)
  bandH = ((bandH + Tile - 1) div Tile) * Tile
  var y = 0
  var band = 0
  while y < boardH and band < MaxBands:
    let h = min(bandH, boardH - y)
    packet.addSprite(BandObjectBase + band, boardW, h,
      terrain.data.toOpenArray(y * boardW * 4, (y + h) * boardW * 4 - 1),
      "kitchen band " & $band)
    packet.addObject(BandObjectBase + band, 0, y, StaticBandZ, MapLayerId,
                     BandObjectBase + band)
    y += h
    inc band
  bandsEmitted = true

proc place(seen: var HashSet[int], id, x, y, z, spriteId: int) =
  seen.incl id
  liveObjects.incl id
  packet.addObject(id, clamp(x, -32000, 32000), clamp(y, -32000, 32000),
                   clamp(z, -32000, 32000), MapLayerId, spriteId)

proc facingFor(action: string): int =
  result = case action
    of "move_east": 1
    of "move_north": 2
    of "move_west": 3
    else: 0

proc stationState(stations: JsonNode, kind: string): (string, int) =
  result = ("idle", 0)
  if stations == nil or stations.kind != JObject: return
  case kind
  of "cooking_station":
    result = (stations{"pot", "state"}.getStr("idle"), stations{"pot", "timer"}.getInt(0))
  of "fryer_station":
    result = (stations{"fryer", "state"}.getStr("idle"),
              stations{"fryer", "timer"}.getInt(0))
  of "chopping_station":
    result = ("idle", max(stations{"chop", "veg"}.getInt(0),
                          stations{"chop", "meat"}.getInt(0)))
  of "wash_station":
    result = ("idle", stations{"sink", "wash"}.getInt(0))
  else: discard

proc emitFrame() =
  let tick = ticks[clamp(playhead, 0, ticks.len - 1)]
  var seen: HashSet[int]

  stampStage("place stations")
  for index, kind in StationKinds:
    if not stationAt.hasKey(kind): continue
    let pos = stationAt[kind]
    let stateAndProgress = stationState(tick.stations, kind)
    let key = "station|" & kind & "|" & stateAndProgress[0] & "|" & $stateAndProgress[1]
    var sprite = knownSprite(key)
    if sprite < 0:
      sprite = registerSprite(key, stationArt(kind, stateAndProgress[0], stateAndProgress[1]))
    seen.place(StationObjectBase + index, pos[0] * Tile, pos[1] * Tile, 5, sprite)

  stampStage("place counters")
  var counterIndex = 0
  let countersNode = tick.stations{"counters"}
  if countersNode != nil and countersNode.kind == JArray:
    for entry in countersNode.getElems():
      let items = entry.getElems()
      if items.len < 3: continue
      if counterIndex >= 400: break
      let name = items[2].getStr("")
      var sprite = knownSprite("item|" & name)
      if sprite < 0:
        sprite = registerSprite("item|" & name, itemArt(name))
      seen.place(CounterObjectBase + counterIndex,
                 items[0].getInt * Tile, items[1].getInt * Tile, 6, sprite)
      inc counterIndex

  stampStage("place heat")
  var heatIndex = 0
  if heatOn:
    for key, count in heatCount:
      if heatIndex >= MaxHeatTiles: break
      let level = (count * 5) div max(1, heatPeak)
      var sprite = knownSprite("heat|" & $level)
      if sprite < 0:
        sprite = registerSprite("heat|" & $level, heatSprite(level))
      seen.place(HeatObjectBase + heatIndex, key[0] * Tile, key[1] * Tile, 2, sprite)
      inc heatIndex

  stampStage("place cogs")
  for index, cog in tick.cogs:
    if index >= seats.len: break
    let seat = seats[index]
    let facing = facingFor(cog.action)
    let pending = (cog.flags and 2) != 0
    let gone = (cog.flags and 4) != 0
    let key = "cog|" & $seat.colour & "|" & $facing & "|" & cog.carrying & "|" &
              seat.alias & "|" & (if pending: "p" else: "-") & (if gone: "d" else: "-")
    var sprite = knownSprite(key)
    if sprite < 0:
      sprite = registerSprite(key,
        cogSprite(seat.colour, facing, cog.carrying, seat.alias, pending, gone))
    seen.place(CogObjectBase + index, cog.x * Tile, cog.y * Tile,
               100 + cog.y, sprite)

  var stale: seq[int]
  for id in liveObjects:
    if id notin seen: stale.add id
  for id in stale:
    packet.addDeleteObject(id)
    liveObjects.excl id

# ---------------------------------------------------------------------------
# Chrome JSON (sprite 4090's label) -- the viewer's whole state contract.

proc chromeJson(): string =
  let tick = ticks[clamp(playhead, 0, ticks.len - 1)]
  let last = playhead >= ticks.len - 1

  var seatNodes = newJArray()
  for index, seat in seats:
    let delivered = if index < tick.scores.len: tick.scores[index] else: 0
    let cog = if index < tick.cogs.len: tick.cogs[index] else: Cog()
    seatNodes.add %*{
      "slot": seat.slot, "alias": seat.alias, "name": seat.name,
      "kind": seat.kind, "color": seat.colour, "delivered": delivered,
      "carrying": cog.carrying,
      "job": (if index < seatJob.len and seatJob[index].len > 0: seatJob[index]
              elif cog.carrying.len > 0: "carrying" else: "working"),
      "pending": (if index < seatPending.len: seatPending[index] else: false),
      "say": (if index < seatSay.len: truncRunes(seatSay[index], SayRunes) else: ""),
      "dc": seat.disconnected or ((cog.flags and 4) != 0)
    }

  var tickerNode = newJArray()
  for entry in serves:
    tickerNode.add %*{"t": entry.t, "recipe": entry.recipe, "alias": entry.alias}

  var heatNode = newJArray()
  var emitted = 0
  for key, count in heatCount:
    if emitted >= MaxHeatTiles: break
    heatNode.add %*[key[0], key[1], count]
    inc emitted

  var feedNode = newJArray()
  while feedSent < feedAll.len:
    let line = feedAll[feedSent]
    feedNode.add %*{"t": line.t, "kind": line.kind, "text": truncRunes(line.text, FeedRunes)}
    inc feedSent

  var beatNode = newJArray()
  for beat in beats:
    beatNode.add %*{"t": beat.t, "k": beat.kind, "label": truncRunes(beat.label, 60)}

  var live = 0
  var expiring = 0
  let board = tick.stations{"board"}
  if board != nil and board.kind == JObject:
    live = board{"salad"}.getInt(0) + board{"soup"}.getInt(0) + board{"fries"}.getInt(0)
    let ticketsNode = board{"tickets"}
    if ticketsNode != nil and ticketsNode.kind == JArray:
      for entry in ticketsNode.getElems():
        let expires = entry{"expires"}.getInt(-1)
        if expires >= 0 and expires - tick.t <= 12: inc expiring

  let reason = resultsNode{"reason"}.getStr("complete")
  let doc = %*{
    "tick": tick.t,
    "ticks": ticks[ticks.len - 1].t,
    "layout": layoutName,
    "phase": (if last: "gameover" else: "play"),
    "dishes": dishes,
    "live": live,
    "expiring": expiring,
    "expired": expired,
    "burned": {"pot": burnedPot, "fryer": burnedFryer},
    "playing": playing,
    "speed": speed,
    "loop": looping,
    "heatOn": heatOn
  }
  doc["reason"] = if last: newJString(reason) else: newJNull()
  doc["heat"] = heatNode
  doc["seats"] = seatNodes
  doc["ticker"] = tickerNode
  doc["feed"] = feedNode
  doc["beats"] = beatNode
  if last:
    var order = newJArray()
    for index, seat in seats:
      order.add %*{
        "alias": seat.alias, "name": seat.name,
        "delivered": (if index < tick.scores.len: tick.scores[index] else: 0)}
    let finalNode = %*{
      "reason": reason,
      "dishes": resultsNode{"dishes"}.getInt(dishes),
      "expired": resultsNode{"orders_expired"}.getInt(expired),
      "burned": {"pot": burnedPot, "fryer": burnedFryer}}
    finalNode["order"] = order
    doc["final"] = finalNode
  else:
    doc["final"] = newJNull()
  result = $doc
  if result.len > ChromeCap:
    # A pathological replay cannot be allowed to blow the label: drop the
    # beats (the scrubber degrades, the board does not).
    doc["beats"] = newJArray()
    result = $doc

proc renderCurrent() =
  packet.setLen(0)
  if not bandsEmitted:
    packet.addLayer(MapLayerId, MapLayerType, ZoomableFlag)
    packet.addViewport(MapLayerId, boardW, boardH)
    emitBands()
  emitFrame()
  packet.addSprite(ChromeSpriteId, 1, 1, [0'u8, 0, 0, 0], chromeJson())

proc seekTo(target: int) =
  let clamped = clamp(target, 0, ticks.len - 1)
  if clamped == playhead: return
  if clamped < playhead:
    rebuildTo(clamped)
  else:
    for i in playhead + 1 .. clamped:
      absorb(ticks[i])
  playhead = clamped

# ---------------------------------------------------------------------------
# Exports

proc ccLoadReplay(data: ptr uint8, length: cint): cint
    {.exportc: "cc_load_replay", cdecl.} =
  try:
    lastError = ""
    runtimeLoaded = false
    currentStage = ""
    loadCogArt()
    parseReplay(data.bytesFromPointer(int(length)))
    let note = " (kitchen " & $boardTilesW & "x" & $boardTilesH & ", " &
               $ticks.len & " ticks)"
    stampStage("reset playback" & note)
    playhead = 0
    playing = true
    speed = 1
    looping = false
    heatOn = false
    lastFrameTick = -1
    bandsEmitted = false
    genSprites = initTable[string, int]()
    nextGenSprite = GenSpriteBase
    liveObjects = initHashSet[int]()
    resetAccumulators()
    absorb(ticks[0])
    stampStage("render first frame" & note)
    renderCurrent()
    stampStage("loaded")
    runtimeLoaded = true
    return 1
  except Exception as error:
    runtimeLoaded = false
    lastError = currentStage & ": " & error.msg & "\n" & error.getStackTrace()
    return 0

proc applyCommand(text: string) =
  if text.len == 0: return
  if text.startsWith("s:"):
    let value = try: parseInt(text[2 .. ^1]) except ValueError: -1
    if value >= 0: seekTo(value)
    return
  if text.startsWith("sp:"):
    let value = try: parseInt(text[3 .. ^1]) except ValueError: 1
    speed = clamp(value, 1, 16)
    return
  case text
  of "play": playing = true
  of "pause": playing = false
  of "toggle": playing = not playing
  of "restart":
    seekTo(0)
    playing = true
  of "end": seekTo(ticks.len - 1)
  of "loop": looping = not looping
  of "heat": heatOn = not heatOn
  of "heat:1": heatOn = true
  of "heat:0": heatOn = false
  of "1": speed = 1
  of "2": speed = 2
  of "3": speed = 3
  of "4": speed = 4
  of "8": speed = 8
  of "6": speed = 16
  else: discard

proc ccInput(data: ptr uint8, length: cint) {.exportc: "cc_input", cdecl.} =
  if not runtimeLoaded: return
  try:
    for item in data.bytesFromPointer(int(length)).parseSpriteClientMessages():
      if item.kind == SpriteClientChatMessage:
        applyCommand(item.text)
  except Exception:
    discard

proc ccFrame(): cint {.exportc: "cc_frame", cdecl.} =
  if not runtimeLoaded:
    return 0
  try:
    if playing:
      if playhead >= ticks.len - 1:
        if looping:
          rebuildTo(0)
          playhead = 0
        else:
          playing = false
      else:
        seekTo(playhead + speed)
    renderCurrent()
    lastFrameTick = playhead
    return 1
  except Exception as error:
    lastError = "advance replay: " & error.msg & "\n" & error.getStackTrace()
    return -1

proc ccPacketPointer(): ptr uint8 {.exportc: "cc_packet_ptr", cdecl.} =
  if packet.len == 0:
    nil
  else:
    packet[0].addr

proc ccPacketLength(): cint {.exportc: "cc_packet_len", cdecl.} =
  cint(packet.len)

proc ccErrorPointer(): ptr uint8 {.exportc: "cc_error_ptr", cdecl.} =
  if lastError.len == 0:
    nil
  else:
    cast[ptr uint8](lastError[0].addr)

proc ccErrorLength(): cint {.exportc: "cc_error_len", cdecl.} =
  cint(lastError.len)

proc ccStagePointer(): ptr uint8 {.exportc: "cc_stage_ptr", cdecl.} =
  ## Unlike cc_error_*, this stays valid after an allocation-failure abort, so
  # JS can report what the runtime was doing when the address space ran out.
  if stageNoteLen == 0:
    nil
  else:
    cast[ptr uint8](stageNote[0].addr)

proc ccStageLength(): cint {.exportc: "cc_stage_len", cdecl.} =
  cint(stageNoteLen)

when defined(emscripten):
  proc emscriptenExitWithLiveRuntime() {.
    importc: "emscripten_exit_with_live_runtime", cdecl.}

when isMainModule and defined(emscripten):
  # Nim's generated main runs every module-global destructor when it returns,
  # freeing the parsed replay, the sprite tables and the cog art while the
  # wasm module stays alive and JS keeps calling cc_load_replay/cc_frame.
  # Unwinding main through emscripten's live-runtime exit skips the destructor
  # epilogue entirely, so globals stay valid for the life of the page.
  emscriptenExitWithLiveRuntime()
