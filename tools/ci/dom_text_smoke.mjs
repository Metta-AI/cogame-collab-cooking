#!/usr/bin/env node
// The worst-case renderer fixture for the model-authored text this viewer
// draws in the DOM (checklist item 15's DOM branch).
//
// `viewer_smoke.mjs` instruments fillText/strokeText, so it can only see
// canvas draws -- and every replay CI can produce carries zero LLM text
// (docker_smoke.sh runs without an ANTHROPIC_API_KEY, so every seat falls back
// to a scripted baseline and no seat ever says anything). This game's say band
// and its feed are DOM, and both were clipping a full-cap remark with the whole
// board green (r2 review, R2-O1/R2-O2).
//
// So: load the REAL page (client/replay_broadcast.html, spliced exactly as the
// Dockerfile splices it, with the real chrome_common.js and the real font),
// stub only the wasm core, and hand the page's own onText a frame built to
// hurt -- a full-cap say on EVERY seat and a feed of full-cap lines, in Latin,
// in CJK, and as one unbroken token -- at several viewport widths. Then assert,
// for every DOM node that carries model-authored text:
//
//   * scrollHeight <= clientHeight  (nothing clipped vertically)
//   * scrollWidth  <= clientWidth   (nothing clipped horizontally)
//   * the node's box is inside #stage, which is overflow:hidden
//   * the node still carries the WHOLE string -- a quietly shortened remark
//     fails the fixture instead of passing it
//
// and that the say band is the same height whether or not anyone is speaking,
// which is the other half of what the design note promises.
//
// Usage: node tools/ci/dom_text_smoke.mjs [--out dom-text-smoke.json]

import { chromium } from 'playwright';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const TOLERANCE = 1; // px: sub-pixel layout rounding, nothing more.

const VIEWPORTS = [
  { width: 1280, height: 800 },
  { width: 1024, height: 640 },
  { width: 900, height: 558 },
  { width: 640, height: 397 },
  { width: 414, height: 736 },
  { width: 360, height: 640 },
  { width: 360, height: 223 }, // the letterboxed featured-match iframe
  { width: 1280, height: 360 },
  { width: 1280, height: 321 },
  { width: 800, height: 340 },
  { width: 500, height: 400 },
  { width: 360, height: 330 },
  { width: 1920, height: 1080 },
];

function arg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index === -1 ? fallback : process.argv[index + 1];
}

// The cap is read from the two places that enforce it, and they must agree:
// the fixture is only evidence if it hands the page exactly what the server
// would let a model say.
async function sayCap() {
  const plans = await fs.readFile(path.join(ROOT, 'src', 'collab_cooking', 'coworld', 'plans.py'), 'utf8');
  const nim = await fs.readFile(path.join(ROOT, 'replay-viewer', 'collab_cooking_replay.nim'), 'utf8');
  const python = /^SAY_RUNES = (\d+)$/m.exec(plans);
  const viewer = /SayRunes = (\d+)/.exec(nim);
  if (!python || !viewer) throw new Error('could not read the say cap from plans.py / the Nim module');
  if (python[1] !== viewer[1]) {
    throw new Error(`say cap disagrees: plans.py ${python[1]} vs the Nim module ${viewer[1]}`);
  }
  return Number(python[1]);
}

function runes(text) {
  return Array.from(text).length;
}

// Four full-cap strings, each a different way for a line box to lose: a Latin
// sentence, an unbroken token with no break opportunity, CJK (multi-byte runes,
// each about one em wide), and a ragged word pattern that wastes the end of
// every line.
function capStrings(cap) {
  const latin =
    'plating the soup now so the pass stays clear for the salad tickets, ' +
    'then I will chop meat while the fryer runs and hand off at the pass ' +
    'before the board fills again';
  const unbroken = 'W'.repeat(cap);
  const cjk = '\u732b\u53a8\u623f'.repeat(Math.ceil(cap / 3));
  const ragged = ('WWWWWWWWWW ').repeat(Math.ceil(cap / 11));
  return [latin, unbroken, cjk, ragged].map((text) => Array.from(text).slice(0, cap).join(''));
}

