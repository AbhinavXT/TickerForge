// Chart-type and timeframe transforms.
//
// Both turn the loaded candles into different candles, which is the kind of
// code that goes wrong quietly: a resample that drops the last bar, or a
// Heikin Ashi seeded wrong, still renders a plausible-looking chart.
let ok = true;
function check(n,c,e=''){ console.log(`  ${c?'ok  ':'FAIL'} ${n} ${e}`); if(!c) ok=false; }
const near=(a,b,t=1e-6)=>Math.abs(a-b)<=t;
const DAY = 86400;

// 2026-01-05 is a Monday.
const MON = Math.floor(new Date(2026,0,5).getTime()/1000);
function bar(i, o, h, l, c, v=100){ return {time: MON + i*DAY, open:o, high:h, low:l, close:c, volume:v}; }

console.log('heikin ashi:');
const src = [bar(0,100,110,90,105), bar(1,105,115,95,110), bar(2,110,120,100,102)];
const ha = heikinAshi(src);
// close is the average of the four; open is the midpoint of the PREVIOUS ha bar
check('close is the OHLC average', near(ha[0].close, (100+110+90+105)/4));
check('first open is seeded from its own bar', near(ha[0].open, (100+105)/2));
check('later opens use the previous ha bar',
      near(ha[1].open, (ha[0].open + ha[0].close)/2));
// high/low must expand to contain the synthetic body, or the wicks lie
check('high contains the body', ha[1].high >= Math.max(ha[1].open, ha[1].close));
check('low contains the body', ha[1].low <= Math.min(ha[1].open, ha[1].close));
check('bar count is preserved', ha.length === src.length);
check('times are untouched', ha.every((b,i) => b.time === src[i].time));
check('empty in, empty out', heikinAshi([]).length === 0);

console.log('\nweekly resample:');
// Mon..Fri then the next Monday — five days in week one, one in week two.
const days = [bar(0,100,105,99,104), bar(1,104,108,103,107), bar(2,107,112,101,102),
              bar(3,102,106,100,105), bar(4,105,109,104,108), bar(7,108,115,107,113)];
const wk = resample(days, weekKey);
check('two weeks', wk.length === 2, `(${wk.length})`);
check('open is the FIRST day of the week', near(wk[0].open, 100));
check('close is the LAST day of the week', near(wk[0].close, 108));
check('high is the highest of the week', near(wk[0].high, 112));
check('low is the lowest of the week', near(wk[0].low, 99));
check('volume sums', wk[0].volume === 500);
check('a partial week still produces a bar', wk[1].close === 113);
// The bar must be stamped at the START of its period, or it plots a week late.
check('stamped at the week start', wk[0].time === weekKey(days[0].time));
check('the two weeks differ', wk[0].time !== wk[1].time);

console.log('\nmonthly resample:');
const jan = bar(0,100,110,90,105);
const feb = {...bar(0,105,120,104,118), time: Math.floor(new Date(2026,1,3).getTime()/1000)};
const mo = resample([jan, feb], monthKey);
check('two months', mo.length === 2);
check('stamped at the month start',
      mo[0].time === Math.floor(new Date(2026,0,1).getTime()/1000));

console.log('\ndispatch:');
check('day passes through untouched', applyTimeframe(days,'day') === days);
check('week resamples', applyTimeframe(days,'week').length === 2);
check('month resamples', applyTimeframe(days,'month').length === 1);
check('unknown falls back to raw', applyTimeframe(days,'nonsense') === days);
check('empty is safe', applyTimeframe([],'week').length === 0);

console.log('\nno data is invented:');
// Every resampled high must exist in the source. A resample that computed
// rather than selected would be showing prices that never traded.
const allHighs = new Set(days.map(d => d.high));
check('highs come from real bars', wk.every(w => allHighs.has(w.high)));
const allLows = new Set(days.map(d => d.low));
check('lows come from real bars', wk.every(w => allLows.has(w.low)));

process.exit(ok?0:1);
