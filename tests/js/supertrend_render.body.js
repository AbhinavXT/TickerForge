// What this guards: Supertrend rendering as TWO full-width lines instead of
// one line that changes colour.
//
// supertrend() itself has always produced two properly gapped arrays — that
// is asserted next door in supertrend.body.js and it still holds. The failure
// was downstream: two line series, each relying on whitespace to stay silent
// across the other trend's bars. That only ever works while every step
// between here and setData preserves points that have a time but no value,
// and it renders as two lines with diagonal bridges the moment one doesn't.
//
// I_super now folds both into ONE series carrying a per-point colour, so
// there is no gap left to bridge and no way for a second line to appear.
let ok = true;
function check(name, cond, extra=''){ console.log(`  ${cond?'ok  ':'FAIL'} ${name} ${extra}`); if(!cond) ok=false; }

const bars = []; let c = 100, t = 0;
const leg = (n, step) => { for (let i=0;i<n;i++){ c += step;
  bars.push({time:t++, high:c+1, low:c-1, close:c}); } };
leg(60, 1.5); leg(60, -1.5); leg(60, 1.5);

const UP = "#26a69a", DN = "#ef5350";
const res = I_super(bars, {length:10, mult:3, up:UP, dn:DN});
const rows = res.overlays;
const d = rows[0].data;

console.log(`  series=${rows.length} points=${d.length}`);
console.log('\nassertions:');

check('exactly one series is emitted', rows.length === 1, `(${rows.length})`);

// The whole point: no valueless points means nothing to bridge.
check('every point carries a value',
      d.every(x => typeof x.value === 'number'),
      `(${d.filter(x => typeof x.value !== 'number').length} without)`);
check('no whitespace remains', d.filter(x => x.value === undefined).length === 0);

// Per-point colour is what replaces the second series.
check('every point carries a colour', d.every(x => x.color === UP || x.color === DN));
check('both colours are used',
      new Set(d.map(x => x.color)).size === 2);

// Times must be strictly increasing or lightweight-charts drops the series.
check('times strictly increase', d.every((x,i) => i === 0 || x.time > d[i-1].time));

// The colour must switch exactly as often as the trend flips — no more (which
// would mean flicker) and no fewer (which would mean a missed flip).
const s = supertrend(bars, 10, 3);
let switches = 0;
for (let i = 1; i < d.length; i++) if (d[i].color !== d[i-1].color) switches++;
check('colour switches once per flip', switches === s.flips.length,
      `(switches=${switches}, flips=${s.flips.length})`);

// At a flip the value jumps from one band to the other — that near-vertical
// segment is the step you see on Kite and TradingView. If the jump vanished,
// the two bands would have been averaged or smoothed somewhere.
let jumped = false;
for (let i = 1; i < d.length; i++) {
  if (d[i].color !== d[i-1].color && Math.abs(d[i].value - d[i-1].value) > 1) jumped = true;
}
check('the flip draws a vertical jump', jumped);

// Colour params must actually be honoured, or the Style tab silently does
// nothing.
const custom = I_super(bars, {length:10, mult:3, up:"#111111", dn:"#222222"});
check('up/dn params are honoured',
      new Set(custom.overlays[0].data.map(x => x.color)).size === 2 &&
      custom.overlays[0].data.every(x => x.color === "#111111" || x.color === "#222222"));

// Defaults must survive a params object that predates the colour fields —
// saved layouts carry the old shape.
const legacy = I_super(bars, {length:10, mult:3});
check('missing colour params fall back to defaults',
      legacy.overlays[0].data.every(x => x.color === UP || x.color === DN));

// A series too short to compute must not throw or emit junk.
check('short series yields no points', I_super(bars.slice(0,5), {length:10, mult:3})
      .overlays[0].data.length === 0);

process.exit(ok?0:1);