function worstCaseFrame(cap, { speaking }) {
  const says = capStrings(cap);
  const aliases = ['Cog-A', 'Cog-B', 'Cog-C', 'Cog-D'];
  const seats = aliases.map((alias, index) => ({
    slot: index,
    alias,
    name: 'policy-with-a-long-name-' + index,
    kind: index === 0 ? 'prompt' : 'scripted',
    color: index,
    delivered: 3 + index,
    carrying: 'chopped_meat',
    job: 'chopping_station',
    pending: index === 0,
    say: speaking ? says[index] : '',
    dc: false,
  }));
  // The feed carries the other surface a remark lands on: "<alias>: <say>".
  const feed = speaking
    ? says.concat(says.slice(0, 2)).map((say, index) => ({
        t: 100 + index,
        kind: 'say',
        text: aliases[index % 4] + ': ' + say,
      }))
    : [];
  return {
    tick: 242,
    ticks: 480,
    layout: 'open-kitchen',
    phase: 'play',
    dishes: 11,
    live: 3,
    expiring: 1,
    expired: 2,
    burned: { pot: 1, fryer: 0 },
    playing: true,
    speed: 1,
    loop: false,
    heatOn: false,
    seats,
    ticker: [{ t: 200, recipe: 'soup', alias: 'Cog-C' }],
    heat: [[3, 4, 12]],
    feed,
    beats: [{ t: 36, k: 'jam', label: 'Jam at the doorway' }],
    final: null,
  };
}

const FIXTURE_CORE = `// Stands in for static_replay.js: the same hook, no wasm.
window.CcStaticReplay = {
  createCore: function (options) {
    window.__ccFixture = {
      onText: options.onText,
      onFirstFrame: options.onFirstFrame,
      onStatus: options.onStatus
    };
    return {
      start: function () {},
      sendCommand: function () {},
      setViewportFit: function () {}
    };
  }
};
`;

async function buildHarness(cap) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'dom-text-smoke-'));
  const page = await fs.readFile(path.join(ROOT, 'client', 'replay_broadcast.html'), 'utf8');
  if (!page.includes('<!-- CHROME_COMMON -->') || !page.includes('<!-- BROADCAST_CORE -->')) {
    throw new Error('client/replay_broadcast.html is missing a splice marker');
  }
  // Exactly the Dockerfile's splice, except that the core is the stub above.
  const html = page
    .replace('<!-- CHROME_COMMON -->', '<script src="./chrome_common.js"></script>')
    .replace('<!-- BROADCAST_CORE -->', '<script src="./fixture_core.js"></script>');
  await fs.writeFile(path.join(dir, 'index.html'), html, 'utf8');
  await fs.copyFile(path.join(ROOT, 'client', 'chrome_common.js'), path.join(dir, 'chrome_common.js'));
  await fs.copyFile(path.join(ROOT, 'data', 'font.ttf'), path.join(dir, 'font.ttf'));
  await fs.writeFile(path.join(dir, 'fixture_core.js'), FIXTURE_CORE, 'utf8');
  return dir;
}

// Runs in the page. Returns one measurement per model-text node.
function probe(payload) {
  const stage = document.getElementById('stage');
  const stageBox = stage.getBoundingClientRect();
  const nodes = [];
  const collect = (selector, kind) => {
    document.querySelectorAll(selector).forEach((element, index) => {
      const style = getComputedStyle(element);
      const visible = element.getClientRects().length > 0 && style.display !== 'none' &&
        style.visibility !== 'hidden';
      const box = element.getBoundingClientRect();
      nodes.push({
        kind,
        index,
        visible,
        text: element.textContent || '',
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        fontSize: style.fontSize,
        lineHeight: style.lineHeight,
        overflow: style.overflow,
        whiteSpace: style.whiteSpace,
        maxHeight: style.maxHeight,
        top: box.top,
        bottom: box.bottom,
        left: box.left,
        right: box.right,
      });
    });
  };
  collect('#saybar .say-chip:not(.say-gauge)', 'say-chip');
  collect('#saybar .say-gauge', 'gauge');
  collect('#feed .feed-row', 'feed-row');
  const bar = document.getElementById('saybar');
  const root = document.documentElement;
  const gauges = [];
  document.querySelectorAll('#saybar .say-gauge').forEach((element) => {
    const box = element.getBoundingClientRect();
    gauges.push({ width: Number(box.width.toFixed(2)), height: Number(box.height.toFixed(2)) });
  });
  return {
    nodes,
    gauges,
    sayFit: bar ? getComputedStyle(bar).getPropertyValue('--sayfit').trim() : '',
    sayBandMinHeight: bar ? bar.style.minHeight : '',
    stage: { top: stageBox.top, bottom: stageBox.bottom, left: stageBox.left, right: stageBox.right },
    sayBandHeight: bar ? bar.getBoundingClientRect().height : 0,
    sayBandShown: bar ? getComputedStyle(bar).display !== 'none' : false,
    topBand: getComputedStyle(root).getPropertyValue('--topband').trim(),
    hudScale: getComputedStyle(root).getPropertyValue('--hudscale').trim(),
    stageWidth: stage.clientWidth,
  };
}

