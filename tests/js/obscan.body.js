// What this guards: the chart's order-block port (obScan) marking the same
// displacements, zones, liquidity and traps as app/board.py. The fixture and
// Python's answers come in through OBSCAN_FIXTURE, written by the test.
const fs = require("fs")
let ok = true
function check(name, cond, extra = "") { console.log(`  ${cond ? "ok  " : "FAIL"} ${name} ${extra}`); if (!cond) ok = false }

const fx = JSON.parse(fs.readFileSync(process.env.OBSCAN_FIXTURE, "utf8"))
for (const [n, cs] of fx.cases.entries()) {
	const js = obScan(cs.candles, { atr: cs.atr, pdh: cs.pdh, pdl: cs.pdl })
	const py = cs.py
	const key = (o, ks) => ks.map((k) => (typeof o[k] === "number" ? +o[k].toFixed(6) : o[k])).join("|")
	const same = (a, b, ks) => JSON.stringify(a.map((o) => key(o, ks))) === JSON.stringify(b.map((o) => key(o, ks)))
	check(`case ${n}: displacements`, same(js.disps, py.disps, ["i", "dir"]), `(js ${js.disps.length}, py ${py.disps.length})`)
	check(`case ${n}: order blocks + live`, same(js.obs, py.obs, ["i", "dir", "top", "bottom", "live"]), `(js ${js.obs.length}, py ${py.obs.length})`)
	check(`case ${n}: liquidity levels`, same(js.levels, py.levels, ["price", "kind", "touches", "formed"]), `(js ${js.levels.length}, py ${py.levels.length})`)
	check(`case ${n}: traps`, same(js.traps, py.traps, ["i", "dir", "kind", "price"]), `(js ${js.traps.length}, py ${py.traps.length})`)
}
check("fixture exercises every kind of mark", fx.cases.some((c) => c.py.traps.length) && fx.cases.some((c) => c.py.obs.some((o) => o.live)) && fx.cases.some((c) => c.py.obs.some((o) => !o.live)))

// The overlay draws only live zones, two lines each, and nothing for daily bars' PDH.
const c0 = fx.cases[0].candles
const ov = I_obliq(c0, { k: 1.5, count: 2, levels: 2 }).overlays
check("overlay rows have data", ov.length > 0 && ov.every((o) => o.data.length > 0))
check("zones come as top/bot pairs", ov.filter((o) => / top$/.test(o.name)).length === ov.filter((o) => / bot$/.test(o.name)).length)
const daily = c0.map((x, i) => ({ ...x, time: i * 86400 }))
check("no PDH/PDL on daily bars", JSON.stringify(obPrevDay(daily)) === "{}")
const intra = [0, 1, 2].flatMap((d) => [0, 1, 2].map((k) => ({ time: d * 86400 + k * 300, open: 10, close: 10, high: 10 + d + k, low: 9 - d - k })))
const pd = obPrevDay(intra)
check("PDH/PDL come from the previous session", pd.pdh === 13 && pd.pdl === 6, JSON.stringify(pd))
check("trap event is registered", typeof EVENTS.liq_trap.fn === "function" && Array.isArray(EVENTS.liq_trap.fn(c0)))
process.exit(ok ? 0 : 1)
