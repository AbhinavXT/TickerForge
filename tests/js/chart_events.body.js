// A series that genuinely traverses both RSI thresholds: rise (RSI high),
// fall (crosses 30 downward), rise again (crosses 70 upward). Long enough
// for a 200-bar SMA to exist so ma_cross can fire.
const bars = []; let c = 100, t = 0;
const leg = (n, step) => { for (let i=0;i<n;i++){ c += step;
  bars.push({time:t++, open:c-step/2, high:c+2, low:c-2, close:c, volume:1000}); } };
leg(140,  2.0);   // strong up  -> RSI well above 70
leg(120, -2.5);   // strong down -> crosses 70 down, then 30 down
leg(140,  2.5);   // strong up  -> crosses 30 up, then 70 up
bars[300].volume = 9000;   // a surge to find

let ok = true;
function check(name, cond, extra=''){ console.log(`  ${cond?'ok  ':'FAIL'} ${name} ${extra}`); if(!cond) ok=false; }

const r = {};
for (const [k,d] of Object.entries(EVENTS)) r[k] = d.fn(bars);
for (const [k,v] of Object.entries(r)) console.log(`  ${k}: ${v.length} event(s)`);

console.log('\nassertions:');
check('supertrend flips detected', r.st_flip.length >= 2, `(${r.st_flip.length})`);
check('RSI fires both directions',
      new Set(r.rsi_ob.map(e=>e.text)).size === 2,
      `[${[...new Set(r.rsi_ob.map(e=>e.text))].join(', ')}]`);
check('golden AND death cross seen',
      new Set(r.ma_cross.map(e=>e.text)).size === 2,
      `[${[...new Set(r.ma_cross.map(e=>e.text))].join(', ')}]`);
check('volume surge found the spike', r.vol_surge.some(e=>e.time===300));
check('bb_break marks transitions not states', r.bb_break.length < bars.length/6,
      `(${r.bb_break.length} of ${bars.length} bars)`);
check('macd crossed both ways',
      new Set(r.macd_cross.map(e=>e.dir)).size === 2);

const all = Object.values(r).flat();
check('all events have time/dir/text/color',
      all.every(e => e.time!=null && e.dir && e.text && e.color));
check('all event times exist in the series',
      all.every(e => bars.some(b => b.time === e.time)));
check('directions are only up/down',
      all.every(e => e.dir === 'up' || e.dir === 'down'));

// no false positives on a flat tape
const flat = Array.from({length:300},(_,i)=>({time:i,open:100,high:100.4,low:99.6,close:100,volume:1000}));
const flatTotal = Object.values(EVENTS).reduce((n,d)=>n+d.fn(flat).length,0);
check('flat series is quiet', flatTotal <= 2, `(${flatTotal} events)`);

// and never throws on a too-short series
let threw = false;
try { for (const d of Object.values(EVENTS)) d.fn(flat.slice(0,5)); } catch(e){ threw = true; }
check('short series does not throw', !threw);

process.exit(ok?0:1);