async function measure(page, frame) {
  await page.evaluate((text) => {
    window.__ccFixture.onText(text);
    if (window.__ccFixture.onFirstFrame) window.__ccFixture.onFirstFrame();
  }, JSON.stringify(frame));
  // Let the layout settle exactly as it does in a browser: relayout() runs
  // again on every resize, and the page is measured only once it is stable.
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  for (let i = 0; i < 3; i++) {
    await page.evaluate(() => window.dispatchEvent(new Event('resize')));
  }
  // The feed rows slide in from the right (the starter's 250ms `feedin`):
  // measure them where they land, not mid-flight.
  await page.waitForTimeout(400);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  return page.evaluate(probe);
}

async function main() {
  const outPath = arg('--out', 'dom-text-smoke.json');
  const shotPath = arg('--screenshot', 'dom-text-smoke.png');
  const cap = await sayCap();
  const strings = capStrings(cap);
  for (const text of strings) {
    if (runes(text) !== cap) {
      throw new Error(`fixture string is ${runes(text)} runes, not the full ${cap}-rune cap`);
    }
  }
  const dir = await buildHarness(cap);
  const url = 'file://' + path.join(dir, 'index.html');
  const browser = await chromium.launch();
  const failures = [];
  let viewportsWithWholeText = 0;
  const report = { cap, tolerance: TOLERANCE, viewports: [] };
  try {
    for (const viewport of VIEWPORTS) {
      const label = `${viewport.width}x${viewport.height}`;
      const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
      const errors = [];
      page.on('pageerror', (error) => errors.push(String(error)));
      await page.goto(url, { waitUntil: 'load' });
      await page.waitForFunction(() => !!window.__ccFixture, null, { timeout: 30000 });
      await page.evaluate(() => document.fonts.ready);

      const quiet = await measure(page, worstCaseFrame(cap, { speaking: false }));
      const loud = await measure(page, worstCaseFrame(cap, { speaking: true }));

      const measured = [];
      for (const node of loud.nodes) {
        if (!node.visible) continue;
        measured.push(node);
        const where = `${label} ${node.kind}[${node.index}]`;
        if (node.scrollHeight > node.clientHeight + TOLERANCE) {
          failures.push(
            `${where}: clipped vertically -- scrollHeight ${node.scrollHeight} > clientHeight ` +
            `${node.clientHeight} (${Math.round((1 - node.clientHeight / node.scrollHeight) * 100)}% hidden)`
          );
        }
        if (node.scrollWidth > node.clientWidth + TOLERANCE) {
          failures.push(
            `${where}: clipped horizontally -- scrollWidth ${node.scrollWidth} > clientWidth ${node.clientWidth}`
          );
        }
        const out = [];
        if (node.right > loud.stage.right + TOLERANCE) out.push(`${(node.right - loud.stage.right).toFixed(1)}px past the right edge`);
        if (node.left < loud.stage.left - TOLERANCE) out.push(`${(loud.stage.left - node.left).toFixed(1)}px past the left edge`);
        if (node.bottom > loud.stage.bottom + TOLERANCE) out.push(`${(node.bottom - loud.stage.bottom).toFixed(1)}px below the stage`);
        if (node.top < loud.stage.top - TOLERANCE) out.push(`${(loud.stage.top - node.top).toFixed(1)}px above the stage`);
        if (out.length) failures.push(`${where}: outside #stage (overflow:hidden) -- ${out.join(', ')}`);
      }

      // Non-vacuity, and the strings are still whole: a viewer that quietly
      // shortened a remark would otherwise pass this fixture while showing
      // three quarters of one.
      const chips = measured.filter((node) => node.kind === 'say-chip');
      const rendered = measured.map((node) => node.text).join('\u0000');
      const whole = strings.every((text) => rendered.includes(text));
      if (loud.sayBandShown) {
        if (chips.length !== 4) {
          failures.push(`${label}: expected 4 visible say chips, measured ${chips.length}`);
        }
        if (!whole) {
          failures.push(`${label}: a ${cap}-rune fixture string is not rendered in full anywhere`);
        }
        viewportsWithWholeText += whole && chips.length === 4 ? 1 : 0;
      } else if (measured.length) {
        // The band is dropped on viewports too short to hold it; a viewport
        // that drops it must not show a partial remark anywhere either.
        failures.push(
          `${label}: the say band is hidden but ${measured.length} model-text nodes are still rendered`
        );
      }

      // The band is reserved whether or not anyone is speaking.
      const jump = Math.abs(loud.sayBandHeight - quiet.sayBandHeight);
      if (jump > TOLERANCE) {
        failures.push(
          `${label}: the say band jumps ${jump.toFixed(1)}px when a remark lands ` +
          `(${quiet.sayBandHeight.toFixed(1)} -> ${loud.sayBandHeight.toFixed(1)})`
        );
      }
      if (quiet.topBand !== loud.topBand) {
        failures.push(`${label}: --topband moves when a remark lands (${quiet.topBand} -> ${loud.topBand})`);
      }
      if (errors.length) failures.push(`${label}: page errors: ${errors.join(' | ')}`);

      if (viewport.width === 1280 && viewport.height === 800) {
        await page.screenshot({ path: shotPath, fullPage: false });
      }
      report.viewports.push({
        viewport: label,
        stageWidth: loud.stageWidth,
        sayBandShown: loud.sayBandShown,
        hudScale: loud.hudScale,
        topBand: loud.topBand,
        sayBandQuiet: Number(quiet.sayBandHeight.toFixed(1)),
        sayBandSpeaking: Number(loud.sayBandHeight.toFixed(1)),
        sayBandReserved: loud.sayBandMinHeight,
        sayFit: loud.sayFit,
        gauges: loud.gauges,
        nodesMeasured: measured.length,
        nodes: measured.map((node) => ({
          kind: node.kind,
          index: node.index,
          clientHeight: node.clientHeight,
          scrollHeight: node.scrollHeight,
          clientWidth: node.clientWidth,
          scrollWidth: node.scrollWidth,
          fontSize: node.fontSize,
          lineHeight: node.lineHeight,
          pastStageRight: Number((node.right - loud.stage.right).toFixed(1)),
        })),
      });
      console.log(
        `${label.padEnd(9)} stage ${String(loud.stageWidth).padStart(4)}px  hudscale ${loud.hudScale}  ` +
        `topband ${loud.topBand}  say band ${quiet.sayBandHeight.toFixed(1)}px quiet / ` +
        `${loud.sayBandHeight.toFixed(1)}px speaking  nodes ${measured.length}`
      );
      await page.close();
    }
  } finally {
    await browser.close();
    await fs.rm(dir, { recursive: true, force: true });
  }

  // The run as a whole has to have rendered the full-cap strings somewhere,
  // or the fixture proved nothing at all.
  if (viewportsWithWholeText < 4) {
    failures.push(
      `only ${viewportsWithWholeText} viewports rendered all four ${cap}-rune strings in full; ` +
      'the fixture covered nothing'
    );
  }
  report.viewportsWithWholeText = viewportsWithWholeText;
  report.failures = failures;
  report.ok = failures.length === 0;
  await fs.writeFile(outPath, JSON.stringify(report, null, 2) + '\n', 'utf8');
  if (failures.length) {
    console.error(`\ndom text smoke FAILED (${failures.length}):`);
    for (const failure of failures) console.error('  ' + failure);
    process.exit(1);
  }
  console.log(`\ndom text smoke OK: every ${cap}-rune remark fits its band at ${VIEWPORTS.length} viewports`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
