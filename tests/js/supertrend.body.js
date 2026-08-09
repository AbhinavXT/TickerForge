// The bug this guards: Supertrend used to render as TWO full-width lines
// because the null points were filtered out, leaving dense series that
// lightweight-charts connected straight across the gaps.
let ok = true;
function check(name, cond, extra=''){ console.log(`  ${cond?'ok  ':'FAIL'} ${name} ${extra}`); if(!cond) ok=false; }

const bars = []; let c = 100, t = 0;
const leg = (n, step) => { for (let i=0;i<n;i++){ c += step;
  bars.push({time:t++, high:c+1, low:c-1, close:c}); } };
leg(60, 1.5); leg(60, -1.5); leg(60, 1.5);

const s = supertrend(bars, 10, 3);
const upVals = s.up.filter(x => x.value != null).length;
const dnVals = s.down.filter(x => x.value != null).length;
const gaps   = s.up.filter(x => x.value == null).length;

console.log(`  up=${upVals} down=${dnVals} whitespace=${gaps} flips=${s.flips.length}`);
console.log('\nassertions:');
check('both trends are drawn', upVals > 0 && dnVals > 0);
check('the up line HAS breaks in it', gaps > 0);
check('arrays are the same length', s.up.length === s.down.length);
check('arrays are time-aligned', s.up.every((x,i) => x.time === s.down[i].time));

// The invariant that makes it read as one line: at almost every bar exactly
// one of the two carries a value. The only exceptions are the bridge points
// that join the colours at a flip.
let both = 0, neither = 0;
for (let i = 0; i < s.up.length; i++) {
  const u = s.up[i].value, d = s.down[i].value;
  if (u != null && d != null) both++;
  if (u == null && d == null) neither++;
}
check('never both lines except at flips', both <= s.flips.length, `(both=${both}, flips=${s.flips.length})`);
check('never a bar with neither line', neither === 0, `(${neither})`);
check('flips detected in both directions', new Set(s.flips.map(f=>f.dir)).size === 2);
check('flips carry time/dir/level', s.flips.every(f => f.time!=null && f.dir && f.level!=null));
check('short series returns empty shape',
      JSON.stringify(supertrend(bars.slice(0,5),10,3)) === '{"up":[],"down":[],"flips":[]}');

process.exit(ok?0:1);
