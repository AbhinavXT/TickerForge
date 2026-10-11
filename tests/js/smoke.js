// Page smoke test: load a page from app/web into jsdom and report what broke.
//
//   node tests/js/smoke.js index.html desk.html ...
//
// Prints one JSON object per page: uncaught errors, plus a set of probes about
// the shell (rail, tab bar, theme toggle). The Python side asserts on it.
//
// Nothing here talks to the network. `fetch` answers every call with a 503,
// the "no Kite session" path every page already has to survive; the charting
// library is replaced by a stub that accepts any call; local scripts
// (`/ui.js`) are inlined from disk.
"use strict"
const fs = require("fs")
const path = require("path")
const { JSDOM, VirtualConsole } = require("jsdom")

const WEB = path.resolve(__dirname, "..", "..", "app", "web")

// A chainable stand-in for LightweightCharts: every property is a function
// that returns the stub again, and it coerces to 0 where a number is wanted
// (priceScale().width()).
function chartStub() {
	const fn = function () {
		return proxy
	}
	const proxy = new Proxy(fn, {
		get(_t, k) {
			if (k === Symbol.toPrimitive) return () => 0
			if (k === "then") return undefined // never look like a promise
			return proxy
		},
		apply() {
			return proxy
		},
	})
	return proxy
}

function inline(html) {
	// drop external scripts (the CDN chart lib); inline same-origin ones
	return html
		.replace(/<script[^>]*src="https?:\/\/[^"]*"[^>]*><\/script>/g, "")
		.replace(/<script([^>]*)src="\/([^"]+\.js)"([^>]*)><\/script>/g, (m, a, f, b) => {
			const p = path.join(WEB, f)
			if (!fs.existsSync(p)) return m
			return `<script${a}${b}>${fs.readFileSync(p, "utf8")}</script>`
		})
		.replace(/<link[^>]*fonts.googleapis[^>]*>/g, "")
}

// Async failures are kept apart from script errors. With every request
// answering 503, a page's boot() can trip over the error body (`wl.map is not
// a function`); that's data-shape handling, not a broken page, so the Python
// side reports these but only fails on `errors`.
let rejections = []
process.on("unhandledRejection", (r) => {
	rejections.push(String((r && r.stack) || r).split("\n").slice(0, 2).join(" | "))
})

async function run(file, opts) {
	const errors = []
	rejections = []
	const vc = new VirtualConsole()
	vc.on("jsdomError", (e) => {
		const msg = String(e && (e.message || e))
		// jsdom has no canvas and no layout; those aren't page bugs
		if (/Not implemented: (HTMLCanvasElement|window\.scrollTo|navigation)/.test(msg)) return
		errors.push(msg + (e && e.detail ? " :: " + String(e.detail.stack || e.detail).split("\n").slice(0, 3).join(" | ") : ""))
	})
	const route = "/" + file.replace(/\.html$/, "").replace(/^index$/, "")
	const html = inline(fs.readFileSync(path.join(WEB, file), "utf8"))
	const dom = new JSDOM(html, {
		url: "http://127.0.0.1:5000" + route,
		runScripts: "dangerously",
		pretendToBeVisual: true,
		virtualConsole: vc,
		beforeParse(w) {
			w.LightweightCharts = chartStub()
			w.fetch = async () => ({
				ok: false,
				status: 503,
				headers: { get: () => "application/json" },
				json: async () => ({ detail: "offline (smoke test)" }),
				text: async () => '{"detail":"offline (smoke test)"}',
				blob: async () => new w.Blob([]),
			})
			w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
			w.IntersectionObserver = w.ResizeObserver
			w.matchMedia = (q) => ({
				matches: evalMedia(q, opts),
				media: q,
				addEventListener() {},
				removeEventListener() {},
				addListener() {},
				removeListener() {},
			})
			if (opts.width) {
				Object.defineProperty(w, "innerWidth", { value: opts.width, configurable: true })
			}
			if (opts.theme) w.localStorage.setItem("tf_theme", opts.theme)
			w.HTMLCanvasElement.prototype.getContext = () => chartStub()
			w.EventSource = class { constructor() {} close() {} addEventListener() {} }
			w.navigator.serviceWorker = { register: async () => ({}) }
		},
	})
	const w = dom.window
	await new Promise((r) => setTimeout(r, opts.wait || 400))
	const d = w.document
	const q = (s) => d.querySelector(s)
	const probes = {
		title: d.title,
		hasRail: !!q(".tf-rail"),
		hasTopbar: !!q(".tf-top"),
		hasTabbar: !!q(".tf-tabbar"),
		tabbarSlots: [...d.querySelectorAll(".tf-tabbar > .tf-tab")].map((a) => a.dataset.key),
		moreLinks: [...d.querySelectorAll(".tf-more a")].map((a) => a.getAttribute("href")),
		railLinks: [...d.querySelectorAll(".tf-rail a.tf-item")].map((a) => a.getAttribute("href")),
		activeRail: (q(".tf-rail a[aria-current=page]") || {}).getAttribute?.("href") || null,
		theme: d.documentElement.getAttribute("data-theme"),
		hasThemeToggle: !!q("[data-tf-theme-toggle]"),
		iconButtonsWithoutLabel: [...d.querySelectorAll("button")].filter(
			(b) => !b.textContent.trim() && !b.getAttribute("aria-label") && !b.getAttribute("title"),
		).length,
	}
	if (opts.toggle) {
		const t = q("[data-tf-theme-toggle]")
		if (t) {
			let heard = null
			w.addEventListener("tf:theme", (e) => (heard = e.detail && e.detail.theme))
			t.click()
			probes.afterToggle = {
				theme: d.documentElement.getAttribute("data-theme"),
				stored: w.localStorage.getItem("tf_theme"),
				event: heard,
			}
		}
	}
	if (opts.more) {
		const m = q(".tf-tabbar [data-key=more]")
		if (m) {
			m.click()
			probes.moreOpen = !!q(".tf-more.open, .tf-more[open]")
		}
	}
	if (opts.paneh) {
		// tf_paneh must still round-trip in its old shape
		probes.paneh = w.localStorage.getItem("tf_paneh")
	}
	w.close()
	return { file, errors, rejections, probes }
}

function evalMedia(q, opts) {
	let m
	if ((m = /max-width:\s*(\d+)px/.exec(q))) return (opts.width || 1440) <= +m[1]
	if ((m = /min-width:\s*(\d+)px/.exec(q))) return opts.width >= +m[1]
	if (/prefers-color-scheme:\s*light/.test(q)) return opts.prefers === "light"
	if (/prefers-color-scheme:\s*dark/.test(q)) return opts.prefers !== "light"
	return false
}

;(async () => {
	const args = process.argv.slice(2)
	const opts = {}
	const files = []
	for (const a of args) {
		const m = /^--(\w+)(?:=(.*))?$/.exec(a)
		if (m) opts[m[1]] = m[2] === undefined ? true : isNaN(+m[2]) ? m[2] : +m[2]
		else files.push(a)
	}
	const out = []
	for (const f of files) out.push(await run(f, opts))
	process.stdout.write(JSON.stringify(out))
	process.exit(0)
})()
