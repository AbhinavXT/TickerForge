/* TickerForge shell + UI helpers. Vanilla, no dependencies.
 *
 * Load it in <head> (it sets the theme before first paint), then call
 * `TF.shell()` from an inline script straight after the page's
 * `<div id="nav" data-page="news">` (which holds static fallback links).
 *
 * The shell is the left rail + top bar on desktop and a 5-slot bottom tab bar
 * (Board, Chart, Desk, Results, More) under 760px. It moves the page's own
 * `#status` span into the top bar, so `$("#status").textContent = …` in page
 * code keeps working.
 *
 * Theme: `tf_theme` in localStorage ("dark" | "light"), else the OS
 * preference. Changing it dispatches `tf:theme` on window with
 * `{detail: {theme}}`; charts listen and re-apply `TF.chartTheme()`.
 */
;(function () {
	"use strict"
	const doc = document
	const root = doc.documentElement
	const THEME_KEY = "tf_theme"

	// ------------------------------------------------------------ navigation
	// One definition for the rail, the tab bar and the More sheet. The static
	// link list in each page mirrors these hrefs (test_shell_smoke checks).
	const NAV = [
		{ group: "Markets", items: [
			["board", "Board", "/board"],
			["chart", "Chart", "/"],
			["desk", "Desk", "/desk"],
			["scanner", "Scanner", "/scanner"],
			["screen", "Screen", "/screen"],
			["query", "Query", "/query"],
		] },
		{ group: "Research", items: [
			["fundamentals", "Fundamentals", "/fundamentals"],
			["results", "Results", "/results"],
			["valuation", "Valuation", "/valuation"],
			["metrics", "Metrics", "/metrics"],
			["news", "News", "/news"],
			["actions", "Corporate Actions", "/actions"],
		] },
		{ group: "Trading", items: [
			["portfolio", "Portfolio", "/portfolio"],
			["paper", "Paper", "/paper"],
			["journal", "Journal", "/journal"],
			["backtest", "Backtest", "/backtest"],
		] },
	]
	const FOOT = [["settings", "Settings", "/settings"]]
	const TABS = ["board", "chart", "desk", "results"]
	const ALL = NAV.flatMap((g) => g.items).concat(FOOT)

	// ----------------------------------------------------------------- icons
	// 24×24, stroke-only, 1.6px (set in app.css on .tf-ic).
	const ICONS = {
		logo: "M4 17l5-6 4 3 7-8",
		board: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 13h7v8H3z",
		chart: "M7 3v3M7 17v4M5 6h4v11H5zM17 3v4M17 15v6M15 7h4v8h-4z",
		desk: "M3 4h18v12H3zM8 20h8M12 16v4",
		scanner: "M12 12l5.5-5.5M21 12a9 9 0 1 1-9-9M16.5 12A4.5 4.5 0 1 1 12 7.5",
		screen: "M3 5h18l-7 8v6l-4 2v-8z",
		query: "M4 5h16v14H4zM8 10l2 2-2 2M12 14h4",
		fundamentals: "M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6",
		results: "M4 20h16M7 16v-5M12 16V7M17 16v-8",
		valuation: "M12 4v16M8 20h8M4 7h16M6 7l-3 7h6zM18 7l-3 7h6z",
		metrics: "M18 5H6l6 7-6 7h12",
		news: "M4 5h13v14H6a2 2 0 0 1-2-2zM17 9h3v8a2 2 0 0 1-2 2M8 9h5M8 13h5M8 16h3",
		actions: "M4 6h16v14H4zM4 10h16M8 3v5M16 3v5",
		portfolio: "M3 8h18v12H3zM8 8V5h8v3M3 13h18",
		paper: "M9 3h6v3H9zM7 4.5H5V21h14V4.5h-2M9 11h6M9 15h4",
		journal: "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h11",
		backtest: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2",
		settings: "M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4",
		more: "M5 12h.01M12 12h.01M19 12h.01",
		sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
		moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
		x: "M6 6l12 12M18 6L6 18",
		search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
		refresh: "M20 11a8 8 0 0 0-14.9-4L3 9M3 4v5h5M4 13a8 8 0 0 0 14.9 4l2.1-2M21 20v-5h-5",
		bell: "M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 21h4",
		plus: "M12 5v14M5 12h14",
		alert: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 8v5M12 16h.01",
		empty: "M3 13l3-8h12l3 8v6H3zM3 13h5l1 3h6l1-3h5",
		down: "M6 9l6 6 6-6",
		filter: "M4 6h16M7 12h10M10 18h4",
		external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
		download: "M12 4v11M7 10l5 5 5-5M5 20h14",
		trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
		grip: "M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01",
	}
	function icon(name, cls) {
		const d = ICONS[name] || ICONS.empty
		return `<svg class="tf-ic${cls ? " " + cls : ""}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="${d}"/></svg>`
	}

	// ----------------------------------------------------------------- theme
	function storedTheme() {
		try {
			const t = localStorage.getItem(THEME_KEY)
			return t === "light" || t === "dark" ? t : null
		} catch (e) {
			return null
		}
	}
	function systemTheme() {
		try {
			return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"
		} catch (e) {
			return "dark"
		}
	}
	function theme() {
		return root.getAttribute("data-theme") || storedTheme() || systemTheme()
	}
	function applyTheme(t, persist) {
		root.setAttribute("data-theme", t)
		if (persist) {
			try {
				localStorage.setItem(THEME_KEY, t)
			} catch (e) {}
		}
		const meta = doc.querySelector('meta[name="theme-color"]')
		if (meta) meta.setAttribute("content", t === "light" ? "#f4f6f8" : "#0b0e13")
		for (const b of doc.querySelectorAll("[data-tf-theme-toggle]")) paintToggle(b)
		window.dispatchEvent(new CustomEvent("tf:theme", { detail: { theme: t } }))
	}
	function toggleTheme() {
		applyTheme(theme() === "dark" ? "light" : "dark", true)
	}
	function paintToggle(b) {
		const dark = theme() === "dark"
		b.innerHTML = icon(dark ? "sun" : "moon")
		b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme")
		b.title = b.getAttribute("aria-label")
	}
	// Before first paint: no flash of the wrong theme.
	root.setAttribute("data-theme", storedTheme() || systemTheme())
	try {
		window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
			if (!storedTheme()) applyTheme(systemTheme(), false)
		})
	} catch (e) {}

	function cssVar(name, el) {
		return getComputedStyle(el || root).getPropertyValue(name).trim()
	}

	// lightweight-charts options from the tokens; re-apply on `tf:theme`.
	function chartTheme() {
		const v = (n) => cssVar(n)
		return {
			layout: {
				background: { type: "solid", color: v("--surface-1") },
				textColor: v("--text-2"),
				fontFamily: v("--mono") || "monospace",
				fontSize: 11,
			},
			grid: {
				vertLines: { color: v("--border") },
				horzLines: { color: v("--border") },
			},
			rightPriceScale: { borderColor: v("--border") },
			leftPriceScale: { borderColor: v("--border") },
			timeScale: { borderColor: v("--border") },
			crosshair: {
				vertLine: { color: v("--text-3"), labelBackgroundColor: v("--surface-3") },
				horzLine: { color: v("--text-3"), labelBackgroundColor: v("--surface-3") },
			},
		}
	}
	function colors() {
		const v = (n) => cssVar(n)
		return {
			bull: v("--bull"), bear: v("--bear"), neutral: v("--neutral"), violet: v("--violet"),
			text: v("--text"), text2: v("--text-2"), text3: v("--text-3"), border: v("--border"),
			surface1: v("--surface-1"), surface2: v("--surface-2"), surface3: v("--surface-3"),
			warn: v("--warn"),
			series: [1, 2, 3, 4, 5].map((i) => v("--series-" + i)),
		}
	}
	// "#7bd3ea" + alpha → rgba(); chart fills want translucency.
	function alpha(hex, a) {
		const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim())
		if (!m) return hex
		const n = parseInt(m[1], 16)
		return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
	}

	// -------------------------------------------------------------- format
	const nf = {}
	function num(v, dp = 2) {
		if (v === null || v === undefined || v === "" || !isFinite(+v)) return "—"
		const k = dp
		nf[k] = nf[k] || new Intl.NumberFormat("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp })
		return nf[k].format(+v)
	}
	function signed(v, dp = 2, suffix = "") {
		if (v === null || v === undefined || !isFinite(+v)) return "—"
		const n = +v
		return (n > 0 ? "+" : n < 0 ? "−" : "") + num(Math.abs(n), dp) + suffix
	}
	function pct(v, dp = 2) {
		return signed(v, dp, "%")
	}
	// ▲/▼ + signed mono number. Direction is never colour alone.
	function delta(v, dp = 2, suffix = "%") {
		if (v === null || v === undefined || !isFinite(+v)) return '<span class="delta flat">—</span>'
		const n = +v
		const dir = n > 0 ? "up" : n < 0 ? "down" : "flat"
		const arrow = n > 0 ? "▲" : n < 0 ? "▼" : "■"
		return `<span class="delta ${dir}"><span aria-hidden="true">${arrow}</span> ${signed(n, dp, suffix)}</span>`
	}
	function esc(s) {
		return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c])
	}
	function ago(ts) {
		if (!ts) return ""
		const t = typeof ts === "number" ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts)
		const s = Math.max(0, Math.round((Date.now() - t) / 1000))
		if (s < 60) return `${s}s ago`
		if (s < 3600) return `${Math.floor(s / 60)}m ago`
		if (s < 86400) return `${Math.floor(s / 3600)}h ago`
		return `${Math.floor(s / 86400)}d ago`
	}
	// "updated Ns ago" that keeps ticking. Call again with a new ts on refresh.
	const stamps = new Set()
	function stamp(el, ts, prefix = "updated ") {
		if (!el) return
		el.dataset.ts = String(ts || Date.now())
		el.dataset.prefix = prefix
		el.classList.add("ago")
		stamps.add(el)
		paintStamp(el)
	}
	function paintStamp(el) {
		el.textContent = el.dataset.prefix + ago(+el.dataset.ts)
	}
	setInterval(() => {
		for (const el of stamps) {
			if (!el.isConnected) stamps.delete(el)
			else paintStamp(el)
		}
	}, 1000)

	// ------------------------------------------------------- states & toast
	function skeleton(rows = 3, cls = "") {
		return Array.from({ length: rows }, (_, i) =>
			`<span class="skel ${cls}" style="width:${[92, 78, 85, 64, 88][i % 5]}%"></span>`).join("")
	}
	function empty(title, hint = "", ic = "empty") {
		return `<div class="empty">${icon(ic)}<b>${esc(title)}</b>${hint ? esc(hint) : ""}</div>`
	}
	// Inline error with a retry button. Returns an element so the handler
	// survives (innerHTML would drop it).
	function errorBox(msg, retry) {
		const d = doc.createElement("div")
		d.className = "err-box"
		d.setAttribute("role", "alert")
		d.innerHTML = `${icon("alert")}<span class="msg">${esc(msg)}</span>`
		if (retry) {
			const b = doc.createElement("button")
			b.className = "btn sm"
			b.type = "button"
			b.innerHTML = `${icon("refresh")}Retry`
			b.onclick = retry
			d.appendChild(b)
		}
		return d
	}
	let toasts
	function toast(msg, kind = "", ms = 3200) {
		if (!toasts) {
			toasts = doc.createElement("div")
			toasts.className = "tf-toasts"
			toasts.setAttribute("aria-live", "polite")
			doc.body.appendChild(toasts)
		}
		const t = doc.createElement("div")
		t.className = "tf-toast " + kind
		t.setAttribute("role", kind === "err" ? "alert" : "status")
		t.textContent = msg
		toasts.appendChild(t)
		setTimeout(() => t.remove(), ms)
		return t
	}

	// --------------------------------------------- drawer / modal / sheet
	const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
	// overlay({kind:"drawer"|"modal"|"sheet", title, body, footer, onClose})
	// body/footer: Node or HTML string. Returns {el, body, close}.
	function overlay(o = {}) {
		const prev = doc.activeElement
		const ov = doc.createElement("div")
		ov.className = "tf-overlay " + (o.kind || "modal")
		const p = doc.createElement("div")
		p.className = "tf-panel"
		p.setAttribute("role", "dialog")
		p.setAttribute("aria-modal", "true")
		const hid = "tfov" + Math.random().toString(36).slice(2, 8)
		p.setAttribute("aria-labelledby", hid)
		p.innerHTML = `<header><h2 id="${hid}">${esc(o.title || "")}</h2>${o.actions || ""}<button type="button" class="icon-btn" data-close aria-label="Close">${icon("x")}</button></header><div class="tf-body"></div>`
		const body = p.querySelector(".tf-body")
		if (o.body instanceof Node) body.appendChild(o.body)
		else if (o.body) body.innerHTML = o.body
		if (o.footer) {
			const f = doc.createElement("footer")
			if (o.footer instanceof Node) f.appendChild(o.footer)
			else f.innerHTML = o.footer
			p.appendChild(f)
		}
		ov.appendChild(p)
		doc.body.appendChild(ov)
		requestAnimationFrame(() => ov.classList.add("open"))
		let closed = false
		function close() {
			if (closed) return
			closed = true
			doc.removeEventListener("keydown", onKey, true)
			ov.remove()
			if (o.onClose) o.onClose()
			if (prev && prev.focus) prev.focus()
		}
		function onKey(e) {
			if (e.key === "Escape") {
				e.stopPropagation()
				e.preventDefault()
				close()
			} else if (e.key === "Tab") {
				const f = [...p.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null || x === doc.activeElement)
				if (!f.length) return
				const first = f[0], last = f[f.length - 1]
				if (e.shiftKey && doc.activeElement === first) { last.focus(); e.preventDefault() }
				else if (!e.shiftKey && doc.activeElement === last) { first.focus(); e.preventDefault() }
			}
		}
		doc.addEventListener("keydown", onKey, true)
		ov.addEventListener("mousedown", (e) => { if (e.target === ov) close() })
		p.querySelector("[data-close]").onclick = close
		const first = body.querySelector(FOCUSABLE) || p.querySelector("[data-close]")
		setTimeout(() => first && first.focus && first.focus(), 0)
		return { el: ov, panel: p, body, close }
	}

	// -------------------------------------------------- segmented control
	// seg(container, onChange): buttons with data-v; toggles .on/aria-pressed.
	function seg(el, onChange) {
		if (!el) return
		el.classList.add("seg")
		el.setAttribute("role", "group")
		el.addEventListener("click", (e) => {
			const b = e.target.closest("button[data-v]")
			if (!b || !el.contains(b)) return
			for (const x of el.querySelectorAll("button[data-v]")) {
				x.classList.toggle("on", x === b)
				x.setAttribute("aria-pressed", x === b ? "true" : "false")
			}
			onChange && onChange(b.dataset.v, b)
		})
	}

	// --------------------------------------------------------- market bar
	function istParts(d = new Date()) {
		const f = new Intl.DateTimeFormat("en-GB", {
			timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
		})
		const o = {}
		for (const p of f.formatToParts(d)) o[p.type] = p.value
		return o
	}
	// Weekday 09:15–15:30 IST. Holidays aren't known here; the server's
	// /api/board/market answer overrides this when it arrives.
	function marketOpenGuess() {
		const p = istParts()
		if (p.weekday === "Sat" || p.weekday === "Sun") return false
		const m = +p.hour * 60 + +p.minute
		return m >= 555 && m < 930
	}
	let mktState = null
	function paintMarket() {
		const el = doc.querySelector(".tf-mkt")
		if (!el) return
		const p = istParts()
		const live = mktState && typeof mktState.open === "boolean" ? mktState.open : marketOpenGuess()
		el.classList.toggle("live", live)
		el.querySelector(".tf-state").textContent = live ? "LIVE" : "CLOSED"
		el.querySelector(".tf-clock").textContent = `${p.hour}:${p.minute}:${p.second} IST`
		const n = el.querySelector(".tf-nifty")
		if (mktState && mktState.nifty && isFinite(mktState.nifty.last)) {
			n.innerHTML = `NIFTY ${num(mktState.nifty.last, 2)} ${delta(mktState.nifty.chg_pct)}`
			n.hidden = false
		} else n.hidden = true
	}
	async function pollMarket() {
		try {
			const r = await fetch("/api/board/market")
			if (r.ok) mktState = await r.json()
		} catch (e) {}
		paintMarket()
	}

	// --------------------------------------------------------------- shell
	let mounted = false
	function shell(opts = {}) {
		if (mounted) return
		const nav = doc.getElementById("nav")
		if (!nav) return
		mounted = true
		const page = opts.page || nav.dataset.page || ""
		const here = ALL.find((x) => x[0] === page)
		const title = opts.title || nav.dataset.title || (here ? here[1] : doc.title)
		const cur = (k) => (k === page ? ' aria-current="page"' : "")

		nav.classList.add("tf-mounted")
		doc.body.classList.add("tf-app")

		// rail
		const rail = doc.createElement("aside")
		rail.className = "tf-rail"
		rail.setAttribute("aria-label", "Primary")
		const item = ([k, label, href]) =>
			`<a class="tf-item" href="${href}" data-key="${k}" title="${esc(label)}"${cur(k)}>${icon(k)}<span class="tf-label">${esc(label)}</span></a>`
		rail.innerHTML =
			`<a class="tf-brand" href="/board">${'<span class="tf-logo">' + icon("logo") + "</span>"}<span class="tf-word">TickerForge</span></a>` +
			NAV.map((g) => `<nav class="tf-group" aria-label="${g.group}"><div class="tf-group-h">${g.group}</div>${g.items.map(item).join("")}</nav>`).join("") +
			`<div class="tf-foot">${FOOT.map(item).join("")}</div>`

		// top bar: title, the page's own #status, market, theme toggle
		const top = doc.createElement("header")
		top.className = "tf-top"
		top.innerHTML = `<h1 class="tf-title">${esc(title)}</h1><span class="sp"></span>` +
			`<div class="tf-mkt" aria-live="off"><span><span class="tf-dot" aria-hidden="true"></span><span class="tf-state">—</span></span><span class="tf-sep"></span><span class="tf-clock"></span><span class="tf-sep"></span><span class="tf-nifty" hidden></span></div>` +
			`<button type="button" class="icon-btn" data-tf-theme-toggle></button>`
		const status = doc.getElementById("status")
		if (status) top.insertBefore(status, top.querySelector(".sp").nextSibling)
		if (opts.actions) top.insertBefore(opts.actions, top.querySelector(".tf-mkt"))

		// phone tab bar + More sheet
		const tabbar = doc.createElement("nav")
		tabbar.className = "tf-tabbar"
		tabbar.setAttribute("aria-label", "Primary")
		const inMore = !TABS.includes(page)
		tabbar.innerHTML = TABS.map((k) => {
			const [, label, href] = ALL.find((x) => x[0] === k)
			return `<a class="tf-tab" href="${href}" data-key="${k}"${cur(k)}>${icon(k)}<span>${label}</span></a>`
		}).join("") +
			`<button type="button" class="tf-tab" data-key="more" aria-expanded="false" aria-controls="tf-more"${inMore ? ' aria-current="page"' : ""}>${icon("more")}<span>More</span></button>`
		const more = doc.createElement("div")
		more.className = "tf-more"
		more.id = "tf-more"
		more.setAttribute("role", "dialog")
		more.setAttribute("aria-label", "More pages")
		more.innerHTML = `<div class="tf-grab"></div><div class="tf-more-grid">${ALL.filter((x) => !TABS.includes(x[0]))
			.map(([k, label, href]) => `<a href="${href}" data-key="${k}"${cur(k)}>${icon(k)}<span>${esc(label)}</span></a>`).join("")}</div>`
		const scrim = doc.createElement("div")
		scrim.className = "tf-scrim"
		const moreBtn = tabbar.querySelector("[data-key=more]")
		function setMore(open) {
			more.classList.toggle("open", open)
			scrim.classList.toggle("open", open)
			moreBtn.setAttribute("aria-expanded", open ? "true" : "false")
			if (open) { const a = more.querySelector("a"); a && a.focus() }
		}
		moreBtn.onclick = () => setMore(!more.classList.contains("open"))
		scrim.onclick = () => setMore(false)
		more.addEventListener("keydown", (e) => { if (e.key === "Escape") { setMore(false); moreBtn.focus() } })

		nav.append(rail, top, tabbar, scrim, more)
		const t = top.querySelector("[data-tf-theme-toggle]")
		paintToggle(t)
		t.onclick = toggleTheme

		paintMarket()
		setInterval(paintMarket, 1000)
		pollMarket()
		setInterval(pollMarket, 60000)
	}
	doc.addEventListener("DOMContentLoaded", () => shell())

	window.TF = {
		NAV, ALL, shell, icon, theme, setTheme: (t) => applyTheme(t, true), toggleTheme,
		cssVar, chartTheme, colors, alpha,
		num, signed, pct, delta, esc, ago, stamp,
		skeleton, empty, errorBox, toast, overlay, seg,
		istParts, marketOpenGuess,
	}
})()
