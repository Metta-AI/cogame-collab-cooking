import std/[os, strutils, algorithm]
import supersnappy
include "../replay-viewer/collab_cooking_replay"

proc feed(text: string): string =
  ## Wrap a command the way the page's sprite chat channel does.
  blobFromSpriteChat(text)

when true:
  let raw = readFile(paramStr(1))
  var buf = newSeq[uint8](raw.len)
  if raw.len > 0: copyMem(buf[0].addr, raw[0].unsafeAddr, raw.len)
  let ok = ccLoadReplay(buf[0].addr, cint(raw.len))
  echo "load=", ok, " err=", lastError
  if ok == 0: quit(1)
  echo "packet=", packet.len, " ticks=", ticks.len, " board=", boardW, "x", boardH
  echo "chrome head=", chromeJson()[0 .. min(600, chromeJson().len - 1)]
  var frames = 0
  for i in 0 ..< 400:
    if ccFrame() < 0:
      echo "frame error: ", lastError
      quit(1)
    inc frames
  echo "frames=", frames, " playhead=", playhead, " packet=", packet.len
  # seek to 50% and 100%
  var cmd = feed("s:" & $(ticks.len div 2))
  var cb = newSeq[uint8](cmd.len)
  copyMem(cb[0].addr, cmd[0].unsafeAddr, cmd.len)
  ccInput(cb[0].addr, cint(cmd.len))
  discard ccFrame()
  echo "after seek 50%: playhead=", playhead, " dishes=", dishes
  cmd = feed("heat:0")
  cb = newSeq[uint8](cmd.len)
  copyMem(cb[0].addr, cmd[0].unsafeAddr, cmd.len)
  ccInput(cb[0].addr, cint(cmd.len))
  discard ccFrame()
  echo "heatOn=", heatOn, " packet=", packet.len
  cmd = feed("s:" & $(ticks.len - 1))
  cb = newSeq[uint8](cmd.len)
  copyMem(cb[0].addr, cmd[0].unsafeAddr, cmd.len)
  ccInput(cb[0].addr, cint(cmd.len))
  discard ccFrame()
  let ch = chromeJson()
  echo "final chrome len=", ch.len
  echo ch[max(0, ch.len - 400) .. ^1]
  # validate the packet parses as sprite_v1
  let msgs = parseSpritePacket(packet)
  echo "packet messages=", msgs.len

  # Composite the current frame into a PNG so a human can look at the board.
  var canvas = newRgba(boardW, boardH)
  var spritePixels = initTable[int, Rgba]()
  var objs: seq[SpritePacketObject]
  packet.setLen(0)
  bandsEmitted = false
  genSprites = initTable[string, int]()
  nextGenSprite = GenSpriteBase
  liveObjects = initHashSet[int]()
  renderCurrent()
  for m in parseSpritePacket(packet):
    if m.kind == spkSprite:
      let raw = supersnappy.uncompress(m.sprite.compressedPixels)
      var img = newRgba(m.sprite.width, m.sprite.height)
      if raw.len == img.data.len:
        for i in 0 ..< raw.len: img.data[i] = raw[i]
      spritePixels[m.sprite.id] = img
    elif m.kind == spkObject:
      objs.add m.objectDef
  objs.sort(proc (a, b: SpritePacketObject): int = cmp(a.z, b.z))
  for o in objs:
    if spritePixels.hasKey(o.spriteId):
      canvas.blit(spritePixels[o.spriteId], o.x, o.y)
  var png = newImage(boardW, boardH)
  for i in 0 ..< boardW * boardH:
    let a = canvas.data[i * 4 + 3]
    png.data[i] = rgbx(
      uint8(int(canvas.data[i * 4]) * int(a) div 255),
      uint8(int(canvas.data[i * 4 + 1]) * int(a) div 255),
      uint8(int(canvas.data[i * 4 + 2]) * int(a) div 255), a)
  png.writeFile("/tmp/board.png")
  echo "wrote /tmp/board.png sprites=", spritePixels.len, " objects=", objs.len
