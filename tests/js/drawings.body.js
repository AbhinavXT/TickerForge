// Geometry behind the drawing tools.
//
// This is the fiddly part and the part that degrades silently. A hit test
// that's subtly wrong doesn't throw — it just means a line you can see is a
// line you can't grab, and you conclude the feature is broken without ever
// knowing why. So the maths gets asserted directly rather than eyeballed.
let ok = true;
function check(name, cond, extra=''){ console.log(`  ${cond?'ok  ':'FAIL'} ${name} ${extra}`); if(!cond) ok=false; }
const near = (a,b,tol=0.001) => Math.abs(a-b) <= tol;

console.log('segment distance:');
// A point directly above the middle of a horizontal segment.
check('perpendicular distance', near(distToSegment(50, 10, 0, 0, 100, 0), 10));
check('on the line is zero', near(distToSegment(50, 0, 0, 0, 100, 0), 0));

// The clamp is what makes it a SEGMENT. Without it, a trendline would stay
// grabbable far past either end, along the infinite line it sits on.
check('past the end measures to the ENDPOINT',
      near(distToSegment(200, 0, 0, 0, 100, 0), 100));
check('before the start measures to the START',
      near(distToSegment(-30, 0, 0, 0, 100, 0), 30));
check('degenerate segment is a point',
      near(distToSegment(3, 4, 0, 0, 0, 0), 5));

console.log('\nray distance:');
// A ray is clamped at the start only — it runs on for ever.
check('past the end is still ON the ray',
      near(distToRay(200, 0, 0, 0, 100, 0), 0));
check('before the start measures to the START',
      near(distToRay(-30, 0, 0, 0, 100, 0), 30));
check('perpendicular still works', near(distToRay(50, 10, 0, 0, 100, 0), 10));
check('ray and segment agree INSIDE the span',
      near(distToRay(50, 10, 0, 0, 100, 0), distToSegment(50, 10, 0, 0, 100, 0)));

console.log('\nrectangle distance:');
// Distance to the OUTLINE. A filled hit area would make a large rectangle
// impossible to draw inside of — every click would grab it.
check('a point well inside is far from the edge',
      distToRect(50, 50, 0, 0, 100, 100) > 40);
check('on an edge is zero', near(distToRect(50, 0, 0, 0, 100, 100), 0));
check('just outside a corner', near(distToRect(-3, -4, 0, 0, 100, 100), 5));
check('corners given in any order',
      near(distToRect(50, 0, 100, 100, 0, 0), distToRect(50, 0, 0, 0, 100, 100)));

console.log('\nfibonacci:');
const up = fibLevels(100, 200);
check('seven levels', up.length === 7);
check('0 sits at the first anchor', near(up[0].price, 100));
check('1 sits at the second anchor', near(up[6].price, 200));
check('0.618 is where it should be', near(up[4].price, 161.8));
check('0.5 is included even though it is not a Fibonacci ratio',
      up.some(l => l.level === 0.5));

// Direction encodes which way you think price moved, so the ladder mirrors.
const down = fibLevels(200, 100);
check('drawn the other way mirrors', near(down[0].price, 200) && near(down[6].price, 100));
check('mirrored 0.618', near(down[4].price, 138.2));

console.log('\nhit dispatch:');
const two = [{x:0,y:0},{x:100,y:100}];
check('trendline', near(hitDistance('trendline', two, 50, 50), 0));
check('hline ignores x entirely',
      near(hitDistance('hline', [{x:10,y:40}], 9999, 40), 0));
check('rect outline', near(hitDistance('rect', two, 0, 50), 0));
check('ray beyond the end still hits',
      near(hitDistance('ray', two, 300, 300), 0));
check('unknown kind never hits', hitDistance('spiral', two, 0, 0) === Infinity);

// A fib is grabbable by ANY of its levels, not just the two anchors.
const fy = 0 + (100 - 0) * 0.618;
check('fib hits on an interior level', near(hitDistance('fib', two, 50, fy), 0));
check('fib misses between levels', hitDistance('fib', two, 50, 55) > 0);

console.log('\nhandles:');
const pts = [{x:10,y:10},{x:90,y:90}];
check('exactly on a handle', nearestHandle(pts, 10, 10) === 0);
check('the far handle', nearestHandle(pts, 90, 90) === 1);
check('within tolerance', nearestHandle(pts, 13, 12) === 0);
// Handles are checked BEFORE whole-shape dragging, so a shape can be reshaped
// rather than only moved. Middle-of-the-line must NOT report a handle.
check('the middle is not a handle', nearestHandle(pts, 50, 50) === -1);

process.exit(ok?0:1);
