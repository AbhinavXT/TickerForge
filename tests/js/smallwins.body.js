// The two pure helpers behind the 4.3 chart wins.
let ok = true;
function check(n,c,e=''){ console.log(`  ${c?'ok  ':'FAIL'} ${n} ${e}`); if(!c) ok=false; }
const near=(a,b,t=0.01)=>Math.abs(a-b)<=t;

console.log('sparkline path:');
// Scaled to its own min/max, so a flat-ish stock still shows its shape rather
// than a dead line — the sparkline exists to show shape, not absolute level.
const p = sparkPath([10, 20, 15], 100, 20);
check('starts with a moveto', p.startsWith('M'));
check('one point per close', (p.match(/[ML]/g) || []).length === 3);
check('spans the full width', p.includes('100.0'));
// y is inverted: the HIGHEST close must sit at the TOP (y=0).
check('high close is at the top', p.includes('M0.0 20.0') && p.includes('L50.0 0.0'));

check('too few points draws nothing', sparkPath([5], 100, 20) === '');
check('empty draws nothing', sparkPath([], 100, 20) === '');
check('undefined draws nothing', sparkPath(undefined, 100, 20) === '');
// A flat series would divide by zero on the span without the guard.
const flat = sparkPath([7, 7, 7], 100, 20);
check('flat series does not produce NaN', !flat.includes('NaN'), flat);

console.log('\nindexing to 100:');
// Raw prices can't be compared: a 3,000-rupee stock and a 40-rupee stock on
// one axis tells you only which is expensive per share.
const idx = indexTo100([50, 60, 40]);
check('first bar is exactly 100', near(idx[0], 100));
check('+20% reads as 120', near(idx[1], 120));
check('-20% reads as 80', near(idx[2], 80));

// Two very differently priced series with identical shape must produce
// identical index lines — that is the entire point.
const a = indexTo100([100, 110, 121]);
const b = indexTo100([1000, 1100, 1210]);
check('price level does not matter', a.every((v,i) => near(v, b[i])));

check('empty is empty', indexTo100([]).length === 0);
check('a zero base is refused rather than dividing', indexTo100([0, 5]).length === 0);

process.exit(ok?0:1);
