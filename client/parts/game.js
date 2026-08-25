(function () {
  'use strict';

  // Shared replay chrome (chrome_common.js, spliced over the CHROME_COMMON
  // marker by the bundle build). A raw file:// open of this source has no
  // splice, so fail loud and early instead of throwing mid-file.
  if (typeof window.ChromeCommon !== 'function') {
    console.error('chrome_common.js was not spliced into this page');
    return;
  }

  var state = null;

  var C = window.ChromeCommon({
    send: function (command) { ccSend(command); },
    sendPov: function () { /* no POV lens: the whole kitchen is always in frame */ },
    getState: function () { return chromeState; }
  });

  // Aliases so the per-view code below reads exactly as it does in ctf. The
  // appended block must never DECLARE a function with any of these names --
  // hoisting would shadow the alias and the beats would render as unlabeled
  // dead divs (tandem, 2026-08-23). Ours are ccDishTicker, ccHeatToggle,
  // ccSayBar, ccSeatPlates.
  var $ = C.$;
  var esc = C.esc;
  var renderTransport = C.renderTransport;
  var AMBER = C.AMBER, PAPER = C.PAPER, GHOST = '#8a7f72';
  var SEAT_COLOURS = ['#e8a33d', '#3f7cc4', '#45a85e', '#e0523a'];
  var RECIPE_LABEL = { salad: 'salad', soup: 'soup', fries: 'fries' };

  var viewport = $('viewport');
  var stage = $('stage');
  var canvas = $('board');
  var statusEl = $('status');

  // ---- the chrome-shaped view of our state -------------------------------
  // chrome_common.js was authored against ctf's frame shape; the transport,
  // the scrubber and the spoiler gate read exactly these fields, so the game
  // block hands it a translation instead of a fork.
  var chromeState = {
    ph: 'lobby', lob: 0, t: 0, st: 0, mx: 1, mt: 0, sp: 1,
    en: false, pl: true, lp: false, sk: false, ff: false, teams: {}
  };

  var BOARD_W = 360, BOARD_H = 216;
  var BOARD_ASPECT = BOARD_W / BOARD_H;

  var core = null;
  var beatsPlaced = false;
  var lastDishCount = 0;
  var freshDishFrames = 0;
  var endcardShown = false;

  function ccSend(command) {
    if (core) core.sendCommand(String(command));
  }

  // Every seek dismisses the endcard, and the endcard stops at var(--band).
  function ccSeek(tick) {
    var card = $('endcard');
    if (card) card.classList.remove('on');
    endcardShown = false;
    ccSend('s:' + Math.max(0, Math.round(tick)));
  }

  // ---- readouts ----------------------------------------------------------
  var ccDishTicker = function (s) {
    var num = $('dt-num');
    if (num) num.textContent = String(s.dishes || 0);
    var strip = $('dt-strip');
    if (!strip) return;
    var chips = (s.ticker || []).slice(-8);
    if (s.dishes > lastDishCount) freshDishFrames = 24;
    lastDishCount = s.dishes || 0;
    if (freshDishFrames > 0) freshDishFrames--;
    var html = '';
    for (var i = 0; i < chips.length; i++) {
      var chip = chips[i];
      var fresh = (i === chips.length - 1) && freshDishFrames > 0;
      html += '<span class="dish-chip' + (fresh ? ' fresh' : '') + '">' +
        '<i class="' + esc(chip.recipe || 'salad') + '"></i>' +
        esc(RECIPE_LABEL[chip.recipe] || chip.recipe || 'dish') + ' &middot; ' +
        esc(chip.alias || '') + ' &middot; t' + (chip.t | 0) + '</span>';
    }
    if (strip.innerHTML !== html) strip.innerHTML = html;
  };

  var ccHeatToggle = function (on) {
    var button = $('heatbtn');
    if (!button) return;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  };

  var ccSayBar = function (s) {
    var bar = $('saybar');
    if (!bar) return;
    var html = '';
    (s.seats || []).forEach(function (seat) {
      var colour = SEAT_COLOURS[(seat.color | 0) % SEAT_COLOURS.length];
      var say = seat.say || '';
      html += '<div class="say-chip' + (say ? '' : ' empty') + '" style="--sc:' + colour + '">' +
        '<b>' + esc(seat.alias || '') + '</b> ' + esc(say || 'no word yet') + '</div>';
    });
    if (bar.innerHTML !== html) bar.innerHTML = html;
  };

  var ccSeatPlates = function (s) {
    // ctf's two team columns become two columns of the same brigade, two cog
    // plates each. No id is invented and none is repurposed silently.
    var sides = [$('plates-l'), $('plates-r')];
    if (!sides[0] || !sides[1]) return;
    var seats = s.seats || [];
    var buckets = [[], []];
    seats.forEach(function (seat, index) { buckets[index % 2].push(seat); });
    for (var side = 0; side < 2; side++) {
      var html = '';
      buckets[side].forEach(function (seat) {
        var colour = SEAT_COLOURS[(seat.color | 0) % SEAT_COLOURS.length];
        html += '<div class="plate' + (seat.pending ? ' thinking' : '') +
          (seat.dc ? ' dc' : '') + '" style="--sc:' + colour + '">' +
          '<span class="chip"></span>' +
          '<span class="plate-stack">' +
          '<span class="plate-name">' + esc(seat.alias || '') + '</span>' +
          '<span class="plate-policy">' + esc(seat.name || '') + '</span>' +
          '<span class="plate-job">' + esc(seat.job || 'waiting') + '</span>' +
          '</span>' +
          '<span class="plate-pending">&#9654;</span>' +
          '<span class="plate-dishes">' + (seat.delivered | 0) + '</span>' +
          '</div>';
      });
      if (sides[side].innerHTML !== html) sides[side].innerHTML = html;
    }
  };

  function renderClockReadout(s) {
    // Real numbers, never internal notation.
    var caption = $('clock-caption');
    var time = $('clock-time');
    var tickClock = $('tick-clock');
    var total = s.ticks || 1;
    if (time) time.textContent = 'TICK ' + (s.tick | 0) + ' OF ' + total;
    if (caption) {
      caption.textContent = (s.live | 0) + ' ORDER' + ((s.live | 0) === 1 ? '' : 'S') +
        ' LIVE' + ((s.expiring | 0) > 0 ? ' \u00b7 ' + (s.expiring | 0) + ' EXPIRING' : '');
    }
    if (tickClock) tickClock.textContent = (s.tick | 0) + ' / ' + total;
  }

  function renderFeed(s) {
    var box = $('feed');
    if (!box) return;
    (s.feed || []).forEach(function (line) {
      var row = document.createElement('div');
      row.className = 'feed-row ' + (line.kind || 'info');
      row.textContent = line.text || '';
      box.appendChild(row);
    });
    while (box.childNodes.length > 6) box.removeChild(box.firstChild);
  }

  function renderBeats(s) {
    if (beatsPlaced || !s.beats || !s.beats.length) return;
    var scrub = $('scrub');
    if (!scrub) return;
    beatsPlaced = true;
    var span = Math.max(1, s.ticks || 1);
    s.beats.forEach(function (beat) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'beat-marker ' + (beat.k || 'plan');
      button.style.left = (Math.min(1, Math.max(0, (beat.t || 0) / span)) * 100) + '%';
      button.title = beat.label || '';
      button.setAttribute('aria-label', beat.label || 'story beat');
      button.addEventListener('click', function (event) {
        event.stopPropagation();
        ccSeek(beat.t || 0);
      });
      scrub.appendChild(button);
    });
  }

  function renderEndcard(s) {
    var card = $('endcard');
    if (!card || !s.final) return;
    if (endcardShown) return;
    endcardShown = true;
    var order = (s.final.order || []).slice().sort(function (a, b) {
      return (b.delivered | 0) - (a.delivered | 0);
    });
    var headline = $('ec-headline');
    if (headline) headline.textContent = (s.final.dishes | 0) + ' DISHES SERVED';
    var wincond = $('ec-wincond');
    if (wincond) {
      wincond.textContent = 'the whole brigade shares one score' +
        (s.final.reason && s.final.reason !== 'complete'
          ? ' \u00b7 ended: ' + s.final.reason : '');
    }
    var how = $('ec-how');
    if (how) {
      var pots = (s.final.burned && s.final.burned.pot) | 0;
      var fryers = (s.final.burned && s.final.burned.fryer) | 0;
      var expiredCount = s.final.expired | 0;
      how.textContent = expiredCount + ' ticket' + (expiredCount === 1 ? '' : 's') +
        ' expired \u00b7 ' + pots + ' pot' + (pots === 1 ? '' : 's') + ' burned \u00b7 ' +
        fryers + ' fryer' + (fryers === 1 ? '' : 's') + ' burned';
    }
    var teams = $('ec-teams');
    if (teams) {
      var html = '<div class="cc-order">';
      order.forEach(function (entry, index) {
        var seat = (s.seats || []).filter(function (x) { return x.alias === entry.alias; })[0];
        var colour = SEAT_COLOURS[((seat && seat.color) | 0) % SEAT_COLOURS.length];
        html += '<div class="cc-line" style="--sc:' + colour + '">' +
          '<span class="rank">' + (index + 1) + '.</span>' +
          '<span class="cc-alias">' + esc(entry.alias || '') + '</span>' +
          '<span class="cc-name">' + esc(entry.name || '') + '</span>' +
          '<span class="cc-dishes">' + (entry.delivered | 0) + '</span></div>';
      });
      teams.innerHTML = html + '</div>';
    }
    card.classList.add('on');
  }

  // ---- frame ingest ------------------------------------------------------
  function onFrame(text) {
    var s;
    try { s = JSON.parse(text); } catch (error) { return; }
    if (!s || typeof s !== 'object' || s.tick === undefined) return;
    state = s;
    if (statusEl && statusEl.textContent !== '') statusEl.textContent = '';

    chromeState.t = s.tick | 0;
    chromeState.mx = Math.max(1, s.ticks | 0);
    chromeState.st = 0;
    chromeState.mt = 0;
    chromeState.ph = s.phase === 'gameover' ? 'gameover' : 'playing';
    chromeState.en = true;
    chromeState.pl = !!s.playing;
    chromeState.lp = !!s.loop;
    chromeState.sp = s.speed || 1;

    renderTransport(chromeState);
    // AFTER the transport: chrome_common writes #tick-clock as "x / y" and the
    // clock readout is the game's, spelled out in real numbers.
    renderClockReadout(s);
    ccSeatPlates(s);
    ccDishTicker(s);
    ccSayBar(s);
    ccHeatToggle(!!s.heatOn);
    renderFeed(s);
    renderBeats(s);
    if (s.final) renderEndcard(s);
  }

  function onStatus(status) {
    if (statusEl) statusEl.textContent = status === 'connecting' ? 'loading replay' : '';
  }

  // ---- core --------------------------------------------------------------
  var adapter = window.CcStaticReplay || null;
  if (!adapter) {
    console.error('static_replay.js was not loaded');
    return;
  }
  core = adapter.createCore({
    canvas: canvas,
    websocket: false,
    playoutBuffer: false,
    onText: onFrame,
    onStatus: onStatus,
    onFirstFrame: function () { relayout(); },
    onTransform: function (transform) {
      if (transform && transform.nativeW > 1 && transform.nativeH > 1) {
        if (transform.nativeW !== BOARD_W || transform.nativeH !== BOARD_H) {
          BOARD_W = transform.nativeW;
          BOARD_H = transform.nativeH;
          BOARD_ASPECT = BOARD_W / BOARD_H;
          relayout();
        }
      }
    }
  });

  // ---- transport wiring --------------------------------------------------
  function bind(id, handler) {
    var element = $(id);
    if (element) element.addEventListener('click', handler);
  }
  bind('btn-play', function () { ccSend('toggle'); });
  bind('btn-restart', function () { ccSeek(0); });
  bind('btn-back', function () { ccSeek(Math.max(0, (state ? state.tick : 0) - 1)); });
  bind('btn-fwd', function () { ccSeek((state ? state.tick : 0) + 120); });
  bind('btn-end', function () { ccSend('end'); });
  bind('btn-loop', function () { ccSend('loop'); });
  // Skip the lull: jump to the next story beat ahead of the playhead.
  bind('btn-skip', function () {
    if (!state || !state.beats) return;
    var here = state.tick | 0;
    for (var i = 0; i < state.beats.length; i++) {
      if ((state.beats[i].t | 0) > here) { ccSeek(state.beats[i].t); return; }
    }
    ccSend('end');
  });
  bind('heatbtn', function () { ccSend('heat'); });

  var scrub = $('scrub');
  if (scrub) {
    scrub.addEventListener('click', function (event) {
      if (event.target && event.target.classList &&
          event.target.classList.contains('beat-marker')) return;
      var rect = scrub.getBoundingClientRect();
      if (!rect.width) return;
      var fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      ccSeek(fraction * (state ? state.ticks : 1));
    });
  }
  window.addEventListener('keydown', function (event) {
    if (event.key === ' ') { event.preventDefault(); ccSend('toggle'); }
    else if (event.key === 'h') ccSend('heat');
    else if (event.key === ',') ccSeek(0);
    else if (event.key === 'e') ccSend('end');
  });

  // ---- layout ------------------------------------------------------------
  // The starter's relayout(), unchanged in contract: it OWNS --hudscale and
  // --band on :root, and the game block only ever reads them.
  function relayout() {
    var boxW = viewport.clientWidth, boxH = viewport.clientHeight;
    if (!boxW || !boxH) return;
    var scorebug = document.getElementById('scorebug');
    var transport = document.getElementById('transport');
    var ticker = document.getElementById('dishticker');
    var sayband = document.getElementById('saybar');
    var root = document.documentElement;
    var topBand = parseFloat(getComputedStyle(root).getPropertyValue('--topband')) || 0;
    var band = parseFloat(getComputedStyle(root).getPropertyValue('--band')) || 0;
    for (var pass = 0; pass < 4; pass++) {
      var prevTop = topBand, prevBand = band;
      var availH = Math.max(1, boxH - topBand - band);
      var boardW, boardH;
      if (boxW / availH > BOARD_ASPECT) {
        boardH = availH; boardW = Math.round(availH * BOARD_ASPECT);
      } else {
        boardW = boxW; boardH = Math.round(boxW / BOARD_ASPECT);
      }
      stage.style.width = boardW + 'px';
      stage.style.height = (boardH + topBand + band) + 'px';
      var scale = Math.max(0.5, Math.min(1.6, boardW / 760));
      root.style.setProperty('--hudscale', scale.toFixed(3));
      stage.classList.toggle('tiny', boardW <= 620);
      // The reserved head band is the scorebug PLUS the appended dish ticker
      // and say band, so a landing say line never pushes the board around.
      var sbHeight = scorebug ? scorebug.offsetHeight : 0;
      var dtHeight = ticker ? ticker.offsetHeight : 0;
      var sayHeight = sayband ? sayband.offsetHeight : 0;
      root.style.setProperty('--sb', sbHeight + 'px');
      root.style.setProperty('--dt', dtHeight + 'px');
      topBand = sbHeight + dtHeight + sayHeight;
      band = transport ? transport.offsetHeight : 0;
      root.style.setProperty('--topband', topBand + 'px');
      root.style.setProperty('--band', band + 'px');
      if (Math.abs(topBand - prevTop) < 0.5 && Math.abs(band - prevBand) < 0.5) break;
    }
    if (core) core.setViewportFit();
  }
  window.addEventListener('resize', relayout);
  relayout();
  core.start();
})();
