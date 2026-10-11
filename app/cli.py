"""TickerForge CLI — Phase 0.

Usage:
  python -m app.cli login                    # daily Kite login
  python -m app.cli sync-instruments         # rebuild local instrument index
  python -m app.cli search INFY              # search stocks
  python -m app.cli watch add RELIANCE       # manage watchlist
  python -m app.cli watch remove RELIANCE
  python -m app.cli watch list
  python -m app.cli notify-test "hello"      # test phone push
  python -m app.cli status                   # session/db overview
"""
import argparse

from . import db, kite_auth, instruments, watchlist, notify, watcher



def _delivery_job(client) -> str | None:
    from . import delivery
    return delivery.maybe_sync(client=client)



def main() -> None:
    db.init()
    p = argparse.ArgumentParser(prog="tickerforge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Daily Kite login (interactive)")
    sub.add_parser("sync-instruments", help="Download instrument dump into SQLite")
    sub.add_parser("status", help="Show session, index and watchlist state")
    sub.add_parser("serve", help="Run the web server (charts UI) on :5000")
    sub.add_parser("up", help="Run all four services under one supervisor")
    sub.add_parser("health", help="Report supervised service status")

    mt = sub.add_parser("maintain", help="Nightly job: backup, top up, prune")
    mt.add_argument("--dry", action="store_true", help="report, change nothing")
    mt.add_argument("--keep", type=int, help="days of backups to keep")
    mt.add_argument("--no-candles", action="store_true",
                    help="skip the candle top-up (no Kite session needed)")
    mt.add_argument("--quiet", action="store_true", help="don't push the report")

    sa = sub.add_parser("semiauto", help="Orders proposed for your approval")
    sasub = sa.add_subparsers(dest="sa_cmd")
    sasub.add_parser("status", help="Armed? What's awaiting approval?")
    sap = sasub.add_parser("propose", help="Propose one by hand")
    sap.add_argument("symbol"); sap.add_argument("--qty", type=int, required=True)
    sap.add_argument("--price", type=float, required=True)
    sap.add_argument("--stop", type=float, required=True)
    sap.add_argument("--tag", default="manual")
    saa = sasub.add_parser("approve"); saa.add_argument("token")
    sar = sasub.add_parser("reject"); sar.add_argument("token")

    mp = sub.add_parser("mf", help="Mutual funds: SIPs, composition, overlap")
    mpsub = mp.add_subparsers(dest="mf_cmd")
    mpsub.add_parser("sips", help="Instructed SIPs, soonest debit first")
    mpsub.add_parser("schemes", help="Imported fund portfolios")
    mi = mpsub.add_parser("import", help="Import an AMC monthly disclosure")
    mi.add_argument("file")
    mi.add_argument("--scheme", required=True, help="how to file it locally")
    mi.add_argument("--as-of", dest="as_of",
                    help="disclosure month, e.g. 2026-07 (default: this month)")
    mo = mpsub.add_parser("overlap", help="How much two funds are the same fund")
    mo.add_argument("a")
    mo.add_argument("b")
    mo.add_argument("--as-of", dest="as_of")
    mpsub.add_parser("lookthrough",
                     help="True exposure: funds + direct holdings")

    pp = sub.add_parser("paper", help="Paper broker: signals traded for real, "
                                     "with no money")
    ppsub = pp.add_subparsers(dest="paper_cmd")
    ppsub.add_parser("status", help="Open, pending and expectancy so far")
    pq = ppsub.add_parser("queue", help="Queue today's flips as orders")
    pq.add_argument("--stop-pct", type=float,
                    help="initial stop as %% below entry")
    ppsub.add_parser("fill", help="Fill anything whose next open has arrived")
    ppsub.add_parser("orders", help="Recent order log")
    pr = ppsub.add_parser("reset", help="Wipe paper history (not real trades)")
    pr.add_argument("--confirm", action="store_true")

    qp = sub.add_parser("query", aliases=["q"],
                        help="Find stocks with an arbitrary expression")
    qp.add_argument("expression", nargs="?",
                    help='e.g. "rsi(14) > 70 and ema(20) crosses_above ema(50)"')
    qp.add_argument("--universe")
    qp.add_argument("--days", type=int, default=420)
    qp.add_argument("--top", type=int, default=25)
    qp.add_argument("--why", type=int, default=0, metavar="N",
                    help="show why N non-matching symbols failed")
    qp.add_argument("--fields", action="store_true", help="list every field")
    qp.add_argument("--examples", action="store_true")
    qp.add_argument("--statements", action="store_true",
                    help="list every statement row you can query")
    qp.add_argument("--backtest", metavar="SYMBOL",
                    help="run the query as a strategy on this symbol")
    qp.add_argument("--save", metavar="NAME", help="store it under a name")
    qp.add_argument("--alert", action="store_true",
                    help="with --save: push when a symbol newly qualifies")
    qp.add_argument("--saved", action="store_true", help="list saved queries")
    qp.add_argument("--forget", metavar="NAME")
    qp.add_argument("--fetch-missing", action="store_true",
                    help="fetch fundamentals for symbols that pass the price "
                         "filter but have none stored")
    qp.add_argument("--check-alerts", action="store_true",
                    help="run every watched query and push what just entered")

    rsp = sub.add_parser("rs", help="Relative strength vs Nifty, ranked")
    rsp.add_argument("--universe")
    rsp.add_argument("--symbols", nargs="*")
    rsp.add_argument("--top", type=int, default=15)

    scr = sub.add_parser("screen", help="Value + momentum: cheap, strong, turning")
    scr.add_argument("--universe")
    scr.add_argument("--min-rs", type=float, default=70.0)
    scr.add_argument("--verdicts", default="undervalued,fair")
    scr.add_argument("--any-signal", action="store_true",
                     help="don't require a strategy to be long")

    mc = sub.add_parser("montecarlo", aliases=["mc"],
                        help="Resample a backtest's trades: how bad could the "
                             "drawdown have been?")
    mc.add_argument("symbol")
    mc.add_argument("strategy")
    mc.add_argument("--sims", type=int, default=1000)
    mc.add_argument("--mode", choices=["shuffle", "bootstrap"],
                    default="shuffle")
    mc.add_argument("--seed", type=int, help="reproducible runs")
    mc.add_argument("--days", type=int, default=730)
    mc.add_argument("--interval", default="day")
    mc.add_argument("--cost-bps", type=float, default=10.0)

    isp = sub.add_parser("install-service",
                         help="Write launchd agents for boot start + nightly job")
    isp.add_argument("--hour", type=int, default=3,
                     help="hour for the nightly job (default 3)")
    isp.add_argument("--dir", help="write plists here instead of ~/Library/LaunchAgents")

    st = sub.add_parser("settings", help="Show or change editable settings")
    st.add_argument("key", nargs="?", help="setting to change")
    st.add_argument("value", nargs="?", help="new value, or 'clear' to revert")

    sp = sub.add_parser("search", help="Search stocks by symbol/name")
    sp.add_argument("query")
    sp.add_argument("--all", action="store_true",
                    help="include non-equity instruments")

    wp = sub.add_parser("watch", help="Manage the watchlist")
    wsub = wp.add_subparsers(dest="wcmd", required=True)
    wa = wsub.add_parser("add"); wa.add_argument("symbol"); wa.add_argument("--exchange")
    wr = wsub.add_parser("remove"); wr.add_argument("symbol")
    wsub.add_parser("list")
    wsub.add_parser("fetch-missing",
                    help="Fetch fundamentals+results for watchlist stocks missing them")

    dp = sub.add_parser("delivery", help="NSE delivery volume (bhavcopy)")
    dsub = dp.add_subparsers(dest="dcmd", required=True)
    ds = dsub.add_parser("sync", help="Fetch missing sessions (default 45 days)")
    ds.add_argument("--days", type=int, default=45)
    dw = dsub.add_parser("show", help="One symbol's delivery vs its average")
    dw.add_argument("symbol")
    dk = dsub.add_parser("spikes", help="Latest session's delivery spikes")
    dk.add_argument("--ratio", type=float, default=2.0)
    dk.add_argument("--all", action="store_true",
                    help="include down days (default: up days only)")


    ep = sub.add_parser("events", help="Corporate events watcher (NSE)")
    esub = ep.add_subparsers(dest="ecmd", required=True)
    esub.add_parser("poll", help="One-shot poll of NSE feeds")
    ed = esub.add_parser("digest", help="Curated market-wide events digest")
    ed.add_argument("--dry", action="store_true",
                    help="print digest, don't send or mark")
    er = esub.add_parser("run", help="Poll continuously + scheduled digests")
    er.add_argument("--every", type=int, default=30,
                    help="seconds between polls during market hours (default 30)")
    er.add_argument("--always", action="store_true",
                    help="poll every --every seconds around the clock (ignore schedule)")
    er.add_argument("--digest-times", default="08:45,21:15",
                    help="daily digest times HH:MM, comma-separated; '' disables")
    er.add_argument("--brief-at", default="08:45",
                    help="pre-market brief time HH:MM (weekdays); '' disables")

    nw = sub.add_parser("news", help="Watchlist news digest")
    nsub = nw.add_subparsers(dest="ncmd", required=True)
    nd = nsub.add_parser("digest", help="Collect + curate + push one digest now")
    nd.add_argument("--hours", type=int, default=24, help="lookback window")
    nd.add_argument("--dry", action="store_true",
                    help="print digest, don't send or mark seen")
    nr = nsub.add_parser("run", help="Send digests at fixed times daily")
    nr.add_argument("--times", default="08:30,18:30",
                    help="comma-separated HH:MM (default 08:30,18:30)")

    fp = sub.add_parser("fund", help="Fundamentals (screener.in)")
    fsub = fp.add_subparsers(dest="fcmd", required=True)
    ff = fsub.add_parser("fetch", help="Fetch + store fundamentals")
    ff.add_argument("symbol", nargs="?", help="omit with --all")
    ff.add_argument("--all", action="store_true", help="whole watchlist")
    fs = fsub.add_parser("show", help="Show stored summary")
    fs.add_argument("symbol")
    fd = fsub.add_parser("diagnose", help="Why did this symbol get no ratios?")
    fd.add_argument("symbol")
    fg = fsub.add_parser("slug", help="Override the screener.in URL slug")
    fg.add_argument("symbol")
    fg.add_argument("slug", nargs="?", help="omit to show the current mapping")

    rp = sub.add_parser("results", help="Market-wide latest quarterly results")
    rsub = rp.add_subparsers(dest="rcmd", required=True)
    rf = rsub.add_parser("fetch-latest", help="Scrape screener's latest-results feed")
    rf.add_argument("--sort", default="latest",
                    choices=["latest", "np", "sales", "mcap", "pe"])
    rl = rsub.add_parser("latest", help="Show the cached latest-results feed")
    rl.add_argument("--sort", default="latest",
                    choices=["latest", "np", "sales", "mcap", "pe"])
    rl.add_argument("--scope", default="all", choices=["all", "watch"])
    rl.add_argument("--limit", type=int, default=20)

    np_ = sub.add_parser("notify-test", help="Send a test push to your phone")
    sub.add_parser("remote", help="Two-way phone control: listen for /commands over ntfy")

    sg = sub.add_parser("signals", help="Run the strategy-signal scan on the watchlist (closed daily bars)")
    sg.add_argument("symbols", nargs="*", help="optional symbols (default: watchlist)")
    sg.add_argument("--push", action="store_true", help="push fresh flips to the phone")

    bt = sub.add_parser("batch", help="Run one strategy across many symbols")
    bt.add_argument("strategy", help="strategy key, e.g. ema_cross")
    bt.add_argument("symbols", nargs="*", help="default: the watchlist")
    bt.add_argument("--days", type=int, default=730)
    bt.add_argument("--interval", default="day")
    bt.add_argument("--cost-bps", type=float, default=10.0)
    bt.add_argument("--sort", default="expectancy_pct",
                    help="expectancy_pct|total_return_pct|profit_factor|"
                         "sharpe|win_rate_pct|max_drawdown_pct|trades|symbol")
    bt.add_argument("--limit", type=int, default=30)

    wf = sub.add_parser("walkforward",
                        help="Optimise on one window, test on the next")
    wf.add_argument("symbol")
    wf.add_argument("strategy")
    wf.add_argument("grid", nargs="*",
                    help="param=5:25:5 or param=5,10,15; omit for defaults")
    wf.add_argument("--is-bars", type=int, default=250)
    wf.add_argument("--oos-bars", type=int, default=60)
    wf.add_argument("--mode", default="rolling", choices=["rolling", "anchored"])
    wf.add_argument("--metric", default="sharpe")
    wf.add_argument("--days", type=int, default=1460)
    wf.add_argument("--cost-bps", type=float, default=10.0)

    up = sub.add_parser("universe", help="Scanner universes + candle backfill")
    usub = up.add_subparsers(dest="ucmd", required=True)
    ul = usub.add_parser("list", help="Universes and their sizes")
    us = usub.add_parser("set", help="Define a universe from symbols")
    us.add_argument("name"); us.add_argument("symbols", nargs="+")
    us.add_argument("--add", action="store_true", help="append instead of replace")
    uw = usub.add_parser("from-watchlist", help="Seed a universe from the watchlist")
    uw.add_argument("name", nargs="?", default="watchlist")
    uc = usub.add_parser("coverage", help="How many symbols have candles")
    uc.add_argument("name", nargs="?", default="nifty200")
    ub = usub.add_parser("backfill", help="Fetch candles (the overnight job)")
    ub.add_argument("name", nargs="?", default="nifty200")
    ub.add_argument("--all", action="store_true", help="re-fetch even if covered")
    ub.add_argument("--days", type=int, default=500)
    ud = usub.add_parser("show", help="List a universe's members")
    ud.add_argument("name")
    um = usub.add_parser("from-market",
                         help="Every ordinary NSE equity — the whole market")
    um.add_argument("name", nargs="?", default="market")
    um.add_argument("--limit", type=int, help="cap it, for a trial run")
    um.add_argument("--include-funds", action="store_true",
                    help="keep ETFs and index funds")
    upr = usub.add_parser("prune",
                          help="Drop illiquid names AFTER a backfill")
    upr.add_argument("name", nargs="?", default="market")
    upr.add_argument("--min-price", type=float, default=20.0)
    upr.add_argument("--min-volume", type=float, default=50_000)
    upr.add_argument("--min-bars", type=int, default=200)
    upr.add_argument("--dry", action="store_true")

    sc = sub.add_parser("scan", help="Run an EOD scan across a universe")
    sc.add_argument("conditions", nargs="+",
                    help="e.g. 52w_high volume_surge  ·  or a saved scan name "
                         "with --saved")
    sc.add_argument("--universe", default="nifty200")
    sc.add_argument("--symbols", help="comma list instead of a universe")
    sc.add_argument("--days", type=int, default=400)
    sc.add_argument("--limit", type=int, default=25)
    sc.add_argument("--saved", action="store_true",
                    help="treat the first argument as a saved scan name")
    sc.add_argument("--save", help="save these conditions under this name")

    hm = sub.add_parser("heatmap", help="Sweep two parameters; peak vs plateau")
    hm.add_argument("symbol")
    hm.add_argument("strategy")
    hm.add_argument("x", help="param=5,10,15  or  param=start:stop:step")
    hm.add_argument("y", nargs="?", help="second param, same syntax")
    hm.add_argument("--metric", default="sharpe")
    hm.add_argument("--days", type=int, default=730)
    hm.add_argument("--interval", default="day")
    hm.add_argument("--cost-bps", type=float, default=10.0)

    pf = sub.add_parser("portfolio", help="Kite holdings (read-only) + annotations")
    psub = pf.add_subparsers(dest="pcmd", required=False)
    psub.add_parser("show", help="Holdings with valuation, signals, stop distance")
    psub.add_parser("sync-watchlist", help="Add holdings to the watchlist")

    bp = sub.add_parser("brief", help="Pre-market brief (positions, tape, alerts)")
    bp.add_argument("--dry", action="store_true",
                    help="print it, don't send or mark the day")
    bp.add_argument("--skip", default="",
                    help="comma-separated sections to omit "
                         "(positions,risk,regime,results,alerts,signals,news)")

    sz = sub.add_parser("size", help="Position size from entry, stop and risk %%")
    sz.add_argument("symbol", nargs="?", help="optional, for the ATR stop")
    sz.add_argument("--entry", type=float, required=True)
    sz.add_argument("--sl", type=float, help="stop; omitted → suggested from ATR")
    sz.add_argument("--risk", type=float, help="%% of capital (default RISK_PCT)")
    sz.add_argument("--account", type=float, help="override ACCOUNT_SIZE")
    sz.add_argument("--lot", type=int, default=1, help="lot size")

    jp = sub.add_parser("journal", help="Trade journal")
    jsub = jp.add_subparsers(dest="jcmd", required=True)
    jl = jsub.add_parser("list", help="Open positions + recent closed")
    jl.add_argument("symbol", nargs="?")
    jo = jsub.add_parser("open", help="Log a trade you've taken")
    jo.add_argument("symbol")
    jo.add_argument("--entry", type=float, required=True)
    jo.add_argument("--sl", type=float, help="stop — without it there is no R")
    jo.add_argument("--qty", type=int, help="omitted → sized from your risk %%")
    jo.add_argument("--side", default="long", choices=["long", "short"])
    jo.add_argument("--tag", help="strategy key, e.g. st_adx")
    jo.add_argument("--note")
    jv = jsub.add_parser("compare",
                         help="Live vs backtested, same tag, same symbols")
    jv.add_argument("tag")
    jv.add_argument("--source", default="paper")
    jv.add_argument("--stop-pct", dest="stop_pct", type=float)
    jv.add_argument("--days", type=int, default=730)
    jc = jsub.add_parser("close", help="Close a logged trade")
    jc.add_argument("id", type=int)
    jc.add_argument("--exit", type=float, required=True)
    jc.add_argument("--reason", default="manual")
    jc.add_argument("--charges", type=float, help="actual; omitted → 10bps/side")
    ja = jsub.add_parser("amend", help="Trail a stop or fix a label")
    ja.add_argument("id", type=int)
    ja.add_argument("--sl", type=float)
    ja.add_argument("--target", type=float)
    ja.add_argument("--tag")
    js = jsub.add_parser("stats", help="Expectancy overall and by tag")
    js.add_argument("--tag")

    ip = sub.add_parser("intraday", help="Live levels + alert watcher")
    isub = ip.add_subparsers(dest="icmd", required=True)
    ir = isub.add_parser("run", help="Watch armed alerts, fire to phone")
    ir.add_argument("--every", type=int, default=5,
                    help="seconds between polls during market hours (default 5)")
    ir.add_argument("--ticks", action="store_true",
                    help="lowest latency: stream via websocket, fire on each tick")
    ir.add_argument("--always", action="store_true",
                    help="poll around the clock (ignore market-hours schedule)")
    il = isub.add_parser("levels", help="Print intraday levels for a symbol")
    il.add_argument("symbol")
    isub.add_parser("alerts", help="List armed alerts")
    np_.add_argument("message", nargs="?", default="TickerForge is alive!")

    args = p.parse_args()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("app.server:app", host="127.0.0.1", port=5000)


    elif args.cmd == "up":
        from . import supervisor
        return supervisor.main()

    elif args.cmd == "health":
        from . import supervisor
        print(supervisor.format_health(supervisor.read_state()))

    elif args.cmd == "maintain":
        from . import maintenance, notify
        kite = None
        if not args.no_candles and not args.dry:
            try:
                kite = kite_auth.get_kite()
            except SystemExit:
                # No session at 03:00 is normal — the token died hours ago and
                # nobody has logged in yet. Back up anyway; that's the part
                # that matters, and it needs no session.
                print("No Kite session — backing up without the candle top-up.")
        report = maintenance.run(kite=kite, dry=args.dry, keep_days=args.keep,
                                 skip_candles=args.no_candles or kite is None)
        text = maintenance.format_report(report)
        print(text)
        if not args.dry and not args.quiet and report["errors"]:
            # Only failures reach the phone. A nightly success push you get
            # 365 times a year is a push you stop reading.
            try:
                notify.send(text, title="TickerForge maintenance",
                            priority="high", tags="warning")
            except (Exception, SystemExit) as e:
                print(f"(could not push: {e})")

    elif args.cmd == "semiauto":
        from . import semiauto as SA
        sub_cmd = getattr(args, "sa_cmd", None) or "status"
        if sub_cmd == "status":
            print(SA.summary())
        elif sub_cmd == "propose":
            out = SA.propose(args.symbol.upper(), args.qty, args.price,
                             args.stop, tag=args.tag)
            if out["problems"]:
                print("Blocked:")
                for x in out["problems"]:
                    print(f"  • {x}")
            else:
                print(f"Proposed. Approve within {out['expires_in_min']} min:")
                print(f"  /approve {out['token']} pin=XXXX")
        elif sub_cmd == "approve":
            try:
                out = SA.approve(args.token)
                print(f"Placed {out['order_id']}"
                      + (f", stop GTT {out['gtt_id']}" if out["stop_attached"]
                         else " — STOP NOT ATTACHED, set it manually"))
            except SA.SemiAutoError as e:
                print(f"Not placed: {e}")
        elif sub_cmd == "reject":
            try:
                SA.reject(args.token); print("rejected")
            except SA.SemiAutoError as e:
                print(e)

    elif args.cmd == "mf":
        from . import mf as MF
        sub_cmd = getattr(args, "mf_cmd", None) or "sips"
        if sub_cmd == "sips":
            print(MF.sips_text(MF.sips(), limit=40))
        elif sub_cmd == "schemes":
            rows = MF.schemes()
            if not rows:
                print("Nothing imported yet.\n"
                      "Kite has no endpoint for what a fund holds — these come\n"
                      "from the AMC's monthly portfolio disclosure:\n"
                      "  tf mf import july.csv --scheme 'Flexi Cap' --as-of 2026-07")
            for r in rows:
                print(f"  {r['scheme']:<38} {r['as_of']}  {r['n']:>3} holdings"
                      f"  {r['matched']:>3} matched")
        elif sub_cmd == "import":
            import pathlib as _pl
            text = _pl.Path(args.file).read_text(encoding="utf-8",
                                                 errors="replace")
            try:
                res = MF.import_portfolio(args.scheme, text, args.as_of)
            except MF.MFError as e:
                print(f"Import failed: {e}")
                return
            print(f"{res['scheme']} ({res['as_of']}): {res['rows']} holdings, "
                  f"{res['resolved']} matched to NSE symbols")
            if res["resolved"] < res["rows"]:
                print(f"  {res['rows'] - res['resolved']} unmatched — overlap "
                      f"still works (it joins on ISIN), but look-through "
                      f"skips them")
        elif sub_cmd == "overlap":
            print(MF.overlap_text(MF.overlap(args.a, args.b, args.as_of)))
        elif sub_cmd == "lookthrough":
            out = MF.look_through(as_of=getattr(args, "as_of", None))
            print(f"True exposure — {out['total']:,.0f}")
            for e in out["exposure"][:25]:
                print(f"  {e['symbol']:<14} {e['pct']:>5.1f}%  "
                      f"{e['total']:>12,.0f}   direct {e['direct']:>10,.0f}"
                      f"   funds {e['via_funds']:>10,.0f}")
            if out.get("note"):
                print(f"\n  {out['note']}")
        return

    elif args.cmd == "paper":
        from . import paper as P
        sub_cmd = getattr(args, "paper_cmd", None) or "status"
        if sub_cmd == "status":
            print(P.summary())
        elif sub_cmd == "queue":
            P.start()
            out = P.from_signals(stop_pct=args.stop_pct)
            print(f"{len(out['queued'])} order(s) queued from "
                  f"{out['flips']} flip(s)")
            for q in out["queued"]:
                print(f"  {q['kind']:<6} {q['symbol']:<12} {q['tag']}")
            for skip in out["skipped"]:
                print(f"  — {skip}")
            print("\nNothing is filled yet: paper orders fill at the NEXT "
                  "open.\nRun `paper fill` after tomorrow's candles land.")
        elif sub_cmd == "fill":
            out = P.fill_pending()
            for f in out["filled"]:
                r = f.get("r_multiple")
                print(f"  {f['kind']:<6} {f['symbol']:<12} {f['qty']:>5} @ "
                      f"{f['price']:<9} {f['when'][:16]}"
                      + (f"  {r:+.2f}R" if r is not None else ""))
            if out["still_pending"]:
                print(f"  {out['still_pending']} still waiting for the next open")
            for prob in out["problems"]:
                print(f"  ! {prob}")
            if not out["filled"] and not out["still_pending"]:
                print("nothing pending")
        elif sub_cmd == "orders":
            for o in P.orders(30):
                print(f"  #{o['id']:<4} {o['status']:<10} {o['kind']:<6} "
                      f"{o['symbol']:<12} signalled {o['signal_time'][:16]}"
                      + (f" filled {o['fill_price']}" if o["fill_price"] else ""))
        elif sub_cmd == "reset":
            try:
                out = P.reset(confirm=args.confirm)
                print(f"cleared {out['orders']} orders, {out['trades']} trades")
            except P.PaperError as e:
                print(e)

    elif args.cmd in ("query", "q"):
        from . import query as Q, universe as U, watchlist as W
        if args.fields:
            for f in Q.fields():
                sig = f["name"] + (f"({', '.join(str(d) for d in f['defaults'])})"
                                   if f["args"] else "")
                print(f"  {sig:26} {f['help']}")
            return
        if args.statements:
            # The accessors take a label, so the labels have to be
            # discoverable — they're screener's and they drift.
            from . import fundamentals as F
            cat = F.row_catalogue()
            if not any(cat.values()):
                print("no statements stored yet — run `fundamentals fetch`")
                return
            alias = {}
            for name, (kind, label) in Q.STATEMENT_ALIASES.items():
                alias.setdefault(label.lower(), name)
            for stmt in sorted(cat):
                how = "q" if stmt in F.QUARTERLY else "fin"
                print(f"\n{stmt}   ->   {how}(\"...\")")
                for row in cat[stmt]:
                    short = alias.get(row.lower())
                    print(f"  {row:34}" + (f"  ({short})" if short else ""))
            print("\nAlso: ttm(\"row\"), growth(\"row\", years), yoy(\"row\")")
            return
        if args.saved:
            rows = Q.saved()
            if not rows:
                print("no saved queries")
            for r in rows:
                mark = "!" if r["alert"] else " "
                where = r["universe"] or "watchlist"
                print(f" {mark} {r['name']:<18} {where:<14} {r['expression']}")
            return
        if args.forget:
            print("forgotten" if Q.forget(args.forget) else "no such query")
            return
        if args.check_alerts:
            out = Q.check_alerts()
            if not out["entered"]:
                print(f"{out['checked']} watched, nothing new")
            for name, syms in out["entered"].items():
                print(f"  {name}: {', '.join(syms)} entered")
            for name, err in out["errors"].items():
                print(f"  ! {name}: {err}")
            return
        if args.save and args.expression:
            try:
                Q.save(args.save, args.expression, args.universe, args.alert)
                print(f"saved '{args.save}'"
                      + (" — will push on new entries" if args.alert else ""))
            except Q.QueryError as e:
                print(f"Not saved: {e}")
            return
        if args.backtest and args.expression:
            try:
                res = Q.backtest(args.expression, args.backtest.upper())
            except Q.QueryError as e:
                print(f"Can't backtest: {e}"); return
            st = res["stats"]
            print(f"{res['symbol']} · {res['strategy_name']}\n")
            print(f"  trades      {st['trades']}")
            print(f"  expectancy  {st['expectancy_pct']:+.2f}%")
            print(f"  win rate    {st['win_rate_pct']:.0f}%")
            print(f"  max DD      {st['max_drawdown_pct']:.1f}%")
            print(f"  return      {st['total_return_pct']:+.1f}%  "
                  f"(buy & hold {res['buyhold']['total_return_pct']:+.1f}%)")
            print(f"  long on     {res['bars_long']} of {res['bars']} bars")
            return
        if args.examples or not args.expression:
            print("Examples:")
            for q, means in Q.EXAMPLES:
                print(f"  {q:44} — {means}")
            if not args.expression:
                print("\nRun `query --fields` for everything you can use.")
            return
        syms = (U.symbols(args.universe) if args.universe
                else [r["tradingsymbol"] for r in W.items()])
        if not syms:
            print("nothing to scan"); return
        def tick(done, total, what):
            # A whole-market screen takes a while. Silence for two minutes
            # reads as a hang.
            print(f"  {what}: {done}/{total}", end="\r", flush=True)

        try:
            res = Q.run(args.expression, syms, days=args.days,
                        explain_misses=args.why,
                        fetch_missing=args.fetch_missing,
                        progress=tick if len(syms) > 200 else None)
            if len(syms) > 200:
                print(" " * 60, end="\r")
        except Q.QueryError as e:
            print(f"Bad query: {e}"); return
        print(Q.summary(res, top=args.top))

    elif args.cmd == "rs":
        from . import rs as RS, universe as U, watchlist as W
        syms = ([x.upper() for x in args.symbols] if args.symbols
                else U.symbols(args.universe) if args.universe
                else [r["tradingsymbol"] for r in W.items()])
        if not syms:
            print("nothing to rank"); return
        try:
            print(RS.summary(RS.rank(syms), top=args.top))
        except RS.RSError as e:
            print(f"Can't rank: {e}")

    elif args.cmd == "screen":
        from . import screen as SC, universe as U
        syms = U.symbols(args.universe) if args.universe else None
        res = SC.run(symbols=syms, min_rs=args.min_rs,
                     verdicts=tuple(v.strip() for v in args.verdicts.split(",")),
                     require_long=not args.any_signal)
        print(SC.summary(res))

    elif args.cmd in ("montecarlo", "mc"):
        from . import backtest as B, montecarlo as MC
        res = B.run(args.symbol.upper(), args.strategy, interval=args.interval,
                    days=args.days, cost_bps=args.cost_bps)
        try:
            out = MC.run(res["trades"], sims=args.sims, mode=args.mode,
                         seed=args.seed)
        except MC.MonteCarloError as e:
            print(f"Can't simulate: {e}"); return
        print(f"{args.symbol.upper()} · {args.strategy}\n")
        print(MC.format_report(out))

    elif args.cmd == "install-service":
        from pathlib import Path as _Path
        from . import service
        result = service.install(
            directory=_Path(args.dir) if args.dir else None, hour=args.hour)
        print(service.format_install(result))

    elif args.cmd == "settings":
        from . import settings as _settings
        if not args.key:
            print(_settings.as_text())
        elif args.value is None:
            print(f"{args.key} = {_settings.value(args.key)} "
                  f"({_settings.source(args.key)})")
        elif args.value == "clear":
            _settings.clear(args.key)
            print(f"{args.key} reverted to {_settings.value(args.key)} "
                  f"({_settings.source(args.key)})")
        else:
            try:
                new = _settings.set_value(args.key, args.value)
                print(f"{args.key} = {new}")
            except _settings.SettingError as e:
                print(f"Not saved: {e}")
    elif args.cmd == "login":
        kite_auth.login_interactive()

    elif args.cmd == "sync-instruments":
        from . import config
        kite = kite_auth.get_kite()
        n = instruments.sync(kite)
        print(f"Synced {n} instruments from {', '.join(config.EXCHANGES)}.")

    elif args.cmd == "search":
        results = instruments.search(args.query, equities_only=not args.all)
        if not results:
            print("No matches. (Have you run sync-instruments?)")
        for r in results:
            print(f"{r['tradingsymbol']:<16} {r['exchange']:<4} "
                  f"{r['instrument_type']:<3} {r['name']}")

    elif args.cmd == "watch":
        if args.wcmd == "add":
            inst = watchlist.add(args.symbol, args.exchange, autofetch=False)
            state = "added" if inst.get("added") else "already on watchlist"
            print(f"Added {inst['tradingsymbol']} ({inst['exchange']}) — "
                  f"{inst['name']}  [{state}]")
            if watchlist.autofetch_enabled():
                print("Fetching fundamentals + results from screener.in …")
                status = watchlist.ensure_fundamentals(
                    inst["tradingsymbol"], only_if_missing=True)
                print(f"  {status}.")
        elif args.wcmd == "remove":
            n = watchlist.remove(args.symbol)
            print("Removed." if n else "Not on watchlist.")
        elif args.wcmd == "fetch-missing":
            print("Priming fundamentals + results for watchlist …")
            for sym, status in watchlist.fetch_missing().items():
                print(f"  {sym:<16} {status}")
        else:
            rows = watchlist.items()
            if not rows:
                print("Watchlist is empty. Add with: watch add SYMBOL")
            for r in rows:
                print(f"{r['tradingsymbol']:<16} {r['exchange']:<4} {r['name']}")

    elif args.cmd == "delivery":
        from . import delivery
        if args.dcmd == "sync":
            r = delivery.sync(days=args.days)
            print(f"fetched {r['fetched']} session(s); latest {r['last_day']}")
            for e in r["errors"]:
                print(f"  ! {e}")
        elif args.dcmd == "show":
            print(delivery.as_text(args.symbol))
        else:
            for r in delivery.spikes(ratio=args.ratio, up_only=not args.all, limit=50):
                chg = "" if r["change_pct"] is None else f"{r['change_pct']:+6.2f}%"
                print(f"{r['symbol']:<14} {r['deliv_ratio']:>5.1f}×  "
                      f"{r['deliv_pct']:>5.1f}% deliv  {chg}")

    elif args.cmd == "events":
        import time as _time
        from . import events
        from .nse import NSEClient
        client = NSEClient()
        if args.ecmd == "poll":
            for k, v in events.poll_once(client).items():
                print(f"{k:<14} {v}")
        elif args.ecmd == "digest":
            print(events.digest(dry=args.dry))
        else:
            dtimes = [t.strip() for t in args.digest_times.split(",") if t.strip()]
            mode = "always-on" if args.always else "market hours + evening/weekend sweeps"
            print(f"Polling NSE every {args.every}s ({mode}); digests at "
                  f"{', '.join(dtimes) or 'never'}. Ctrl+C to stop.")
            fired_today: set = set()
            today = _time.strftime("%Y-%m-%d")
            last_poll = 0.0
            while True:
                do_poll, why = (True, "always") if args.always else events.should_poll()
                # a 'sweep' slot fires once; market mode respects the --every gap
                slot = _time.strftime("%H:%M")
                if do_poll and why == "sweep" and f"poll@{slot}" in fired_today:
                    do_poll = False
                if do_poll and why == "market" and (_time.time() - last_poll) < args.every:
                    do_poll = False
                if do_poll:
                    try:
                        summary = events.poll_once(client)
                        # only surface a line when the poll actually found
                        # something new — silence when nothing changed
                        newish = any(
                            isinstance(v, str) and v[0:1].isdigit()
                            and not v.startswith("0")
                            for v in summary.values())
                        if newish or why == "sweep":
                            stamp = _time.strftime("%H:%M:%S")
                            print(f"[{stamp}] ({why}) " +
                                  "  ".join(f"{k}:{v}" for k, v in summary.items()
                                           if not str(v).startswith("0")))
                    except (Exception, SystemExit) as e:
                        print(f"poll error: {e}")
                    last_poll = _time.time()
                    if why == "sweep":
                        fired_today.add(f"poll@{slot}")
                if _time.strftime("%Y-%m-%d") != today:
                    today = _time.strftime("%Y-%m-%d")
                    fired_today.clear()
                now = _time.strftime("%H:%M")
                for t in dtimes:
                    if now >= t and t not in fired_today:
                        fired_today.add(t)
                        try:
                            print(f"[{now}] digest: {events.digest()}")
                        except Exception as e:
                            print(f"digest error: {e}")
                        break

                # Pre-market brief. Lives here because this is the process
                # already awake before the open — the intraday watcher is
                # idling until 09:15. maybe_send is date-guarded in the DB, so
                # a restart can't double-send.
                try:
                    from . import brief as _brief
                    if _brief.maybe_send(at=args.brief_at):
                        print(f"[{now}] pre-market brief sent")
                except Exception as e:
                    print(f"brief error: {e}")

                # 09:08 pre-open gaps, evening delivery file, insider
                # disclosures. Each is self-scheduling and date/time-guarded,
                # and each failure is its own: one NSE endpoint down mustn't
                # take the others with it.
                for label, job in (
                        ("delivery", _delivery_job),
                ):
                    try:
                        msg = job(client)
                        if msg:
                            print(f"[{_time.strftime('%H:%M:%S')}] {msg}")
                    except (Exception, SystemExit) as e:
                        print(f"{label} error: {e}")
                _time.sleep(20)      # wake often; should_poll() gates real work

    elif args.cmd == "news":
        import time as _time
        from . import news
        if args.ncmd == "digest":
            print(news.send_digest(hours=args.hours, dry=args.dry))
        else:
            times = [t.strip() for t in args.times.split(",") if t.strip()]
            print(f"News digests daily at {', '.join(times)}. Ctrl+C to stop.")
            fired = None
            while True:
                now = _time.strftime("%H:%M")
                if now in times and fired != now:
                    fired = now
                    try:
                        print(f"[{now}] {news.send_digest(hours=24)}")
                    except Exception as e:
                        print(f"[{now}] digest error: {e}")
                _time.sleep(20)

    elif args.cmd == "fund":
        from . import fundamentals
        if args.fcmd == "fetch":
            if args.all:
                for sym, res in fundamentals.fetch_all().items():
                    print(f"{sym:<16} {res}")
            elif args.symbol:
                fundamentals.fetch_and_store(args.symbol.upper())
                print(f"Fetched {args.symbol.upper()}.")
            else:
                print("Give a symbol or --all")
        elif args.fcmd == "diagnose":
            d = fundamentals.diagnose(args.symbol)
            print(f"{d['symbol']}  (screener slug: {d['slug']}"
                  f"{' — overridden' if d['slug_overridden'] else ''})")
            for s in d["stages"]:
                print(f"  [{'ok ' if s['ok'] else 'FAIL'}] {s['stage']:18} "
                      f"{s['detail']}")
            print(f"\n{d['verdict']}")
            if d["fix"]:
                print(d["fix"])
        elif args.fcmd == "slug":
            if args.slug:
                fundamentals.set_slug(args.symbol, args.slug)
                print(f"{args.symbol.upper()} → screener.in/company/{args.slug}/")
                print("Now run: fund fetch " + args.symbol.upper())
            else:
                print(f"{args.symbol.upper()} → screener.in/company/"
                      f"{fundamentals.slug_for(args.symbol)}/")
        else:
            s = fundamentals.summary(args.symbol)
            if not s:
                print("Nothing stored. Run: fund fetch " + args.symbol.upper())
            else:
                print(f"{s['symbol']} ({s['variant']}, fetched {s['fetched_at']})")
                for k, v in s["snapshot"].items():
                    print(f"  {k:<24} {v}")
                print("  -- computed --")
                for k, v in s["computed"].items():
                    print(f"  {k:<32} {v}")

    elif args.cmd == "notify-test":
        notify.send(args.message, title="TickerForge test", tags="tada")
        print("Sent. Check your phone.")

    elif args.cmd == "remote":
        from . import commands
        commands.listen()

    elif args.cmd == "results":
        from . import latest_results as L
        if args.rcmd == "fetch-latest":
            print(f"Scraping screener latest-results feed (sort={args.sort})…")
            n = L.refresh(sort=args.sort)
            print(f"cached {n} companies as of {L.fetched_at()}")
        elif args.rcmd == "latest":
            data = L.rows(scope=args.scope, sort=args.sort, limit=args.limit)
            if not data:
                print("No cached feed. Run: results fetch-latest"); return
            total = len(L.rows(scope=args.scope))
            print(f"Latest results ({args.scope}, by {args.sort}) — "
                  f"{total} companies, showing {len(data)}:")
            for r in data:
                print(f"  {(r.get('name') or r.get('symbol') or '?')[:24]:24} "
                      f"NP {L._f(r.get('np_qtr'),0):>10} {L._pct(r.get('np_yoy')):>8}  "
                      f"Sales {L._f(r.get('sales_qtr'),0):>10} {L._pct(r.get('sales_yoy')):>8}")

    elif args.cmd == "signals":
        from . import signals
        res = signals.scan(symbols=[x.upper() for x in args.symbols] or None,
                           push=args.push)
        for f in res["flips"]:
            print(f"{'LONG' if f['action']=='LONG' else 'FLAT'}  {f['symbol']:12} "
                  f"{f['name']}  @ {f['price']}")
        if not res["flips"]:
            print("no fresh flips")
        for sym, row in sorted(res["matrix"].items()):
            longs = [k for k, v in row.items() if v]
            print(f"  {sym:12} long: {', '.join(longs) or '—'}")
        for e in res["errors"]:
            print(f"  ! {e}")

    elif args.cmd == "walkforward":
        from . import walkforward as W
        grid = {}
        for spec in args.grid:
            name, _, vals = spec.partition("=")
            if not vals:
                print(f"Bad grid entry '{spec}' — use param=5:25:5"); return
            grid[name] = (vals if ":" in vals else
                          [int(float(v)) if float(v).is_integer() else float(v)
                           for v in vals.split(",") if v.strip()])
        try:
            res = W.run(args.symbol.upper(), args.strategy, grid or None,
                        is_bars=args.is_bars, oos_bars=args.oos_bars,
                        mode=args.mode, metric=args.metric, days=args.days,
                        cost_bps=args.cost_bps)
        except ValueError as e:
            print(f"Can't walk that forward: {e}"); return
        print(W.as_text(res))
        print()
        print(W.CAVEAT)

    elif args.cmd == "universe":
        from . import universe as U
        if args.ucmd == "list":
            rows = U.names()
            if not rows:
                print("No universes yet. Try: universe from-watchlist")
            for r in rows:
                mark = U.last_backfill(r["name"])
                print(f"  {r['name']:<16} {r['n']:>4} symbols"
                      + (f"   backfilled {mark}" if mark else "   never backfilled"))
        elif args.ucmd == "set":
            r = U.set_members(args.name, args.symbols, replace=not args.add)
            print(f"{args.name}: {r['added']} symbols")
            if r["unknown"]:
                print(f"  ! not in the instrument index: {', '.join(r['unknown'])}")
        elif args.ucmd == "from-watchlist":
            r = U.from_watchlist(args.name)
            print(f"{args.name}: {r['added']} symbols from the watchlist")
        elif args.ucmd == "from-market":
            out = U.from_market(args.name, limit=args.limit,
                                include_funds=args.include_funds)
            n = len(out["symbols"])
            print(f"{args.name}: {n} equities")
            if n > 500:
                mins = n * 0.4 / 60
                print(f"\nBackfilling {n} symbols is roughly {mins:.0f} "
                      f"minutes of rate-limited requests. Run it overnight:")
                print(f"  python -m app.cli universe backfill {args.name} "
                      f"--days 500")
                print(f"Then drop what you couldn't trade anyway:")
                print(f"  python -m app.cli universe prune {args.name}")

        elif args.ucmd == "prune":
            out = U.prune(args.name, min_price=args.min_price,
                          min_volume=args.min_volume,
                          min_bars=args.min_bars, dry=args.dry)
            verb = "would keep" if out["dry"] else "kept"
            print(f"{verb} {out['kept']}, dropped {out['dropped']}")
            for sym, why in list(out["why"].items())[:15]:
                print(f"  {sym:<14} {why}")

        elif args.ucmd == "show":
            for m in U.members(args.name):
                print(f"  {m['tradingsymbol']}")
        elif args.ucmd == "coverage":
            c = U.coverage(args.name, days=args.days if hasattr(args, "days") else 500)
            print(f"{c['universe']}: {c['covered']}/{c['symbols']} have candles")
            if c["missing"]:
                print(f"  missing ({len(c['missing'])}): "
                      f"{', '.join(c['missing'][:10])}"
                      + (" …" if len(c["missing"]) > 10 else ""))
            if c["thin"]:
                print(f"  thin ({len(c['thin'])}): {', '.join(c['thin'][:10])}"
                      + (" …" if len(c["thin"]) > 10 else ""))
            print(f"  {c['todo']} to fetch, roughly {c['est_seconds']:.0f}s")
        else:
            def prog(i, n, sym, state):
                mark = {"ok": "+", "skip": ".", "fail": "!"}[state]
                print(f"  [{i}/{n}] {mark} {sym}", flush=True)
            try:
                r = U.backfill(args.name, days=args.days,
                               only_missing=not args.all, progress=prog)
            except (ValueError, RuntimeError) as e:
                print(f"Backfill failed: {e}"); return
            U.mark_backfilled(args.name)
            print(f"\nfetched {r['fetched']}, skipped {r['skipped']}, "
                  f"failed {len(r['failed'])} in {r['seconds']:.0f}s")
            for f in r["failed"][:10]:
                print(f"  ! {f['symbol']}: {f['error']}")

    elif args.cmd == "scan":
        from . import scanner as S, universe as U
        if args.saved:
            try:
                res = S.run_saved(args.conditions[0])
            except ValueError as e:
                print(e)
                print("Saved: " + ", ".join(s["name"] for s in U.saved_scans()))
                return
        else:
            bad = [c for c in args.conditions if c not in S.CONDITIONS]
            if bad:
                print(f"Unknown condition(s): {', '.join(bad)}")
                print("Available: " + ", ".join(S.CONDITIONS))
                return
            try:
                res = S.scan(list(args.conditions), name=args.universe,
                             symbols=([x.strip().upper()
                                       for x in args.symbols.split(",")]
                                      if args.symbols else None),
                             days=args.days, limit=args.limit)
            except ValueError as e:
                print(e); return
            if args.save:
                U.save_scan(args.save, {"universe": args.universe,
                                        "conditions": list(args.conditions),
                                        "days": args.days, "limit": args.limit})
                print(f"(saved as '{args.save}')")
        print(S.as_text(res, limit=args.limit))

    elif args.cmd == "heatmap":
        from . import heatmap

        def split(spec):
            if not spec:
                return None, None
            name, _, vals = spec.partition("=")
            if not vals:
                print(f"Bad sweep '{spec}' — use param=5,10,15 or param=5:30:5")
                return None, None
            # "a:b:c" stays a string so heatmap._values expands it; a comma
            # list becomes explicit values. Keeping them distinct is what
            # stops "5,10,15" being read as a range and collapsing to one cell.
            out = (vals if ":" in vals else
                   [int(float(v)) if float(v).is_integer() else float(v)
                    for v in vals.split(",") if v.strip()])
            return name, out

        xp, xv = split(args.x)
        yp, yv = split(args.y)
        if not xp:
            return
        try:
            res = heatmap.sweep(args.symbol.upper(), args.strategy, xp, xv,
                                yp, yv, metric=args.metric,
                                interval=args.interval, days=args.days,
                                cost_bps=args.cost_bps)
        except ValueError as e:
            print(f"Can't sweep that: {e}")
            return
        print(heatmap.as_text(res))
        print()
        print(heatmap.CAVEAT)

    elif args.cmd == "batch":
        from . import batch
        try:
            res = batch.run(args.strategy,
                            symbols=[x.upper() for x in args.symbols] or None,
                            interval=args.interval, days=args.days,
                            cost_bps=args.cost_bps, sort=args.sort)
        except ValueError as e:
            print(f"Can't run that: {e}")
            print("Strategies: " + ", ".join(sorted(
                __import__("app.backtest", fromlist=["x"]).STRATEGIES)))
            return
        print(batch.as_text(res, limit=args.limit))
        print()
        print(batch.CAVEAT)

    elif args.cmd == "portfolio":
        from . import portfolio
        try:
            if getattr(args, "pcmd", None) == "sync-watchlist":
                r = portfolio.sync_watchlist()
                print(f"added {len(r['added'])}: {', '.join(r['added']) or '—'}")
                for f in r["failed"]:
                    print(f"  ! {f}")
            else:
                print(portfolio.as_text(portfolio.snapshot()))
        except portfolio.PortfolioError as e:
            print(f"Portfolio unavailable: {e}")

    elif args.cmd == "brief":
        from . import brief
        skip = tuple(x.strip() for x in args.skip.split(",") if x.strip())
        if args.dry or skip:
            print(brief.compose(skip=skip))
        else:
            print(brief.send())

    elif args.cmd == "size":
        from . import config, risk
        account = args.account or float(config.get("ACCOUNT_SIZE", 0) or 0)
        risk_pct = args.risk or float(config.get("RISK_PCT", 1) or 1)
        if account <= 0:
            print("No ACCOUNT_SIZE set. Add it to .env or pass --account."); return
        stop = args.sl
        if stop is None:
            if not args.symbol:
                print("Give --sl, or a SYMBOL so a stop can be suggested from ATR.")
                return
            inst = instruments.lookup(args.symbol)
            if not inst:
                print(f"Unknown symbol {args.symbol}."); return
            from . import candles as cs
            kite = kite_auth.get_kite()
            cs.ensure(kite, inst["instrument_token"], "day", 120)
            sug = risk.atr_stop(cs.get(inst["instrument_token"], "day", 120),
                                entry=args.entry, period=14, mult=2)
            if not sug:
                print("Not enough history for an ATR stop — pass --sl."); return
            stop = sug["stop"]
            print(f"stop from 2xATR({sug['atr']:g}) = {stop:g} "
                  f"({sug['distance_pct']:g}% away)")
        try:
            print(risk.format_size(
                risk.size(args.entry, stop, account, risk_pct,
                          max_position_pct=float(config.get("MAX_POSITION_PCT", 100)),
                          lot_size=args.lot)))
        except risk.RiskError as e:
            print(f"Can't size that: {e}")

    elif args.cmd == "journal":
        from . import config, journal, risk
        if args.jcmd == "compare":
            print(journal.compare_text(journal.compare(
                args.tag, source=args.source, days=args.days,
                stop_pct=args.stop_pct)))
            return
        dl = float(config.get("DAILY_LOSS_R", 2))
        mp = int(config.get("MAX_POSITIONS", 5))

        if args.jcmd == "open":
            qty = args.qty
            if qty is None:
                account = float(config.get("ACCOUNT_SIZE", 0) or 0)
                if account <= 0 or args.sl is None:
                    print("Need --qty (no ACCOUNT_SIZE set, or no --sl given)."); return
                qty = risk.size(args.entry, args.sl, account,
                                float(config.get("RISK_PCT", 1))) ["qty"]
                print(f"sized {qty} from {config.get('RISK_PCT', 1)}% risk")
            try:
                tid = journal.open_trade(args.symbol.upper(), entry=args.entry,
                                         qty=qty, stop=args.sl, side=args.side,
                                         tag=args.tag, notes=args.note,
                                         source="cli")
            except journal.JournalError as e:
                print(f"Not logged: {e}"); return
            print(journal.format_trade(journal.get(tid)))
            if args.sl is None:
                print("  ! no stop — this trade can't be scored in R")

        elif args.jcmd == "close":
            try:
                t = journal.close_trade(args.id, exit_price=args.exit,
                                        reason=args.reason, charges=args.charges)
            except journal.JournalError as e:
                print(f"Not closed: {e}"); return
            print(journal.format_trade(t))
            g = journal.guard(daily_loss_r=dl, max_positions=mp)
            print(f"  today {g['realised_r']:+.2f}R · "
                  f"{g['remaining_r']:.2f}R left before the limit")
            for r in g["reasons"]:
                print(f"  ! {r}")

        elif args.jcmd == "amend":
            fields = {}
            if args.sl is not None:
                fields["stop"] = args.sl
            if args.target is not None:
                fields["target"] = args.target
            if args.tag:
                fields["tag"] = args.tag
            if not fields:
                print("Nothing to amend. Use --sl / --target / --tag."); return
            try:
                t = journal.update_trade(args.id, **fields)
            except journal.JournalError as e:
                print(f"Not amended: {e}"); return
            print(journal.format_trade(t))
            if "stop" in fields and t.get("initial_stop") is not None:
                print(f"  (R still measured against the entry stop "
                      f"{t['initial_stop']:g})")

        elif args.jcmd == "stats":
            print(journal.format_stats(journal.stats(tag=args.tag), label="All: "))
            print()
            for tag, s in journal.by_tag().items():
                print(journal.format_stats(s, label=f"{tag}: "))
                print()

        else:
            sym = args.symbol.upper() if args.symbol else None
            opens = journal.open_positions(sym)
            print(f"Open ({len(opens)}):" if opens else "No open positions.")
            for t in opens:
                print("  " + journal.format_trade(t))
            closed = journal.closed_trades(symbol=sym, limit=15)
            if closed:
                print("\nClosed:")
                for t in closed:
                    print("  " + journal.format_trade(t))
            print()
            print(journal.format_stats(journal.stats()))
            g = journal.guard(daily_loss_r=dl, max_positions=mp)
            print(f"Today {g['realised_r']:+.2f}R realised · "
                  f"{g['open_risk_r']:.2f}R live · "
                  f"{g['remaining_r']:.2f}R to the limit")
            for r in g["reasons"]:
                print(f"  ! {r}")

    elif args.cmd == "intraday":
        # Imported here rather than at module scope so `cli --help` and the
        # cheap subcommands don't pay for the watcher's import chain.
        from . import alerts as al, intraday
        if args.icmd == "run":
            if args.ticks:
                watcher.run_ticks(always=args.always)
            else:
                watcher.run(every=args.every, always=args.always)
        elif args.icmd == "levels":
            inst = instruments.lookup(args.symbol)
            if not inst:
                print(f"Unknown symbol {args.symbol}."); return
            kite = kite_auth.get_kite()
            lv = intraday.levels(kite, inst["tradingsymbol"],
                                 inst["instrument_token"])
            print(intraday.as_text(lv))
        elif args.icmd == "alerts":
            rows = al.active()
            if not rows:
                print("No armed alerts.")
            for r in rows:
                print(f"#{r['id']:<4} {al.label(r)}")

    elif args.cmd == "status":
        from . import config
        with db.connect() as conn:
            n_inst = conn.execute("SELECT COUNT(*) c FROM instruments").fetchone()["c"]
            n_watch = conn.execute("SELECT COUNT(*) c FROM watchlist").fetchone()["c"]
        print(f"Kite session   : {kite_auth.token_status()}")
        print(f"Instruments    : {n_inst} "
              f"(synced: {db.get_meta('instruments_synced_at') or 'never'})")
        print(f"Watchlist      : {n_watch} symbols")
        print(f"ntfy topic set : {'yes' if config.NTFY_TOPIC else 'NO — edit .env'}")


if __name__ == "__main__":
    main()
