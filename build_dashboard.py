#!/usr/bin/env python3
"""Builds the public BTC Carry Fund board. Stdlib only, no keys.

    python3 build_dashboard.py [index.html]

Three inputs, and the page never depends on the third:
  1. live reads at the latest block (live_reads.py), shown on every build;
  2. live_samples.jsonl, one reading appended per build, which carries the charts
     past the last sync;
  3. public_snapshot.json, the agent activity and history synced from KPK's tooling.
     Without it the page still builds and the history-based parts say so.
"""
import base64
import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_reads as L  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "index.html"
CONFIG = json.loads((HERE / "config.json").read_text())
SNAPSHOT_PATH = HERE / "public_snapshot.json"
SAMPLES_PATH = HERE / "live_samples.jsonl"
ASSETS = HERE / "assets"

PERF_FEE = float(CONFIG["performance_fee"])
MIN_HF = float(CONFIG["minimum_hf"])
# Realised carry needs this many days since the re-lever; overridable only to exercise the code path.
REALISED_DAYS = float(os.environ.get("BOARD_REALISED_DAYS", "7"))
DAY = 86_400
ETHERSCAN = "https://etherscan.io"

# --- Formatting ---------------------------------------------------------------------


def esc(x):
    return html.escape(str(x), quote=True)


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def stamp(ts):
    return utc(ts).strftime("%-d %b %H:%M UTC")


def short(ts):
    return utc(ts).strftime("%-d %b %H:%M")


def n(x, dp=2):
    return "n/a" if x is None else f"{x:,.{dp}f}"


def pct(x, dp=2, sign=False):
    if x is None:
        return "n/a"
    return f"{x:+.{dp}f}%" if sign else f"{x:.{dp}f}%"


def usd(x, dp=0):
    return "n/a" if x is None else f"${x:,.{dp}f}"


def musd(x):
    if x is None:
        return "n/a"
    return f"${x / 1e6:,.2f}M" if abs(x) >= 1e6 else f"${x / 1e3:,.0f}k"


def hf_txt(x):
    if x is None:
        return "no debt"
    return "> 100 (almost no debt)" if x > 100 else f"{x:.4f}"


def dur(seconds):
    s = int(round(seconds))
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min"
    d, h = divmod(h, 24)
    return f"{d} days {h} h"


def short_hash(h):
    return f"{h[:8]}…{h[-4:]}"


def safe(fn):
    try:
        return fn(), None
    except Exception as exc:  # a soft source failing must not stop the page
        return None, str(exc)[:200]


LOGOS = {p.stem: "data:image/webp;base64," + base64.b64encode(p.read_bytes()).decode() for p in sorted((ASSETS / "logos").glob("*.webp"))}


def icon(name, size=22):
    src = LOGOS.get(name)
    return f'<img class="logo-icon" src="{src}" width="{size}" height="{size}" alt="">' if src else ""



# --- Data ---------------------------------------------------------------------------

now = int(time.time())
chain = L.read_chain()  # the one hard requirement: without the position there is no page
apys, apys_err = safe(L.read_vault_apys)
rebate, rebate_err = safe(L.read_borrow_rebate)
rewards, rewards_err = safe(lambda: L.read_rewards(chain["btc_px"]))
rebate_hist, rebate_hist_err = safe(lambda: L.read_rebate_history([c["opportunity"] for c in (rebate or {}).get("campaigns", [])]))
LOOP = CONFIG["loop_sleeve"]
loop_draws = {s: chain["debt_usdc"] * s * (LOOP["leverage"] - 1) for s in LOOP["shares"]}
loop_in, loop_err = safe(lambda: L.read_loop_inputs([round(d) for d in loop_draws.values()]))

snap = None
if SNAPSHOT_PATH.exists():
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
    except ValueError:
        snap = None
synced_at = int(datetime.strptime(snap["synced_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) if snap else None
synced_block = snap["synced_block"] if snap else 0

live_samples = []
if SAMPLES_PATH.exists():
    for line in SAMPLES_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                live_samples.append(json.loads(line))
            except ValueError:
                pass

# A NAV read with a stale price is not a NAV. Show the last good one, and say so.
nav_note = ""
if not chain["nav_healthy"]:
    good = [s for s in live_samples + list((snap or {}).get("samples", [])) if s.get("nav")]
    last_good = max(good, key=lambda s: s["t"], default=None)
    if last_good:
        nav_note = f"The NAV calculator flagged a stale price at this block; NAV shows the last good reading, {stamp(last_good['t'])}."
        chain["nav_wbtc"] = last_good["nav"]
        chain["nav_usd"] = last_good["nav"] * chain["btc_px"]
        chain["share_price_wbtc"] = last_good["nav"] / last_good["sup"]
    else:
        nav_note = "The NAV calculator flagged a stale price at this block, so NAV is approximate until the next build."

# Band: the last synced values, and every change to them in the window.
band_hist = (snap or {}).get("band", {}).get("history") or []
if band_hist:
    band = band_hist[-1]
    band_asof = f"synced {stamp(synced_at)}"
else:
    band = CONFIG["band_fallback"]
    band_asof = f"as set on {band['as_of']}"


def band_at(t):
    if not band_hist:
        return band
    current = band_hist[0]
    for b in band_hist:
        if b["t"] <= t:
            current = b
    return current


# Liquid sleeve: blend the 24h APYs by the live allocation, over the vaults that report one.
alloc = {v["address"].lower(): v["usdc"] for v in chain["vaults"]}
sleeve_usdc = sum(alloc.values())
vault_rows = []
for v in chain["vaults"]:
    a = next((x for x in (apys or []) if x["address"].lower() == v["address"].lower()), None)
    vault_rows.append({**v, "apy": a["apy_24h"] if a else None, "apy_7d": a["apy_7d"] if a else None, "apy_30d": a.get("apy_30d") if a else None, "apy_ex": a.get("apy_24h_ex_rewards") if a else None, "weight": v["usdc"] / sleeve_usdc if sleeve_usdc else 0})
reporting = [v for v in vault_rows if v["apy"] is not None and v["usdc"] > 0]
deploy_apy = sum(v["apy"] * v["usdc"] for v in reporting) / sum(v["usdc"] for v in reporting) if reporting else None
# Morpho's average net APY already counts the Merkl rewards the vaults pass on; this is their share of it.
with_ex = [v for v in reporting if v.get("apy_ex") is not None]
vault_rewards_pp = sum((v["apy"] - v["apy_ex"]) * v["usdc"] for v in with_ex) / sum(v["usdc"] for v in with_ex) if with_ex else None

core = chain["core_usdc"]
rp = chain["risk_premium_bps"] / 10_000
borrow_gross = core["borrow_apy"] * (1 + rp)
rebate_pct = rebate["rebate_pct"] if rebate else 0.0
borrow_net = borrow_gross - rebate_pct
ltv = chain["ltv"] or 0.0
spread = None if deploy_apy is None else deploy_apy - borrow_net
carry = None if spread is None else ltv * spread * (1 - PERF_FEE)
hf_now = chain["hf"]
cf = chain["cf"]
px = chain["btc_px"]

# Readings for the charts: synced history, then the live tail, then this build's own read.
this_sample = {
    "t": chain["read_at"], "b": chain["block"], "hf": round(hf_now, 5) if hf_now else None,
    "px": round(px, 2) if px else None, "liq": round(chain["liq_px"], 2) if chain["liq_px"] else None,
    "coll": round(chain["collateral_wbtc"], 8), "debt": round(chain["debt_usdc"], 2), "cf": round(cf, 4),
    "nav": round(chain["nav_wbtc"], 8) if chain["nav_healthy"] else None, "sup": round(chain["supply"], 8),
    "rew_usd": round(rewards["total_usd"], 2) if rewards else None,
    "rew_wbtc": round(rewards["total_wbtc"], 8) if rewards and rewards["total_wbtc"] else None,
    "core_borrow": round(core["borrow_apy"], 3),
}
points = list((snap or {}).get("samples", []))
points += [s for s in live_samples if s.get("b", 0) > synced_block]
if not points or this_sample["b"] > max(p["b"] for p in points):
    points.append(this_sample)
points.sort(key=lambda p: (p["t"], 0 if p.get("tag") == "before" else 1))

transactions = sorted((snap or {}).get("transactions", []), key=lambda r: (r["block"], r["hash"]))
window_t0 = (snap or {}).get("window_start", {}).get("t") or (points[0]["t"] if points else now)
runs = (snap or {}).get("runs") or {}

history_t0 = (snap or {}).get("history_start", {}).get("t") or window_t0
# The HF and price charts start here: early setup is noise, and the testing examples worth seeing are from October.
charts_t0 = max(history_t0, int(datetime.strptime(CONFIG.get("charts_from", "2000-01-01T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()))
# The live phase starts at the pre-pilot subscription; everything before it is setup and tests at a few dollars.
live_txs = [t for t in transactions if t["t"] >= window_t0]
test_txs = [t for t in transactions if t["t"] < window_t0]
subs = [t for t in live_txs if t["kind"] in ("subscription", "collateral")]
relevers = [t for t in live_txs if t["kind"] == "relever"]
seed = next((t for t in live_txs if t["kind"] == "seed_borrow"), None)
first_relever = relevers[0] if relevers else None
relever_switch = next((b for i, b in enumerate(band_hist) if i > 0 and b["relever"] and not band_hist[i - 1]["relever"]), None)

# Has the position moved since the snapshot? Debt accrues interest, so allow for that.
position_changed = False
if snap and snap.get("position_at_sync"):
    ps = snap["position_at_sync"]
    accrued = ps["debt_usdc"] * (1 + borrow_gross / 100 * max(0, now - ps["t"]) / (365 * DAY))
    if abs(chain["collateral_wbtc"] - ps["collateral_wbtc"]) > 1e-8 or abs(chain["debt_usdc"] - accrued) > max(50.0, 0.002 * accrued):
        position_changed = True

# --- Derived figures ---------------------------------------------------------------------

after_relever = [p for p in points if first_relever and p["t"] >= first_relever["t"] and p.get("hf")]
closest = None
for p in after_relever:
    b = band_at(p["t"])
    headroom = (1 - b["repay_trigger"] / p["hf"]) * 100
    if closest is None or headroom < closest["headroom"]:
        closest = {"headroom": headroom, "t": p["t"], "hf": p["hf"], "trigger": b["repay_trigger"]}
liq_buffer = (1 - 1 / hf_now) * 100 if hf_now else None
lowest = None
if closest:
    lowest = {**closest, "to_liq": (1 - 1 / closest["hf"]) * 100}

# Share of the health-factor manager's runs since the first re-lever that found the position inside the band.
band_runs = [h for h in runs.get("hourly", []) if first_relever and h["t"] >= first_relever["t"]]  # full hours after the re-lever
inside_n = sum(h["states"].get("inside the band, no action", 0) for h in band_runs)
band_total = sum(h["n"] for h in band_runs)


def trailing_spread(days):
    """Vault yield, gross borrow, rebate and spread averaged over the last `days`, or None without the history."""
    t_lo = now - days * DAY
    key = {7: "apy_7d", 30: "apy_30d"}[days]
    vs = [v for v in vault_rows if v.get(key) is not None and v["usdc"] > 0]
    if not vs:
        return None
    vault = sum(v[key] * v["usdc"] for v in vs) / sum(v["usdc"] for v in vs)
    hist = [(r["t"], r["apy"]) for r in (snap or {}).get("core_usdc_rates", []) if r["t"] >= t_lo]
    last_t = hist[-1][0] if hist else 0
    hist += [(s["t"], s["core_borrow"]) for s in live_samples if s.get("core_borrow") and s["t"] > last_t and s["t"] >= t_lo]
    hist.append((now, core["borrow_apy"]))
    if hist[0][0] > t_lo + DAY:  # the history does not reach back far enough to call this a trailing average
        return None
    borrow = sum(v for _, v in hist) / len(hist) * (1 + rp)
    if rebate_hist is None:
        return None
    reb = sum(v for d, v in rebate_hist if d >= t_lo - DAY / 2) / days
    net = borrow - reb
    return {"vault": vault, "borrow_gross": borrow, "rebate": reb, "borrow_net": net, "spread": vault - net}


spreads = [{"key": "live", "label": "Live", "vault": deploy_apy, "borrow_gross": borrow_gross, "rebate": rebate_pct, "borrow_net": borrow_net, "spread": spread}]
for d in (7,):  # 30d dropped (Giel, 12 Oct): most of its window predates the rebate on this hub
    tr = trailing_spread(d)
    if tr and tr["spread"] is not None:
        spreads.append({"key": f"{d}d", "label": f"{d}d avg", **tr})
spreads = [s for s in spreads if s["spread"] is not None]
for s in spreads:
    s["carry"] = ltv * s["spread"] * (1 - PERF_FEE)

# Loop sleeve projection. Today's only USDG line with room is too thin for the loop's own draw, so the
# projection prices the loop at today's Core-hub USDG rate, which is what more Aave capacity would offer.
loop_proj = None
if spread is not None and loop_in:
    a, L_ = loop_in["syrup_7d"], LOOP["leverage"]
    b_need = chain["core_usdg"]["borrow_apy"]
    y_loop = a * L_ - b_need * (L_ - 1)
    rows_ = [{"share": 0.0, "carry": carry}]
    for s_ in LOOP["shares"]:
        mix = (1 - s_) * deploy_apy + s_ * y_loop
        rows_.append({"share": s_, "carry": ltv * (mix - borrow_net) * (1 - PERF_FEE), "today_rate": loop_in["borrow_after"].get(round(loop_draws[s_]))})
    loop_proj = {"syrup": a, "borrow": b_need, "gd_now": loop_in["borrow_now"], "gd_liq": loop_in["liquidity"], "yield": y_loop, "rows": rows_,
                 "breakeven": a * L_ / (L_ - 1), "loop_hf": LOOP["collateral_factor"] * L_ / (L_ - 1)}


def share_price(p):
    return p["nav"] / p["sup"] if p.get("nav") and p.get("sup") else None


def avg_over(t_lo, t_hi, fn):
    vals = [fn(p) for p in points if t_lo <= p["t"] <= t_hi and fn(p) is not None]
    return sum(vals) / len(vals) if vals else None


def rewards_wbtc_at(t):
    """Unclaimed rewards in WBTC at a moment: the nearest build that read them, else zero before the first borrow."""
    have = [s for s in live_samples if s.get("rew_wbtc") is not None]
    near = min(have, key=lambda s: abs(s["t"] - t), default=None)
    if near and abs(near["t"] - t) <= 6 * 3600:
        return near["rew_wbtc"]
    start = seed["t"] if seed else window_t0
    if t <= start:
        return 0.0
    if have:  # rewards accrue roughly linearly between claims
        first = min(have, key=lambda s: s["t"])
        if t < first["t"]:
            return first["rew_wbtc"] * (t - start) / max(1, first["t"] - start)
    return None


realised = None
realised_from = (first_relever["t"] + REALISED_DAYS * DAY) if first_relever else None
if first_relever and now >= realised_from:
    end, start = now, now - REALISED_DAYS * DAY
    sp_end = avg_over(end - DAY, end, share_price)
    sp_start = avg_over(start, start + DAY, share_price)
    years = REALISED_DAYS / 365
    if sp_end and sp_start:
        r_nav = ((sp_end / sp_start) ** (1 / years) - 1) * 100
        rw_end, rw_start = rewards_wbtc_at(end), rewards_wbtc_at(start)
        r_all = None
        if rw_end is not None and rw_start is not None:
            sup = chain["supply"]
            r_all = (((sp_end + rw_end / sup) / (sp_start + rw_start / sup)) ** (1 / years) - 1) * 100

        def stable_usd(p):  # the USDC leg's equity, in dollars: everything that is not the BTC
            return (p["nav"] - p["coll"] - chain["idle_wbtc"]) * p["px"] if p.get("nav") and p.get("px") else None

        e_end = avg_over(end - DAY, end, stable_usd)
        e_start = avg_over(start, start + DAY, stable_usd)
        realised = {"nav": r_nav, "all": r_all, "usd_pnl": (e_end - e_start) if e_end is not None and e_start is not None else None}

# --- SVG charts ------------------------------------------------------------------------------

W, H = 1000, 380
PAD_L, PAD_R, PAD_T, PAD_B = 64, 92, 22, 34


class Frame:
    def __init__(self, t0, t1, y0, y1, log=False, h=H):
        self.t0, self.t1 = t0, max(t1, t0 + 1)
        self.y0, self.y1, self.log, self.h = y0, y1, log, h
        self.pw, self.ph = W - PAD_L - PAD_R, h - PAD_T - PAD_B

    def x(self, t):
        return PAD_L + (t - self.t0) / (self.t1 - self.t0) * self.pw

    def y(self, v):
        if self.log:
            f = (math.log(v) - math.log(self.y0)) / (math.log(self.y1) - math.log(self.y0))
        else:
            f = (v - self.y0) / (self.y1 - self.y0)
        return PAD_T + (1 - min(1.05, max(-0.05, f))) * self.ph


def time_ticks(f):
    out = []
    start = int(f.t0 // DAY + 1) * DAY
    span_days = (f.t1 - f.t0) / DAY
    step = DAY * max(1, int(span_days // 8) + (1 if span_days > 8 else 0))
    for t in range(start, int(f.t1), int(step)):
        x = f.x(t)
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T + f.ph}"/>'
                   f'<text class="tick" x="{x:.1f}" y="{f.h - 12}" text-anchor="middle">{utc(t).strftime("%-d %b")}</text>')
    return "".join(out)


def y_ticks(f, values, fmt):
    out = []
    for v in values:
        y = f.y(v)
        out.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + f.pw}" y2="{y:.1f}"/>'
                   f'<text class="tick" x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end">{esc(fmt(v))}</text>')
    return "".join(out)


def path(f, pts, key):
    segs, cur = [], []
    for p in pts:
        v = p.get(key) if isinstance(p, dict) else p[1]
        t = p["t"] if isinstance(p, dict) else p[0]
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
            continue
        cur.append(f"{f.x(t):.1f},{f.y(v):.1f}")
    if cur:
        segs.append(cur)
    return " ".join("M" + " L".join(s) for s in segs if s)


def marker(kind, x, y, title):
    """Shape carries the action as well as colour: up = borrow, down = repay, ring = deposit, diamond = manual."""
    t = f"<title>{esc(title)}</title>"
    if kind in ("relever",):
        return f'<g class="mk relever"><path d="M{x:.1f},{y - 7:.1f} L{x + 6.5:.1f},{y + 5:.1f} L{x - 6.5:.1f},{y + 5:.1f} Z"/>{t}</g>'
    if kind in ("derisk",):
        return f'<g class="mk derisk"><path d="M{x:.1f},{y + 7:.1f} L{x + 6.5:.1f},{y - 5:.1f} L{x - 6.5:.1f},{y - 5:.1f} Z"/>{t}</g>'
    if kind in ("unwind",):
        return f'<g class="mk unwind"><path d="M{x:.1f},{y + 7:.1f} L{x + 6.5:.1f},{y - 5:.1f} L{x - 6.5:.1f},{y - 5:.1f} Z"/>{t}</g>'
    if kind in ("seed_borrow",):
        return f'<g class="mk manual"><path d="M{x:.1f},{y - 7:.1f} L{x + 7:.1f},{y:.1f} L{x:.1f},{y + 7:.1f} L{x - 7:.1f},{y:.1f} Z"/>{t}</g>'
    return f'<g class="mk deposit"><circle cx="{x:.1f}" cy="{y:.1f}" r="5.5"/>{t}</g>'


def hover_attr(rows):
    return esc(json.dumps(rows, separators=(",", ":")))


def chart_svg(cid, f, body, hover, label):
    return (f'<div class="chart" id="{cid}"><svg viewBox="0 0 {W} {f.h}" role="img" aria-label="{esc(label)}" data-hover="{hover_attr(hover)}">'
            f'{body}<line class="xhair" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T + f.ph}" visibility="hidden"/>'
            f'<rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{f.pw}" height="{f.ph}"/></svg>'
            f'<div class="tip" role="status"></div></div>')


def tx_title(t):
    return f"{stamp(t['t'])} · {t['actor']} · {t['action']} HF {hf_txt(t['hf_before'])} → {hf_txt(t['hf_after'])}"


def testing_shade(f):
    """The testing phase, shaded and named, so its few-dollar moves are not read as the live fund."""
    tp = CONFIG["milestones"]["testing_phase"]
    a, b = max(f.t0, iso_ts(tp["from"])), min(f.t1, window_t0)
    if b <= a:
        return ""
    return (f'<rect class="phase" x="{f.x(a):.1f}" y="{PAD_T}" width="{f.x(b) - f.x(a):.1f}" height="{f.ph}"/>'
            f'<text class="lab" x="{f.x(a) + 6:.1f}" y="{PAD_T + 14}">testing, a few dollars of KPK\'s own money</text>')


def iso_ts(s):
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def hf_chart():
    pts = [p for p in points if p["t"] >= charts_t0 and p.get("hf")]
    if len(pts) < 2:
        return '<p class="empty">The health-factor history appears once the position has a few readings.</p>'
    t0, t1 = charts_t0, max(now, pts[-1]["t"])
    # Capped: after a near-full unwind HF reads in the tens of thousands, which would flatten the band.
    top = min(max(max(p["hf"] for p in pts) * 1.08, band["borrow_trigger"] * 1.1), 3.0)
    f = Frame(t0, t1, 0.97, top, log=True)
    body = [testing_shade(f)]
    pts_plot = [{**p, "hf_plot": min(p["hf"], top)} for p in pts]
    # The band in force at each moment, drawn from the first reading with debt.
    edges = [b for b in band_hist if b["t"] > pts[0]["t"]]
    starts = [pts[0]["t"]] + [b["t"] for b in edges]
    ends = starts[1:] + [t1]
    for s, e in zip(starts, ends):
        b = band_at(s)
        x0, x1 = f.x(s), f.x(e)
        yt, yb = f.y(b["borrow_trigger"]), f.y(b["repay_trigger"])
        body.append(f'<rect class="band" x="{x0:.1f}" y="{yt:.1f}" width="{x1 - x0:.1f}" height="{yb - yt:.1f}"/>'
                    f'<line class="band-edge" x1="{x0:.1f}" y1="{yt:.1f}" x2="{x1:.1f}" y2="{yt:.1f}"/>'
                    f'<line class="band-edge" x1="{x0:.1f}" y1="{yb:.1f}" x2="{x1:.1f}" y2="{yb:.1f}"/>'
                    f'<line class="band-target" x1="{x0:.1f}" y1="{f.y(b["borrow_target"]):.1f}" x2="{x1:.1f}" y2="{f.y(b["borrow_target"]):.1f}"/>')
    ticks = [1.0, 1.1, 1.2, 1.3, 1.5, 2.0, 2.5, 3.0]
    body.append(y_ticks(f, [v for v in ticks if v <= top], lambda v: f"{v:.1f}"))
    body.append(time_ticks(f))
    yl = f.y(1.0)
    body.append(f'<line class="liq" x1="{PAD_L}" y1="{yl:.1f}" x2="{PAD_L + f.pw}" y2="{yl:.1f}"/>'
                f'<text class="lab liq-lab" x="{PAD_L + f.pw + 6}" y="{yl + 4:.1f}">liq. 1.00</text>')
    b = band
    body.append(f'<text class="lab band-lab" x="{PAD_L + f.pw + 6}" y="{f.y(b["borrow_trigger"]) + 4:.1f}">borrow {b["borrow_trigger"]:.2f}</text>'
                f'<text class="lab band-lab" x="{PAD_L + f.pw + 6}" y="{f.y(b["repay_trigger"]) + 12:.1f}">repay {b["repay_trigger"]:.2f}</text>')
    body.append(f'<path class="line" d="{path(f, pts_plot, "hf_plot")}"/>')
    # Name the planned hold so a high HF after the seed borrow does not read as a fault.
    if seed and first_relever and seed.get("hf_after"):
        body.append(f'<text class="lab" x="{f.x(seed["t"]) - 10:.1f}" y="{f.y(min(seed["hf_after"], top)) + 4:.1f}" text-anchor="end">seed borrow, then a planned hold</text>')
        body.append(f'<text class="lab" x="{f.x(first_relever["t"]) + 8:.1f}" y="{f.y(first_relever["hf_after"] or 1.21) - 22:.1f}">re-lever on: levered to target</text>')
    if synced_at and any(p["t"] > synced_at for p in pts):
        xs = f.x(synced_at)
        body.append(f'<line class="sync" x1="{xs:.1f}" y1="{PAD_T}" x2="{xs:.1f}" y2="{PAD_T + f.ph}"/><text class="lab" x="{xs + 4:.1f}" y="{PAD_T + 12}">last sync</text>')
    for t in transactions:
        if t["t"] < t0:
            continue
        if t.get("hf_after") and t["kind"] in ("relever", "derisk", "seed_borrow", "unwind"):
            body.append(marker(t["kind"], f.x(t["t"]), f.y(min(t["hf_after"], top)), tx_title(t)))
        else:
            x = f.x(t["t"])
            body.append(f'<line class="event" x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T + f.ph}"><title>{esc(tx_title(t))}</title></line>')
    hover = []
    for p in pts:
        bb = band_at(p["t"])
        hover.append({"x": round(f.x(p["t"]), 1), "l": [stamp(p["t"]) + (" · testing" if p["t"] < window_t0 else ""), f"HF {hf_txt(p['hf'])}", f"band {bb['repay_trigger']:.2f} to {bb['borrow_trigger']:.2f}, target {bb['borrow_target']:.2f}" + ("" if bb["relever"] else ", re-lever off"), f"debt {n(p['debt'], 0)} USDC"]})
    return chart_svg("hfChart", f, "".join(body), hover, "Health factor over time with the band in force and a marker per transaction")


def price_chart():
    pts = [p for p in points if p["t"] >= charts_t0 and p.get("px")]
    if len(pts) < 2:
        return '<p class="empty">The price history appears once the position has a few readings.</p>'
    t0, t1 = charts_t0, max(now, pts[-1]["t"])
    # A near-empty loan has a liquidation price near zero; leave a gap there rather than a spike.
    with_liq = [p for p in pts if p.get("liq") and p["liq"] > 0.3 * p["px"]]
    lo = min([p["px"] for p in pts] + [p["liq"] for p in with_liq]) * 0.96
    hi = max([p["px"] for p in pts] + [p["liq"] * band_at(p["t"])["borrow_trigger"] for p in with_liq]) * 1.02
    f = Frame(t0, t1, lo, hi)
    body = [testing_shade(f)]
    step = 10 ** math.floor(math.log10((hi - lo) / 4))
    step = step * (5 if (hi - lo) / step > 20 else 2 if (hi - lo) / step > 8 else 1)
    body.append(y_ticks(f, [v * step for v in range(math.ceil(lo / step), math.floor(hi / step) + 1)], lambda v: f"{v / 1000:,.0f}k"))
    body.append(time_ticks(f))
    # The no-action corridor: liquidation price x [repay trigger, borrow trigger], with the band in force.
    # While re-lever is off the agent only repays, so the corridor has no upper edge. Each run of readings
    # with the same re-lever setting is drawn on its own, and a reading without a loan breaks the corridor.
    # A retune between two readings is drawn as a step at the moment it took effect, not a slope.
    shown = {id(p) for p in with_liq}
    rows = []
    for p in pts:
        ok = id(p) in shown
        if rows and rows[-1]["ok"] and ok:
            for nb in band_hist:
                if rows[-1]["t"] < nb["t"] <= p["t"]:
                    rows += [{**rows[-1], "t": nb["t"]}, {**rows[-1], "t": nb["t"], "band": nb}]
        rows.append({"t": p["t"], "liq": p.get("liq"), "band": band_at(p["t"]), "ok": ok})
    runs, cur, key = [], [], None
    for r in rows:
        if not r["ok"]:
            if cur:
                runs.append((key, cur))
            cur, key = [], None
            continue
        k = r["band"]["relever"]
        if cur and k != key:
            runs.append((key, cur + [r]))
            cur = []
        if not cur:
            key = k
        cur.append(r)
    if cur:
        runs.append((key, cur))
    for relever, run in runs:
        if len(run) < 2:
            continue
        lower = [(r["t"], r["liq"] * r["band"]["repay_trigger"]) for r in run]
        upper = [(r["t"], r["liq"] * r["band"]["borrow_trigger"] if relever else hi) for r in run]
        poly = " ".join(f"{f.x(t):.1f},{f.y(v):.1f}" for t, v in upper) + " " + " ".join(f"{f.x(t):.1f},{f.y(v):.1f}" for t, v in reversed(lower))
        body.append(f'<polygon class="band{"" if relever else " open"}" points="{poly}"/>')
        body.append((f'<path class="band-edge" d="{path(f, upper, None)}"/>' if relever else "") + f'<path class="band-edge" d="{path(f, lower, None)}"/>')
        if not relever and run[-1]["t"] - run[0]["t"] >= 6 * 3600:
            x, y = f.x(run[0]["t"]) + 12, f.y(lower[0][1]) - 6
            body.append(f'<text class="lab band-lab" transform="rotate(-90 {x:.1f} {y:.1f})" x="{x:.1f}" y="{y:.1f}">re-lever off: repays only</text>')
    body.append(f'<path class="liq" d="{path(f, [{"t": p["t"], "liq": p["liq"] if id(p) in shown else None} for p in pts], "liq")}"/>')
    body.append(f'<path class="line" d="{path(f, pts, "px")}"/>')
    last = pts[-1]
    body.append(f'<text class="lab" x="{PAD_L + f.pw + 6}" y="{f.y(last["px"]) + 4:.1f}">BTC {last["px"] / 1000:,.1f}k</text>')
    if with_liq:
        ll = with_liq[-1]
        body.append(f'<text class="lab liq-lab" x="{PAD_L + f.pw + 6}" y="{f.y(ll["liq"]) + 4:.1f}">liq. {ll["liq"] / 1000:,.1f}k</text>')
    for t in transactions:
        if t["t"] < t0 or not t.get("px"):
            continue
        if t["kind"] in ("subscription", "collateral"):
            x = f.x(t["t"])
            body.append(f'<line class="event" x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T + f.ph}"><title>{esc(tx_title(t))}</title></line>')
        else:
            body.append(marker(t["kind"], f.x(t["t"]), f.y(t["px"]), tx_title(t)))
    hover = []
    for p in pts:
        bb = band_at(p["t"])
        rows = [stamp(p["t"]), f"BTC oracle price ${p['px']:,.0f}"]
        if p.get("liq") and not bb["relever"]:
            rows += [f"re-lever off: the agent only repays, below ${p['liq'] * bb['repay_trigger']:,.0f}", f"liquidation price ${p['liq']:,.0f}"]
        elif p.get("liq"):
            rows += [f"no-action corridor ${p['liq'] * bb['repay_trigger']:,.0f} to ${p['liq'] * bb['borrow_trigger']:,.0f}", f"liquidation price ${p['liq']:,.0f}"]
        else:
            rows += ["no debt yet"]
        hover.append({"x": round(f.x(p["t"]), 1), "l": rows})
    return chart_svg("priceChart", f, "".join(body), hover, "BTC oracle price against the no-action corridor, the liquidation price and agent actions")


def share_events():
    """What moved the strategy, oldest first: the config names what the data cannot, the data names the rest."""
    ev = [(iso_ts(x["t"]), x["label"], x.get("short", x["label"])) for x in CONFIG["milestones"]["events"]]
    if deposit_tx:
        ev.append((deposit_tx["t"], f"pre-pilot deposit of {deposit_tx['amount']}", "pre-pilot deposit"))
    if seed:
        ev.append((seed["t"], f"seed borrow of {seed['amount']} on the Core hub", "seed borrow"))
    if first_relever:
        ev.append((first_relever["t"], f"re-lever to the {first_relever['target'] or band['borrow_target']:.2f} target", "re-lever"))
    ev += [(t["t"], f"rewards claimed, {t['amount']}", "rewards claimed") for t in live_txs if t["kind"] == "claim"]
    ev += [(t["t"], f"redemption of {t['amount']}", "redemption") for t in live_txs if t["kind"] == "redemption"]
    return sorted(ev)


def share_chart():
    pts = []
    for p in ((snap or {}).get("series") or {}).get("points", []):
        if p["t"] < window_t0 and p.get("share_price_wbtc"):
            pts.append({"t": p["t"], "sp": p["share_price_wbtc"]})
    pts += [{"t": p["t"], "sp": share_price(p)} for p in points if p["t"] >= window_t0 and share_price(p)]
    if len(pts) < 2:
        return '<p class="empty">The share-price history appears after the first sync.</p>'
    vals = [p["sp"] for p in pts]
    pad = max((max(vals) - min(vals)) * 0.25, 0.00005)
    f = Frame(pts[0]["t"], max(now, pts[-1]["t"]), min(vals) - pad * 2.5, max(vals) + pad, h=320)
    body = []
    tp = CONFIG["milestones"]["testing_phase"]
    a, b = max(f.t0, iso_ts(tp["from"])), min(f.t1, iso_ts(tp["to"]))
    if b > a:
        body.append(f'<rect class="phase" x="{f.x(a):.1f}" y="{PAD_T}" width="{f.x(b) - f.x(a):.1f}" height="{f.ph}"/>'
                    f'<text class="lab" x="{f.x(a) + 6:.1f}" y="{PAD_T + 14}">{esc(tp["label"])}</text>')
    step = 10 ** math.floor(math.log10((f.y1 - f.y0) / 3))
    body.append(y_ticks(f, [v * step for v in range(math.ceil(f.y0 / step), math.floor(f.y1 / step) + 1)][::2] or [f.y0], lambda v: f"{v:.4f}"))
    body.append(time_ticks(f))
    body.append(f'<path class="line" d="{path(f, pts, "sp")}"/>')
    # Numbered markers; events within 6 hours share one marker, and the list under the chart says what each is.
    last_x = -1e9
    for i, (ts, label, short_label) in enumerate(share_events(), start=1):
        if not (f.t0 <= ts <= f.t1):
            continue
        x = f.x(ts)
        tx = max(x + 4, last_x + 15)  # keep vertical labels at least 15 units apart
        last_x = tx
        body.append(f'<line class="event" x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T + f.ph}"/>'
                    f'<text class="lab ev-v" transform="rotate(-90 {tx + 9:.1f} {PAD_T + f.ph - 4})" x="{tx + 9:.1f}" y="{PAD_T + f.ph - 4}" text-anchor="start">{i}. {esc(short_label)}</text>')
    body.append(f'<text class="lab" x="{PAD_L + f.pw + 6}" y="{f.y(pts[-1]["sp"]) + 4:.1f}">{pts[-1]["sp"]:.5f}</text>')
    hover = [{"x": round(f.x(p["t"]), 1), "l": [stamp(p["t"]), f"{p['sp']:.6f} WBTC per share"]} for p in pts]
    return chart_svg("shareChart", f, "".join(body), hover, "Share price in WBTC since inception")


def share_events_list():
    items = "".join(f'<li><span class="when">{i}. {esc(utc(ts).strftime("%-d %b %H:%M"))}</span><span>{esc(label)}</span></li>' for i, (ts, label, _) in enumerate(share_events(), start=1))
    return f'<ul class="timeline events">{items}</ul>'


# --- The automation at work: legs, counters, narrative ------------------------------------------

STATE_ORDER = ["inside the band, no action", "above the borrow trigger, re-lever switched off", "no debt to manage yet, no action"]


def clusters(txs=None):
    """Transactions within 15 minutes of each other are one event; a re-lever switch joins the event it caused."""
    out = []
    for t in (live_txs if txs is None else txs):
        if out and t["t"] - out[-1]["t1"] <= 900:
            out[-1]["txs"].append(t)
            out[-1]["t1"] = t["t"]
        else:
            out.append({"t": t["t"], "t1": t["t"], "txs": [t]})
    for c in out:
        c["switch"] = relever_switch if relever_switch and -900 <= c["t"] - relever_switch["t"] <= 900 else None
    return out


def states_between(a, b):
    counts = {}
    for h in runs.get("hourly", []):
        if a <= h["t"] + 1800 < b:
            for k, v in h["states"].items():
                counts[k] = counts.get(k, 0) + v
    return counts


def brief(t):
    """One transaction in a few words, for the legs table; the full sentence is in the actions table."""
    k, a = t["kind"], t["amount"] or ""
    if k in ("seed_borrow", "relever"):
        return f"+{a} borrowed" + (", split across both vaults" if "deposited" in t["action"] else "")
    if k == "derisk":
        return f"{a} repaid"
    if k == "subscription":
        m = re.search(r"minted ([\d.,]+) shares", t["action"])
        return f"{a} in" + (f", {m.group(1)} shares minted" if m else "")
    if k == "collateral":
        return f"{a} supplied as collateral"
    return t["action"].rstrip(".")


def leg_rows():
    cs = clusters()
    rows = []
    badge = {"relever": ("positive", "re-lever"), "derisk": ("negative", "repay"), "seed_borrow": ("info", "manual seed borrow"), "subscription": ("info", "deposit"),
             "collateral": ("info", "supply as collateral"), "claim": ("generic", "rewards claimed"), "redemption": ("negative", "redemption"), "requests": ("generic", "requests"), "other": ("generic", "other")}
    for i, c in enumerate(cs):
        last = c["txs"][-1]
        p_after = last.get("px")
        kinds = [t["kind"] for t in c["txs"]]
        if "subscription" in kinds or "collateral" in kinds:
            dep = next((t for t in c["txs"] if t["kind"] in ("subscription", "collateral")), c["txs"][0])
            move, saw = "deposit, not price", f"{dep['amount']} deposit, no debt yet" if not last.get("hf_after") else f"{dep['amount']} deposit"
        elif "seed_borrow" in kinds:
            move, saw = "operator action", "no debt yet: the agent does not open a position from zero"
        elif c["switch"]:
            b_prev = band_hist[band_hist.index(c["switch"]) - 1]
            move = "re-lever switched on"
            saw = f"HF {hf_txt(c['txs'][0]['hf_before'])} above the {b_prev['borrow_trigger']:.2f} borrow trigger"
        else:
            first = c["txs"][0]
            bb = band_at(first["t"])
            saw = f"HF {hf_txt(first['hf_before'])} vs band {bb['repay_trigger']:.2f} to {bb['borrow_trigger']:.2f}"
            move = "price"
        resp = " ".join(f'<span class="badge {badge.get(k, badge["other"])[0]}">{badge.get(k, badge["other"])[1]}</span>' for k in dict.fromkeys(kinds))
        detail = "; ".join(brief(t) for t in c["txs"])
        if c["switch"]:
            detail = f"switched on {utc(c['switch']['t']).strftime('%H:%M:%S')}, borrowed {dur(last['t'] - c['switch']['t'])} later; " + detail
        rows.append({"win": short(c["t"]) if c["t1"] - c["t"] < 60 else f"{short(c['t'])} → {utc(c['t1']).strftime('%H:%M')}", "px": usd(p_after) if p_after else "n/a",
                     "move": move, "saw": saw, "resp": resp, "detail": detail, "debt": n(next((p["debt"] for p in points if p["b"] == last["block"]), None), 0) if any(p["b"] == last["block"] for p in points) else "n/a", "hf": hf_txt(last.get("hf_after"))})
        # The quiet stretch until the next event.
        a = c["t1"] + 1
        b_end = cs[i + 1]["t"] if i + 1 < len(cs) else max(now, points[-1]["t"] if points else now)
        seg = [p for p in points if a <= p["t"] < b_end and p.get("px") and not p.get("tag")]
        if len(seg) < 2:
            continue
        st = states_between(a, b_end)
        nruns = sum(st.values())
        main = max(st, key=st.get) if st else None
        hfs = [p["hf"] for p in seg if p.get("hf")]
        saw2 = (main or "no run data")
        if hfs and main == "inside the band, no action":
            low = min(seg, key=lambda p: p["hf"] or 9)
            saw2 = f"inside the band · min HF {low['hf']:.4f} ({short(low['t'])})"
        rows.append({"win": f"{short(seg[0]['t'])} → {short(seg[-1]['t'])}", "px": f"{seg[0]['px']:,.0f} → {seg[-1]['px']:,.0f}",
                     "move": pct((seg[-1]["px"] / seg[0]["px"] - 1) * 100, 1, sign=True), "saw": saw2,
                     "resp": '<span class="badge generic">no rebalance</span>',
                     "detail": f"{nruns} agent runs" + (f" · HF {min(hfs):.4f} to {max(hfs):.4f}" if hfs else " · no debt"),
                     "debt": n(seg[-1]["debt"], 0), "hf": hf_txt(seg[-1].get("hf"))})
    return rows


def narrative():
    """The story in one line per step: (when, what). Generated from the transactions and the run log."""
    cs = clusters()
    items = []
    if test_txs:
        hfm = [t for t in test_txs if t["actor"] == "Health-factor manager"]
        nr, nd = sum(t["kind"] == "relever" for t in hfm), sum(t["kind"] == "derisk" for t in hfm)
        nu = sum(t["kind"] == "unwind" for t in test_txs)
        items.append((f"{utc(test_txs[0]['t']).strftime('%-d %b')} → {utc(window_t0).strftime('%-d %b')}",
                       f"<b>Testing.</b> On a few dollars of KPK's own money the health-factor manager re-levered {nr} times and de-risked {nd} times, "
                       f"and the exit agent unwound the position {nu} times, ending with a full exit that cleared the test debt."))
    for i, c in enumerate(cs):
        kinds = [t["kind"] for t in c["txs"]]
        when = short(c["t"])
        if "subscription" in kinds:
            sub = next(t for t in c["txs"] if t["kind"] == "subscription")
            col = next((t for t in c["txs"] if t["kind"] == "collateral"), None)
            items.append((when, f"<b>Deposit.</b> {esc(sub['amount'])} subscribed" + (f" and supplied as collateral {dur(col['t'] - sub['t'])} later." if col else ".")))
        elif "seed_borrow" in kinds:
            t = next(t for t in c["txs"] if t["kind"] == "seed_borrow")
            items.append((when, f"<b>Seed borrow.</b> The {esc(t['actor'])} borrowed {esc(t['amount'])} and split it across both vaults (HF {hf_txt(t['hf_after'])}). "
                                "The agent manages a position but does not open one, so the first borrow is manual."))
        elif "relever" in kinds:
            t = next(t for t in c["txs"] if t["kind"] == "relever")
            lead = f"Switched on at {utc(c['switch']['t']).strftime('%H:%M:%S')}; {dur(t['t'] - c['switch']['t'])} later the agent " if c["switch"] else "The agent "
            items.append((when, f"<b>Re-lever.</b> {lead}borrowed {esc(t['amount'])} and landed HF at {hf_txt(t['hf_after'])} (target {t['target'] or band_at(t['t'])['borrow_target']:.2f})."))
        elif "derisk" in kinds:
            t = next(t for t in c["txs"] if t["kind"] == "derisk")
            items.append((when, f"<b>De-risk.</b> HF fell below {band_at(t['t'])['repay_trigger']:.2f}; the agent repaid {esc(t['amount'])} (HF {hf_txt(t['hf_after'])})."))
        elif "claim" in kinds:
            t = next(t for t in c["txs"] if t["kind"] == "claim")
            items.append((when, f"<b>Rewards claimed.</b> {esc(t['amount'])} moved into NAV in one step."))
        a = c["t1"] + 1
        b_end = cs[i + 1]["t"] if i + 1 < len(cs) else now
        st = states_between(a, b_end)
        total = sum(st.values())
        if total >= 3:
            main = max(st, key=st.get)
            if main == "above the borrow trigger, re-lever switched off":
                items.append((f"{short(a)} → {short(b_end)}", f"<b>Hold.</b> Re-lever was off on purpose: {total} checks, no action."))
            elif main == "inside the band, no action" and i + 1 == len(cs):
                low = f" Lowest HF {lowest['hf']:.4f}." if lowest else ""
                items.append(("Since then", f"<b>Inside the band.</b> {st.get(main, 0):,} of {total:,} checks needed no action.{low}"))
    if rewards:
        items.append(("Now", f"<b>Rewards.</b> {usd(rewards['total_usd'], 2)} unclaimed (≈ {n(rewards['total_wbtc'], 5)} WBTC), outside NAV until claimed."))
    return items


# --- Page pieces --------------------------------------------------------------------------------

as_of = stamp(chain["read_at"])
app_url = CONFIG["app_url"]
snap_label = f"agent activity as of {stamp(synced_at)}" if snap else "agent activity appears after the first sync"
nav_plus = chain["nav_wbtc"] + (rewards["total_wbtc"] or 0) if rewards else None
window_txs = [t for t in transactions if t["t"] >= window_t0]
by_agent = runs.get("by_agent", [])
runs_total = sum(a["runs"] for a in by_agent)
runs_ok = sum(a["completed"] for a in by_agent)
runs_fail = sum(a["failed"] for a in by_agent)
agent_txs = [t for t in window_txs if t["actor"] not in ("Manager Safe (2 of 5 signers)", "Other")]
manual_txs = [t for t in window_txs if t["actor"] == "Manager Safe (2 of 5 signers)"]
deposit_tx = subs[0] if subs else None
lever_time = (first_relever["t"] - deposit_tx["t"]) if first_relever and deposit_tx else None
first_borrow_carry = None


def counters():
    cells = []
    cells.append(("Agent runs", f"{runs_total:,}" if by_agent else "n/a", f"{runs_ok:,} completed · {runs_fail:,} failed" if by_agent else snap_label))
    cells.append(("Transactions", f"{len(window_txs)}", f"{len(agent_txs)} by agents · {len(manual_txs)} by the Manager Safe" + (f" · {len(test_txs)} more in testing" if test_txs else "")))
    cells.append(("Lowest HF", f"{lowest['hf']:.4f}" if lowest else "n/a",
                  f"{short(lowest['t'])}; the agent repays only below {lowest['trigger']:.2f}, liquidation is at 1.00" if lowest else "after the first re-lever"))
    cells.append(("Liquidation buffer", pct(liq_buffer, 1), f"BTC would have to fall this far; liquidation at {usd(chain['liq_px'])}" if chain["liq_px"] else "no debt"))
    cells.append(("Inside the band", f"{inside_n / band_total * 100:.0f}%" if band_total else "n/a",
                  f"{inside_n:,} of {band_total:,} checks since the re-lever needed no action" if band_total else "after the first re-lever"))
    return "".join(f'<div class="counter"><div class="c-label">{esc(a)}</div><div class="c-value">{esc(b)}</div><div class="c-sub">{esc(c)}</div></div>' for a, b, c in cells)


def tx_rows(txs):
    rows = []
    for t in reversed(txs):
        target = f'<br><span class="dim">target {t["target"]:.2f}</span>' if t.get("target") else ""
        hf = "no debt" if t["hf_before"] is None and t["hf_after"] is None else f"{hf_txt(t['hf_before'])} → <b>{hf_txt(t['hf_after'])}</b>{target}"
        rows.append(f'<tr><td class="mono nowrap">{utc(t["t"]).strftime("%-d %b %H:%M:%S")}</td><td class="mid">{esc(t["actor"])}</td><td class="wide">{esc(t["action"])}</td>'
                    f'<td class="mono num nowrap">{esc(t["amount"] or "")}</td><td class="mono nowrap">{hf}</td>'
                    f'<td class="mono nowrap"><a href="{ETHERSCAN}/tx/{esc(t["hash"])}" target="_blank" rel="noopener">{short_hash(t["hash"])} ↗</a></td></tr>')
    return (f'<div class="table-scroll"><table class="data"><thead><tr><th>UTC, {utc(window_t0).year}</th><th>Actor</th><th>Action</th><th class="num">Amount</th><th>HF before → after</th><th>Transaction</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def tx_table():
    if not window_txs:
        return f'<p class="empty">No transactions yet ({esc(snap_label)}).</p>'
    out = tx_rows(window_txs)
    if test_txs:
        out += (f'<details class="fold" style="margin-top:14px"><summary>Testing phase, {esc(utc(test_txs[0]["t"]).strftime("%-d %b"))} to {esc(utc(test_txs[-1]["t"]).strftime("%-d %b"))}: '
                f'{len(test_txs)} transactions on a few dollars of KPK\'s own money</summary>'
                '<p class="lede">The same agents, run on test positions of 0.0002 to 0.0035 WBTC while the fund was set up, including setup of the agents\' permissions. '
                'Test amounts were sometimes below the agents\' minimum transaction size, so a few steps waited for a parameter change.</p>'
                f'{tx_rows(test_txs)}</details>')
    return out


def legs_table():
    rows = leg_rows() if snap else []
    if not rows:
        return f'<p class="empty">{esc(snap_label.capitalize())}.</p>'
    body = "".join(f'<tr><td class="mid">{esc(r["win"])}</td><td class="num nowrap"><span class="mono">{esc(r["px"])}</span><br><span class="dim">{esc(r["move"])}</span></td>'
                   f'<td class="wide">{esc(r["saw"][:1].upper() + r["saw"][1:])}<br><span class="dim">{esc(r["detail"])}{(" · debt " + esc(r["debt"]) + " USDC") if r["debt"] not in ("n/a", "0") else ""}</span></td>'
                   f'<td>{r["resp"]}</td><td class="mono num nowrap">{esc(r["hf"])}</td></tr>' for r in rows)
    return ('<div class="table-scroll"><table class="data"><thead><tr><th>When (UTC)</th><th class="num">BTC price, move</th><th>What happened</th><th>Agent action</th><th class="num">HF after</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def scaling_rows():
    out = []
    for s in CONFIG["scaling"]["stages"]:
        cls = {"live": "positive", "in progress": "info", "planned": "generic"}.get(s["status"], "generic")
        ics = "".join(icon(i, 20) for i in s.get("icons", []))
        out.append(f'<tr><td class="nowrap"><b>{esc(s["stage"])}</b><div class="stage-icons">{ics}</div></td><td>{esc(s["adds"])}</td><td>{esc(s["needs"])}</td><td class="nowrap"><span class="badge {cls}">{esc(s["status"])}</span></td></tr>')
    bk = CONFIG["scaling"].get("backup")
    if bk:
        cls = {"live": "positive", "in progress": "info", "planned": "generic"}.get(bk["status"], "generic")
        ics = "".join(icon(i, 20) for i in bk.get("icons", []))
        out.append(f'<tr><td class="nowrap"><b>{esc(bk["label"])}</b><div class="stage-icons">{ics}</div></td><td colspan="2">{esc(bk["text"])}</td><td class="nowrap"><span class="badge {cls}">{esc(bk["status"])}</span></td></tr>')
    return "".join(out)


def capacity_line():
    cu, cg = chain["core_usdc"], chain["core_usdg"]
    past = cu["room_to_kink"] <= 0 and cu["utilisation"] is not None
    first = (f"USDC borrow room to the kink on the Core hub: <b>{musd(cu['room_to_kink']) if cu['room_to_kink'] > 0 else '$0'}</b>"
             + (f" (the hub runs at {cu['utilisation'] * 100:.1f}% used against a {cu['optimal_usage'] * 100:.0f}% kink, so new USDC borrowing pays a steeper rate)" if past else ""))
    parts = [first, f"USDG room under the Bluechip spoke's cap: <b>{musd(cg['bluechip_room_under_cap'])}</b>", f"Core hub USDG liquidity: <b>{musd(cg['liquidity'])}</b> at {pct(cg['borrow_apy'])}"]
    cap = (snap or {}).get("capacity") or {}
    extra = ""
    if cap.get("core_usdc_after_1m_apy") is not None:
        extra = (f" At the last sync ({stamp(cap['t'])}), borrowing another $1M on the Core hub would have cost {pct(cap['core_usdc_after_1m_apy'])} gross in USDC"
                 + (f" or {pct(cap['core_usdg_after_1m_apy'])} in USDG" if cap.get("core_usdg_after_1m_apy") is not None else "")
                 + (f"; $3.5M of USDG, about what a $5M fund borrows at today's LTV, {pct(cap['core_usdg_after_3_5m_apy'])}" if cap.get("core_usdg_after_3_5m_apy") is not None else "")
                 + ". Rates before Merkl.")
    return "; ".join(parts) + "." + extra


# Simulator inputs.
sim = {
    "cf": cf, "liveHf": hf_now or band["borrow_target"], "spread": spread if spread is not None else 0, "fee": PERF_FEE, "px": px,
    "minHf": MIN_HF, "lowHf": 1.05, "maxHf": 1.6, "target": band["borrow_target"], "repay": band["repay_trigger"], "borrow": band["borrow_trigger"],
}

loop_live = chain["maple_active"]
loop = CONFIG["loop_sleeve"]

stale_note = ""
if position_changed:
    stale_note = f'<div class="note">The position has changed since {esc(stamp(synced_at))}; detail follows at the next update.</div>'

rew_rows = ""
if rewards:
    rew_rows = "".join(f'<div class="kv"><span>{esc(g["group"])}</span><span class="mono">{usd(g["claimable_usd"], 2)} claimable · {usd(g["pending_usd"], 2)} pending</span></div>' for g in rewards["groups"])

if realised:
    realised_html = (f'<div class="stat-value small">{pct(realised["nav"], 2, sign=True)}</div><div class="stat-sub">from NAV, last {REALISED_DAYS:.0f} days, annualised in WBTC</div>'
                     + (f'<div class="kv"><span>including unclaimed rewards</span><span class="mono">≈ {pct(realised["all"], 2, sign=True)}</span></div>' if realised["all"] is not None else "")
                     + (f'<div class="kv"><span>USD carry P&amp;L, same {REALISED_DAYS:.0f} days</span><span class="mono">≈ {usd(realised["usd_pnl"])}</span></div>' if realised["usd_pnl"] is not None else "")
                     + '<div class="stat-sub">The carry accrues in USDC while the unit is BTC, so a BTC rally marks it down in BTC terms even as the USD figure climbs.</div>')
else:
    realised_html = (f'<div class="stat-sub">Realised carry is shown once {REALISED_DAYS:.0f} days have passed since the re-lever on {esc(utc(first_relever["t"]).strftime("%-d %b")) if first_relever else "its first day"}: '
                     'twice, from NAV and including unclaimed rewards, with the USD carry P&amp;L beside it.</div>')

def scope_cards():
    out = []
    for card in CONFIG["scope"]["cards"]:
        live = card["status"] == "live" or (card.get("live_if") == "maple" and chain["maple_active"])
        tag = "" if live else '<span class="pill">planned</span>'
        ic = (f'<span class="kpk-mark">{logo_svg}</span>' if card.get("icon") == "kpk" else icon(card.get("icon", ""), 26))
        out.append(f'<div class="scope-card{"" if live else " planned"}"><div class="scope-head"><div class="stat-label">{esc(card["label"])} {tag}</div>{ic}</div>'
                   f'<div class="scope-value">{esc(card["value"])}</div><div class="stat-sub">{esc(card["detail"])}</div></div>')
    return "".join(out)


deposit_chips = "".join(f'<span class="chip {"live" if a["status"] == "live" else "soon"}">{icon(a["asset"].lower(), 16)}{esc(a["asset"])}{" · coming" if a["status"] != "live" else ""}</span>' for a in CONFIG["deposit_assets"])

legs_vaults = "".join(
    f'<div class="kv"><span><a href="https://app.morpho.org/ethereum/vault/{esc(v["address"])}" target="_blank" rel="noopener">{esc(v["name"].replace("KPK USDC ", ""))} ↗</a> <span class="dim">{v["weight"] * 100:.0f}%</span></span>'
    f'<span class="mono">{pct(v["apy"]) if v["apy"] is not None else "APY n/a"}</span></div>' for v in vault_rows)

# --- CSS and JS (plain strings: no em dashes, tokens only) ----------------------------------------

tokens_css = re.sub(r"/\*[\s\S]*?\*/", "", (ASSETS / "kpk-tokens.css").read_text())
fonts_css = re.sub(r"/\*[\s\S]*?\*/", "", (ASSETS / "kpk-fonts.css").read_text())
dark_block = re.search(r':root\[data-theme="dark"\],\s*\.dark\s*\{([\s\S]*?)\}', tokens_css).group(1)
logo_svg = (ASSETS / "kpk-logo.svg").read_text()

CSS = """
:root {
  --c-line: #2E7FC1; --c-liq: #D2553A; --c-band: #2D8C6A;
  --c-band-fill: rgba(45, 140, 106, 0.12); --c-good: #2D8C6A; --c-bad: #D2553A; --c-info: #2E7FC1;
}
:root[data-theme="dark"] { --c-line: #3F93D4; --c-liq: #DD6447; --c-band: #35A578; --c-band-fill: rgba(53, 165, 120, 0.16); --c-good: #35A578; --c-bad: #DD6447; --c-info: #3F93D4; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { DARK_TOKENS --c-line: #3F93D4; --c-liq: #DD6447; --c-band: #35A578; --c-band-fill: rgba(53, 165, 120, 0.16); --c-good: #35A578; --c-bad: #DD6447; --c-info: #3F93D4; }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--kpk-surface); color: var(--kpk-ink); font-family: var(--kpk-font-sans); font-weight: 400; line-height: 1.5; -webkit-font-smoothing: antialiased; }
a { color: inherit; text-decoration-color: var(--kpk-ink-faint); text-underline-offset: 2px; }
.page { max-width: 1040px; margin: 0 auto; padding: var(--kpk-space-6) var(--kpk-space-4) var(--kpk-space-16); display: flex; flex-direction: column; gap: var(--kpk-space-8); }
header.top { display: flex; align-items: center; justify-content: space-between; gap: var(--kpk-space-4); flex-wrap: wrap; }
.logo { width: 84px; color: var(--kpk-ink); }
.logo svg { width: 100%; height: auto; display: block; }
.meta { display: flex; gap: var(--kpk-space-3); align-items: center; flex-wrap: wrap; font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); }
.asof.stale { color: var(--c-bad); font-weight: 500; }
.theme-btn { font: inherit; font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); background: none; border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-pill); padding: 2px 10px; cursor: pointer; }
.hero h1 { font-size: var(--kpk-text-display-h1); font-weight: 500; letter-spacing: -0.01em; margin: 0 0 var(--kpk-space-3); line-height: 1.1; }
.hero p { font-size: var(--kpk-text-body); max-width: 70ch; margin: 0 0 var(--kpk-space-4); }
.hero .row { display: flex; gap: var(--kpk-space-3); flex-wrap: wrap; align-items: center; }
.cta { display: inline-flex; align-items: center; gap: 8px; background: var(--kpk-ink); color: var(--kpk-surface); text-decoration: none; padding: 10px 18px; border-radius: var(--kpk-radius-pill); font-weight: 500; font-size: var(--kpk-text-body-small); }
.cta:hover { opacity: 0.9; }
.chip { display: inline-block; font-size: var(--kpk-text-fs-tag); letter-spacing: 0.04em; text-transform: uppercase; padding: 3px 9px; border-radius: var(--kpk-radius-tag); border: var(--kpk-rule-hard) solid var(--kpk-line-strong); }
.chip.soon { border-color: var(--kpk-line); color: var(--kpk-ink-muted); }
.chip { display: inline-flex; align-items: center; gap: 6px; }
.chip.soon .logo-icon { opacity: 0.6; }
.pill { display: inline-block; font-size: var(--kpk-text-fs-tag); letter-spacing: 0.04em; text-transform: uppercase; padding: 3px 9px; border-radius: var(--kpk-radius-tag); background: var(--kpk-pill); color: var(--kpk-ink); vertical-align: middle; }
section { display: flex; flex-direction: column; gap: var(--kpk-space-4); scroll-margin-top: var(--kpk-space-4); }
.label { align-self: flex-start; background: var(--kpk-label-bg); color: var(--kpk-label-ink); font-size: var(--kpk-text-label); font-weight: 500; letter-spacing: 0.08em; text-transform: uppercase; padding: 3px 9px; border-radius: var(--kpk-radius-tag); }
h2 { font-size: var(--kpk-text-fs-section); font-weight: 500; margin: 0; letter-spacing: -0.01em; line-height: 1.2; }
.lede { color: var(--kpk-ink-muted); font-size: var(--kpk-text-body-small); max-width: 80ch; margin: 0; }
.asof-note { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); }
.note { font-size: var(--kpk-text-body-small); border: var(--kpk-rule-hard) solid var(--c-bad); border-radius: var(--kpk-radius-label); padding: 10px 14px; }
.card { background: var(--kpk-surface-card); border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-card); padding: var(--kpk-space-6); min-width: 0; }
.grid { display: grid; gap: var(--kpk-space-4); }
.g3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.g2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
@media (max-width: 760px) { .g3, .g2 { grid-template-columns: 1fr; } }
.stat-label { font-size: var(--kpk-text-label); letter-spacing: 0.08em; text-transform: uppercase; color: var(--kpk-ink-muted); font-weight: 500; }
.stat-value { font-size: var(--kpk-text-fs-stat); font-weight: 500; letter-spacing: -0.02em; line-height: 1.1; margin: 6px 0 4px; font-variant-numeric: tabular-nums; }
.stat-value.small { font-size: 28px; }
.stat-value.good { color: var(--c-good); }
.muted { color: var(--kpk-ink-muted); }
.stat-sub { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); margin-top: 4px; }
.kv { display: flex; justify-content: space-between; gap: 12px; font-size: var(--kpk-text-body-small); padding: 6px 0; border-top: var(--kpk-rule-soft) solid var(--kpk-line); flex-wrap: wrap; }
.kv:first-of-type { margin-top: 8px; }
.kv > span:last-child { margin-left: auto; text-align: right; }
.mono { font-family: var(--kpk-font-mono); font-size: var(--kpk-text-data-mono); }
.math { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; font-size: var(--kpk-text-body-small); }
.math .chunk { display: flex; flex-direction: column; align-items: center; }
.math .term { font-weight: 500; font-family: var(--kpk-font-mono); font-size: 16px; }
.math .sub { font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); }
.math .op { color: var(--kpk-ink-muted); }
.flow { display: grid; grid-template-columns: 1fr auto 1fr auto 1fr; gap: 12px; align-items: stretch; }
.flow .arrow { align-self: center; color: var(--kpk-ink-muted); font-size: 20px; }
@media (max-width: 760px) { .flow { grid-template-columns: 1fr; } .flow .arrow { transform: rotate(90deg); justify-self: center; } }
.leg-kind { font-size: var(--kpk-text-label); letter-spacing: 0.08em; text-transform: uppercase; font-weight: 500; color: var(--kpk-ink-muted); }
.leg-amount { font-size: 22px; font-weight: 500; margin-top: 6px; font-variant-numeric: tabular-nums; }
.leg-sub { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); }
.loop { border-style: dashed; }
.risk-list { display: flex; flex-direction: column; }
.risk-row { display: grid; grid-template-columns: 160px 1fr 1.3fr; gap: 14px; padding: 10px 0; border-top: var(--kpk-rule-soft) solid var(--kpk-line); font-size: var(--kpk-text-body-small); }
.risk-row:first-child { border-top: 0; padding-top: 0; }
.risk-n { font-weight: 500; }
.risk-h { color: var(--kpk-ink-muted); padding-left: 10px; border-left: 2px solid var(--c-band); }
.risk-row.muted-row .risk-h { border-left-color: var(--kpk-line); }
@media (max-width: 760px) { .risk-row { grid-template-columns: 1fr; gap: 4px; } }
.redeem { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.redeem-tile { border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-label); padding: 12px 14px; }
.redeem-tile.strong { border-color: var(--c-good); background: var(--c-band-fill); }
.redeem-v { font-size: 22px; font-weight: 500; }
.redeem-tile.strong .redeem-v { color: var(--c-good); }
.redeem-k { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); margin-top: 4px; }
.cta.ghost { background: transparent; color: var(--kpk-ink); border: var(--kpk-rule-hard) solid var(--kpk-line-strong); }
.doc-links { display: flex; gap: 10px; flex-wrap: wrap; }
.doc-links a { font-size: var(--kpk-text-fs-meta); padding: 4px 10px; border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-pill); text-decoration: none; }
.doc-links a:hover { border-color: var(--kpk-line-strong); }
.sub-h { font-size: var(--kpk-text-label); letter-spacing: 0.08em; text-transform: uppercase; color: var(--kpk-ink-muted); font-weight: 500; margin: 6px 0 -6px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.pill.live-pill { background: var(--c-band-fill); color: var(--c-good); }
.chip-s { font-size: var(--kpk-text-fs-tag); padding: 3px 8px; border-radius: var(--kpk-radius-pill); background: var(--kpk-pill); color: var(--kpk-ink); line-height: 1.4; }
.watch-head { display: flex; gap: 10px; align-items: flex-start; }
.wakes { margin-top: 12px; padding-top: 10px; border-top: var(--kpk-rule-soft) solid var(--kpk-line); }
.wakes-t { font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
.wake { font-size: var(--kpk-text-fs-meta); padding: 4px 0 4px 10px; border-left: 2px solid var(--c-band); margin-bottom: 4px; }
.layer-tag { display: inline-block; font-size: var(--kpk-text-fs-tag); letter-spacing: 0.05em; text-transform: uppercase; padding: 3px 9px; border-radius: var(--kpk-radius-tag); }
.layer-tag.auto { background: var(--c-band-fill); color: var(--c-good); border: var(--kpk-rule-hard) solid var(--c-good); }
.layer-tag.human { background: var(--kpk-pill); color: var(--kpk-ink); }
.layer-t { font-size: 18px; font-weight: 500; margin: 10px 0 0; }
.example { font-size: var(--kpk-text-fs-meta); background: var(--kpk-surface); border-radius: var(--kpk-radius-label); padding: 10px 12px; margin-top: 10px; line-height: 1.5; }
.example b { font-weight: 500; }
details.agent > summary { cursor: pointer; list-style: none; display: flex; flex-direction: column; gap: 4px; }
details.agent > summary::-webkit-details-marker { display: none; }
details.agent > summary::after { content: "Show more"; font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); text-decoration: underline; margin-top: 4px; }
details.agent[open] > summary::after { content: "Show less"; }
.agent-s { font-size: 15px; font-weight: 500; }
.riskflow { display: grid; grid-template-columns: 1fr auto 1fr auto 1fr auto 1fr; gap: 10px; align-items: stretch; }
.rf-step { background: var(--kpk-surface-card); border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-card); padding: 14px 16px; }
.rf-n { width: 22px; height: 22px; border-radius: 50%; background: var(--kpk-ink); color: var(--kpk-surface); font-size: 12px; font-weight: 500; display: flex; align-items: center; justify-content: center; }
.rf-t { font-weight: 500; margin: 8px 0 4px; }
.rf-b { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); line-height: 1.5; }
.rf-arrow { align-self: center; color: var(--kpk-ink-muted); font-size: 20px; }
@media (max-width: 860px) { .riskflow { grid-template-columns: 1fr; } .rf-arrow { transform: rotate(90deg); justify-self: center; } }
.agent-b { font-size: var(--kpk-text-body-small); margin: 8px 0 0; line-height: 1.55; }
.stage-icons { display: flex; gap: 4px; margin-top: 6px; }
.leg { display: flex; flex-direction: column; }
.leg-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.logo-icon { border-radius: 50%; flex: none; display: inline-block; vertical-align: middle; }
.leg .leg-amount { margin: 4px 0 6px; }
.leg .kv:first-of-type { margin-top: 0; }
.scope { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: var(--kpk-space-6); }
@media (max-width: 760px) { .scope { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.scope-card { background: var(--kpk-surface-card); border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-card); padding: 12px 14px; min-width: 0; }
.scope-card.planned { border-style: dashed; background: transparent; }
.scope-card.planned .scope-value, .scope-card.planned .stat-sub { color: var(--kpk-ink-faint); }
.scope-value { font-size: 17px; font-weight: 500; margin: 4px 0 2px; }
.scope-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.scope-card.planned .logo-icon { opacity: 0.55; }
.kpk-mark { width: 44px; color: var(--kpk-ink); flex: none; }
.kpk-mark svg { width: 100%; height: auto; display: block; }
.counters { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 900px) { .counters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.counter { background: var(--kpk-surface-card); border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-card); padding: 14px 16px; min-width: 0; }
.c-label { font-size: var(--kpk-text-label); letter-spacing: 0.08em; text-transform: uppercase; color: var(--kpk-ink-muted); font-weight: 500; }
.c-value { font-size: 26px; font-weight: 500; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.c-sub { font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); line-height: 1.4; }
.chart { position: relative; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; touch-action: pan-y; }
.chart .grid { stroke: var(--kpk-line); stroke-width: 1; }
.chart .tick { fill: var(--kpk-ink-muted); font-size: 12px; font-family: var(--kpk-font-sans); }
.chart .lab { fill: var(--kpk-ink-muted); font-size: 12px; font-family: var(--kpk-font-sans); }
.chart .line { fill: none; stroke: var(--c-line); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .liq { fill: none; stroke: var(--c-liq); stroke-width: 1.5; stroke-dasharray: 6 4; }
.chart .liq-lab { fill: var(--c-liq); }
.chart .band { fill: var(--c-band-fill); stroke: none; }
.chart .band.open { opacity: 0.5; }
.chart .band-edge { fill: none; stroke: var(--c-band); stroke-width: 1; stroke-dasharray: 3 3; }
.chart .band-target { stroke: var(--c-band); stroke-width: 1.5; }
.chart .band-lab { fill: var(--c-band); }
.chart .event, .chart .sync { stroke: var(--kpk-ink-faint); stroke-width: 1; stroke-dasharray: 2 3; }
.chart .mk path, .chart .mk circle { stroke: var(--kpk-surface-card); stroke-width: 2; }
.chart .mk.relever path { fill: var(--c-band); }
.chart .mk.derisk path { fill: var(--c-liq); }
.chart .mk.manual path { fill: var(--kpk-ink); }
.chart .mk.unwind path { fill: var(--kpk-ink-muted); }
.chart .mk.deposit circle { fill: var(--kpk-surface-card); stroke: var(--kpk-ink); }
.chart .xhair { stroke: var(--kpk-ink-muted); stroke-width: 1; }
.chart .phase { fill: var(--kpk-ink); opacity: 0.05; }
.chart .ev-num { fill: var(--kpk-ink); font-weight: 500; }
.chart .ev-v { fill: var(--kpk-ink); font-size: 11px; paint-order: stroke; stroke: var(--kpk-surface-card); stroke-width: 3px; stroke-linejoin: round; }
.timeline.events { margin-top: 8px; font-size: var(--kpk-text-fs-meta); gap: 4px; }
.timeline.events li { grid-template-columns: 130px 1fr; }
@media (max-width: 640px) {
  .chart { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .chart svg { min-width: 720px; }
  .chart .tick, .chart .lab { font-size: 17px; }
}
.chart .hit { fill: transparent; }
.chart .tip { position: absolute; top: 0; pointer-events: none; visibility: hidden; background: var(--kpk-sheet); border: var(--kpk-rule-hard) solid var(--kpk-line-strong); border-radius: var(--kpk-radius-label); padding: 8px 10px; font-size: 12px; white-space: nowrap; transform: translate(-50%, 0); z-index: 3; }
.chart .tip div:first-child { color: var(--kpk-ink-muted); }
.legend { display: flex; gap: 18px; flex-wrap: wrap; font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); align-items: center; }
.legend i { display: inline-block; width: 16px; height: 0; border-top: 2px solid var(--c-line); vertical-align: middle; margin-right: 6px; }
.legend i.liq { border-top: 1.5px dashed var(--c-liq); }
.legend i.band { height: 10px; border: 1px dashed var(--c-band); background: var(--c-band-fill); }
.legend b { font-weight: 500; color: var(--kpk-ink); }
.legend .shape { display: inline-block; margin-right: 6px; font-size: 13px; }
.statrow { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 760px) { .statrow { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.data { border-collapse: collapse; width: 100%; font-size: var(--kpk-text-body-small); }
table.data th, table.data td { text-align: left; padding: 8px 10px; border-bottom: var(--kpk-rule-soft) solid var(--kpk-line); vertical-align: top; }
table.data th { font-size: var(--kpk-text-label); letter-spacing: 0.06em; text-transform: uppercase; color: var(--kpk-ink-muted); font-weight: 500; }
table.data .num { text-align: right; }
table.data td.wide { min-width: 210px; }
table.data td.mid { min-width: 130px; }
.nowrap { white-space: nowrap; }
.dim { color: var(--kpk-ink-muted); }
.badge { display: inline-block; font-size: var(--kpk-text-fs-tag); padding: 2px 8px; border-radius: var(--kpk-radius-tag); border: var(--kpk-rule-hard) solid var(--kpk-line); white-space: nowrap; }
.badge.positive { border-color: var(--c-good); color: var(--c-good); }
.badge.negative { border-color: var(--c-bad); color: var(--c-bad); }
.badge.info { border-color: var(--c-info); color: var(--c-info); }
.notes { margin: 0; padding-left: 20px; display: flex; flex-direction: column; gap: 10px; font-size: var(--kpk-text-body-small); max-width: 90ch; }
.notes b { font-weight: 500; }
.empty { color: var(--kpk-ink-muted); font-size: var(--kpk-text-body-small); }
.sim-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.sim-out { display: flex; align-items: baseline; gap: 10px; }
.sim-out .big { font-size: 36px; font-weight: 500; color: var(--c-good); font-variant-numeric: tabular-nums; }
.slider-wrap { margin-top: 18px; }
.spread-picker { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
.picker-label { font-size: var(--kpk-text-fs-meta); color: var(--kpk-ink-muted); margin-right: 2px; }
.spread-btn { display: flex; flex-direction: column; align-items: flex-start; gap: 1px; padding: 6px 11px; border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-label); background: var(--kpk-surface); color: var(--kpk-ink-muted); font: inherit; cursor: pointer; text-align: left; }
.spread-btn i { font-style: normal; font-size: var(--kpk-text-fs-tag); letter-spacing: 0.03em; }
.spread-btn b { font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; }
.spread-btn[aria-current="true"] { border-color: var(--c-good); color: var(--kpk-ink); }
.spread-btn[aria-current="true"] b { color: var(--c-good); }
.picker-note { flex: 1 1 100%; font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); }
.headline { display: grid; grid-template-columns: 1.3fr 1fr; gap: var(--kpk-space-4); align-items: start; }
@media (max-width: 760px) { .headline { grid-template-columns: 1fr; } }
.hero-value { font-size: var(--kpk-text-slide-stat); margin: 10px 0 8px; }
.stat-note { font-size: var(--kpk-text-body-small); color: var(--kpk-ink-muted); max-width: 52ch; }
.stat-note b { color: var(--kpk-ink); font-weight: 500; }
.side-stats { display: flex; flex-direction: column; background: var(--kpk-surface-card); border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-card); padding: 4px 18px; }
.mini { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 9px 0; }
.mini + .mini { border-top: var(--kpk-rule-soft) solid var(--kpk-line); }
.m-label { font-size: var(--kpk-text-body-small); color: var(--kpk-ink-muted); font-weight: 500; }
.m-label em { display: block; font-style: normal; font-weight: 400; font-size: var(--kpk-text-fs-tag); margin-top: 2px; }
.m-value { font-size: 20px; font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
.loop-proj { margin-top: 14px; padding-top: 12px; border-top: var(--kpk-rule-soft) solid var(--kpk-line); display: flex; flex-direction: column; gap: 8px; }
table.data.compact { max-width: 460px; }
details.fold > summary { cursor: pointer; font-size: var(--kpk-text-body-small); font-weight: 500; }
details.fold[open] > summary { margin-bottom: 10px; }
.muted-row { color: var(--kpk-ink-faint); }
.timeline { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 8px; font-size: var(--kpk-text-body-small); }
.timeline li { display: grid; grid-template-columns: 150px 1fr; gap: 12px; }
@media (max-width: 640px) { .timeline li { grid-template-columns: 1fr; gap: 2px; } }
.timeline .when { color: var(--kpk-ink-muted); font-family: var(--kpk-font-mono); font-size: var(--kpk-text-data-mono); }
.timeline b { font-weight: 500; }
.ends { display: flex; justify-content: space-between; font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); margin-bottom: 6px; }
.ends .hi { color: var(--c-bad); }
input[type=range] { width: 100%; -webkit-appearance: none; appearance: none; height: 6px; border-radius: 4px; outline: none; margin: 0;
  background: linear-gradient(to right, var(--c-good) 0%, var(--c-good) 62%, var(--kpk-orange-500) 80%, var(--c-bad) 88%, var(--c-bad) 100%); }
input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 22px; height: 22px; border-radius: 50%; background: var(--kpk-ink); border: 3px solid var(--kpk-surface-card); cursor: pointer; }
input[type=range]::-moz-range-thumb { width: 22px; height: 22px; border-radius: 50%; background: var(--kpk-ink); border: 3px solid var(--kpk-surface-card); cursor: pointer; }
.marks { position: relative; height: 66px; margin-top: 4px; }
.band-seg { position: absolute; top: 0; height: 8px; background: var(--c-band-fill); border: var(--kpk-rule-hard) dashed var(--c-band); border-radius: 4px; }
.band-seg span { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); font-size: var(--kpk-text-fs-tag); color: var(--c-band); white-space: nowrap; }
.marks button { position: absolute; transform: translateX(-50%); background: none; border: 0; padding: 2px 4px; font: inherit; font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); cursor: pointer; white-space: nowrap; }
.marks button::before { content: ""; display: block; width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid var(--kpk-ink-muted); margin: 0 auto 3px; }
.marks button.on { color: var(--kpk-ink); font-weight: 500; }
.marks button.on::before { background: var(--kpk-ink); border-color: var(--kpk-ink); }
.sim-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
@media (max-width: 760px) { .sim-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.sim-cell { border: var(--kpk-rule-hard) solid var(--kpk-line); border-radius: var(--kpk-radius-label); padding: 10px 12px; }
.sim-cell .k { font-size: var(--kpk-text-fs-tag); color: var(--kpk-ink-muted); text-transform: uppercase; letter-spacing: 0.04em; }
.sim-cell .v { font-size: 17px; font-weight: 500; margin-top: 2px; font-variant-numeric: tabular-nums; }
footer { border-top: var(--kpk-rule-hard) solid var(--kpk-line-strong); padding-top: var(--kpk-space-4); font-size: var(--kpk-text-fs-footer); color: var(--kpk-ink-muted); display: flex; flex-direction: column; gap: 8px; }
footer p { margin: 0; max-width: 100ch; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--c-info); outline-offset: 2px; }
""".replace("DARK_TOKENS", dark_block)

JS = """
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem('btc-carry-theme'); if (saved) root.setAttribute('data-theme', saved); } catch (e) {}
  var btn = document.getElementById('themeBtn');
  function label() { var t = root.getAttribute('data-theme'); btn.textContent = t === 'dark' ? 'Dark' : t === 'light' ? 'Light' : 'Auto'; }
  if (btn) {
    label();
    btn.addEventListener('click', function () {
      var t = root.getAttribute('data-theme'), next = t === 'light' ? 'dark' : t === 'dark' ? null : 'light';
      if (next) root.setAttribute('data-theme', next); else root.removeAttribute('data-theme');
      try { if (next) localStorage.setItem('btc-carry-theme', next); else localStorage.removeItem('btc-carry-theme'); } catch (e) {}
      label();
    });
  }
  var el = document.getElementById('asOf');
  if (el) {
    var built = parseInt(el.getAttribute('data-built'), 10), mins = Math.floor((Date.now() / 1000 - built) / 60);
    if (mins >= 0) {
      el.textContent += ' · ' + (mins < 60 ? mins + ' min ago' : mins < 1440 ? Math.floor(mins / 60) + ' h ago' : Math.floor(mins / 1440) + ' days ago');
      if (mins > 300) el.classList.add('stale');
    }
  }
  [].forEach.call(document.querySelectorAll('.chart'), function (box) {
    var svg = box.querySelector('svg'), tip = box.querySelector('.tip'), xh = svg && svg.querySelector('.xhair');
    if (!svg || !tip) return;
    var pts = JSON.parse(svg.getAttribute('data-hover') || '[]'), vbw = svg.viewBox.baseVal.width;
    function show(ev) {
      if (!pts.length) return;
      var r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width * vbw, best = pts[0];
      for (var i = 1; i < pts.length; i++) if (Math.abs(pts[i].x - x) < Math.abs(best.x - x)) best = pts[i];
      xh.setAttribute('x1', best.x); xh.setAttribute('x2', best.x); xh.setAttribute('visibility', 'visible');
      tip.innerHTML = '';
      best.l.forEach(function (line) { var d = document.createElement('div'); d.textContent = line; tip.appendChild(d); });
      var left = best.x / vbw * r.width, w = tip.offsetWidth;
      tip.style.left = Math.max(w / 2, Math.min(r.width - w / 2, left)) + 'px';
      tip.style.visibility = 'visible';
    }
    function hide() { tip.style.visibility = 'hidden'; xh.setAttribute('visibility', 'hidden'); }
    svg.addEventListener('pointermove', show); svg.addEventListener('pointerdown', show); svg.addEventListener('pointerleave', hide);
  });
  var S = window.SIM, slider = document.getElementById('hfSlider');
  if (!S || !slider) return;
  function hfOf(v) { return S.maxHf - v / 100; }
  function valOf(hf) { return Math.round((S.maxHf - hf) * 100); }
  slider.min = 0; slider.max = valOf(S.lowHf); slider.value = valOf(Math.min(S.maxHf, Math.max(S.lowHf, S.liveHf)));
  // The band is one shaded segment on the track, so only live, target and the minimum need marks.
  var box = document.getElementById('hfMarks'), els = [];
  function pos(hf) { var f = valOf(hf) / slider.max; return 'calc(' + (f * 100) + '% + ' + ((0.5 - f) * 22) + 'px)'; }
  var seg = document.createElement('div'); seg.className = 'band-seg';
  var fa = valOf(S.borrow) / slider.max, fb = valOf(S.repay) / slider.max;
  seg.style.left = pos(S.borrow);
  seg.style.width = 'calc(' + ((fb - fa) * 100) + '% - ' + ((fb - fa) * 22) + 'px)';
  seg.innerHTML = '<span>band ' + S.repay.toFixed(2) + ' to ' + S.borrow.toFixed(2) + '</span>';
  box.appendChild(seg);
  var sameAsTarget = Math.abs(S.liveHf - S.target) < 0.006;
  var marks = [
    { hf: S.target, label: sameAsTarget ? 'target ' + S.target.toFixed(2) + ' = live' : 'target ' + S.target.toFixed(2), row: 1 },
    { hf: S.minHf, label: 'minimum ' + S.minHf.toFixed(2), row: 1 }
  ];
  if (!sameAsTarget) marks.push({ hf: S.liveHf, label: 'live ' + S.liveHf.toFixed(3), row: 2 });
  marks.forEach(function (m) {
    var b = document.createElement('button'); b.type = 'button'; b.textContent = m.label;
    b.style.left = pos(m.hf);
    b.style.top = (8 + m.row * 22) + 'px';
    b.addEventListener('click', function () { slider.value = valOf(m.hf); render(); });
    box.appendChild(b); els.push({ el: b, hf: m.hf });
  });
  function fmt(x, dp) { return x.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp }); }
  function render() {
    var hf = hfOf(parseFloat(slider.value)), ltv = S.cf / hf, carry = ltv * S.spread * (1 - S.fee);
    document.getElementById('simHf').textContent = hf.toFixed(2);
    document.getElementById('simCarry').textContent = (carry >= 0 ? '+' : '') + carry.toFixed(2) + '%';
    document.getElementById('simLtv').textContent = (ltv * 100).toFixed(1) + '%';
    document.getElementById('simBorrow').textContent = fmt(S.px * ltv, 0) + ' USDC';
    document.getElementById('simUsd').textContent = '$' + fmt(S.px * ltv * S.spread / 100 * (1 - S.fee), 0) + ' a year';
    document.getElementById('simDrop').textContent = ((1 - 1 / hf) * 100).toFixed(1) + '%';
    document.getElementById('simWarn').hidden = hf >= S.minHf - 0.0001;
    els.forEach(function (m) { m.el.classList.toggle('on', Math.abs(m.hf - hf) < 0.006); });
  }
  // Spread picker: switching basis re-runs the simulation at the current HF.
  var btns = [].slice.call(document.querySelectorAll('.spread-btn'));
  btns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      S.spread = parseFloat(btn.getAttribute('data-spread'));
      btns.forEach(function (b) { b.setAttribute('aria-current', b === btn ? 'true' : 'false'); });
      render();
    });
  });
  slider.addEventListener('input', render);
  render();
})();
"""

# --- The page ---------------------------------------------------------------------------------------

rebate_txt = f"{pct(rebate_pct)}" if rebate else "not read at this build"
carry_txt = f"{carry:+.2f}%" if carry is not None else "n/a"
net_borrow_txt = pct(borrow_net)
math_strip = (f'<div class="math"><div class="chunk"><span class="term">{pct(deploy_apy)}</span><span class="sub">vault yield, 24h avg</span></div><span class="op">−</span>'
              f'<div class="chunk"><span class="term">{net_borrow_txt}</span><span class="sub">borrow, net of rebate</span></div><span class="op">=</span>'
              f'<div class="chunk"><span class="term">{n(spread)} pp</span><span class="sub">spread</span></div><span class="op">×</span>'
              f'<div class="chunk"><span class="term">{ltv * 100:.1f}%</span><span class="sub">LTV</span></div><span class="op">×</span>'
              f'<div class="chunk"><span class="term">0.9</span><span class="sub">after 10% fee</span></div><span class="op">=</span>'
              f'<div class="chunk"><span class="term" style="color:var(--c-good)">{carry_txt}</span><span class="sub">carry on BTC</span></div></div>') if spread is not None else ""

terms_band = f"repay below {band['repay_trigger']:.2f} back to {band['repay_target']:.2f}; re-lever above {band['borrow_trigger']:.2f} back to {band['borrow_target']:.2f}"
drop_from_repay = (1 - 1 / band["repay_trigger"]) * 100
drop_from_min = (1 - 1 / MIN_HF) * 100


hf_caption = ""
if seed and first_relever:
    hf_caption = (f"HF started high on purpose. The first borrow is manual and was sized below the target ({seed['amount']}, HF {hf_txt(seed['hf_after'])}); "
                  f"re-lever stayed off until {short(relever_switch['t'] if relever_switch else first_relever['t'])}, when the agent levered to the "
                  f"{first_relever['target'] or band['borrow_target']:.2f} target in one step. Since then the line sits inside the band.")



def px_near(ts, back):
    """The BTC price reading nearest to `back` seconds before `ts`."""
    target = ts - back
    cand = [p for p in points if p.get("px") and abs(p["t"] - target) <= 3 * 3600]
    return min(cand, key=lambda p: abs(p["t"] - target))["px"] if cand else None


def band_changed_near(ts):
    return any(0 <= ts - b["t"] <= 900 for b in band_hist)


def risk_evidence():
    """What the agents have actually done on this fund, picked from the transactions, newest example of each kind."""
    out = []
    ex = {e["hash"].lower(): e for e in runs.get("executed", [])}
    secs = lambda t: (ex.get(t["hash"].lower()) or {}).get("secs")
    if first_relever:
        s = secs(first_relever)
        lead = (f"re-lever was switched on at {utc(relever_switch['t']).strftime('%H:%M:%S')} and the agent's borrow confirmed {dur(first_relever['t'] - relever_switch['t'])} later"
                if relever_switch else "the agent re-levered")
        out.append(f"<b>{esc(short(first_relever['t']))}, live:</b> {lead}" + (f", {s:.0f} s from reading the chain to confirmation" if s else "")
                   + f". HF {hf_txt(first_relever['hf_before'])} → {hf_txt(first_relever['hf_after'])}.")
    for t in reversed(transactions):
        if t["kind"] == "relever" and (t.get("hf_before") or 0) > 3:
            dep = [d for d in transactions if d["kind"] in ("subscription", "collateral") and 0 <= t["t"] - d["t"] <= 600]
            if dep:
                phase = "testing" if t["t"] < window_t0 else "live"
                out.append(f"<b>{esc(short(t['t']))}, {phase}:</b> a deposit landed and the agent re-levered {dur(t['t'] - dep[0]['t'])} later, taking HF from {hf_txt(t['hf_before'])} back to {hf_txt(t['hf_after'])}.")
                break
    for kind, word in (("derisk", "repaid from the liquid sleeve"), ("relever", "borrowed and deployed")):
        cands = [t for t in transactions if t["kind"] == kind and t["actor"] == "Health-factor manager" and not band_changed_near(t["t"])
                 and (t.get("hf_before") or 0) < 3 and t is not first_relever]
        if not cands:
            continue
        t = cands[-1]
        b = band_at(t["t"])
        p0, p1 = px_near(t["t"], DAY), t.get("px")
        move = ""
        if p0 and p1:
            move = f"BTC {'fell' if p1 < p0 else 'rose'} {abs(p1 / p0 - 1) * 100:.1f}% in a day and "
        trig = f"below the {b['repay_trigger']:.2f} repay trigger" if kind == "derisk" else f"above the {b['borrow_trigger']:.2f} borrow trigger"
        phase = "testing" if t["t"] < window_t0 else "live"
        out.append(f"<b>{esc(short(t['t']))}, {phase}:</b> {move}HF moved to {hf_txt(t['hf_before'])}, {trig}; the agent {word} and brought it back to {hf_txt(t['hf_after'])}.")
    u = [t for t in transactions if t["kind"] == "unwind"]
    if u:
        full = [t for t in u if t["hf_after"] is None]
        ss = [s for s in (secs(t) for t in u) if s]
        out.append(f"<b>Exit agent:</b> {len(u)} unwinds in testing" + (f", ending with a full exit on {esc(short(full[-1]['t']))} that repaid all the test debt" if full else "")
                   + (f"; each ran in {min(ss):.0f} to {max(ss):.0f} s from reading the chain to confirmation." if ss else "."))
    return out


RC = CONFIG["risk_controls"]
PERMISSIONS_CARD = """  <div class="card">
    <div class="stat-label">Onchain permissions</div>
    <ul class="notes" style="margin-top:8px">
      <li><b>No key can move the fund's assets directly.</b> The Safe that holds the position has a single owner, a contract that holds no key. Every action reaches it through a Zodiac Roles permissions layer.</li>
      <li><b>Only whitelisted actions.</b> That layer allows a fixed set of calls on approved contracts, such as borrowing, repaying and supplying on the named Aave V4 spoke and depositing into the two KPK USDC vaults. Anything outside that set is rejected onchain. Each agent gets a narrower role inside that set, and so does the Manager Safe (2 of 5 signers).</li>
      <li><b>Changes are slow and visible.</b> The outer set of permissions is owned by a timelock: KPK's Security Council proposes a change, and it waits 3 days onchain before it can take effect.</li>
    </ul>
  </div>
"""


def chips(items):
    return "".join(f'<span class="chip-s">{esc(x)}</span>' for x in items)


DOCS = CONFIG["docs"]


def risk_section():
    steps = [
        ("Watch", "Onchain monitoring around the clock: the position, the oracles, and every venue the money sits in."),
        ("Trigger", f"Rules set in advance fire the action. Inside the band, {band['repay_trigger']:.2f} to {band['borrow_trigger']:.2f}, nothing happens. Outside it, HF goes back to {band['borrow_target']:.2f}. Past the hard limits, the position unwinds."),
        ("Act", "The agent sends the transaction itself, typically within one or two blocks."),
        ("Bounded", "Every call passes the permissions layer: whitelisted actions only, and any change to them sits behind a 3-day timelock."),
    ]
    flow = '<span class="rf-arrow" aria-hidden="true">→</span>'.join(
        f'<div class="rf-step"><div class="rf-n">{i}</div><div class="rf-t">{esc(a)}</div><div class="rf-b">{esc(b)}</div></div>' for i, (a, b) in enumerate(steps, start=1))
    watch = [
        ("The fund's position", "aave", "Checked by the health-factor manager on every run", [
            "health factor", "debt and collateral", "Core hub free liquidity", "the spoke's borrow cap", "reserve paused, frozen or not borrowable",
            "hub active or halted", "withdrawable liquidity in the liquid sleeve"],
         [f"HF outside {band['repay_trigger']:.2f} to {band['borrow_trigger']:.2f}: wakes the health-factor manager",
          f"HF below {RC['hf_exit_below']:.2f}: full unwind by the exit agent",
          f"borrowing costs more than the vaults earn for {RC['spread_exit']}: full unwind"]),
        ("Collateral and accounting", "ethereum", "Watched on the BTC leg and in the fund's own contracts", [
            "BTC price from Chainlink", "every price in the NAV against independent monitor feeds", "stablecoin pegs", "the age of each NAV read"],
         ["a stale or irregular price anywhere in the book: subscriptions and redemptions pause until it clears",
          "a NAV that disagrees with an independent cross-check: settlement pauses",
          "a settlement price more than 30% off the last one: rejected onchain",
          f"a redemption that would take HF below {RC['redemption_min_hf']:.2f}: the settler withdraws no collateral"]),
        ("The KPK USDC vaults", "morpho", "Watched in the vaults themselves and in every market they lend into", [
            "oracle liveness, staleness and sudden drops", "bad debt", "vault liquidity, at two severities", "pegs and reserves",
            "Morpho governance events", "manual allocator activity"],
         ["exploit, pause or Safe-compromise patterns at a collateral issuer, or at a venue it deploys into: that issuer's markets are exited",
          "an ownership change on an oracle feed behind a market: that market is exited",
          "a large deposit or withdrawal: an immediate rebalance, so liquidity is rebuilt or new money deployed",
          "forceDeallocate griefing: the penalty goes up and allocations are restored"]),
    ]
    watch_html = "".join(
        f'<div class="card watch"><div class="watch-head">{icon(ic, 24)}<div><div class="stat-label">{esc(title)}</div><div class="stat-sub">{esc(sub)}</div></div></div>'
        + (f'<div class="wakes-t" style="margin-top:12px">{"Reviewed by the risk team" if title == "The KPK USDC vaults" else "Checked"}</div>' if wakes and title == "The KPK USDC vaults" else "")
        + f'<div class="chips">{chips(items)}</div>'
        + (f'<div class="wakes"><div class="wakes-t">Triggers an automated action, no human approval needed</div>{"".join(f"<div class=wake>{esc(w)}</div>" for w in wakes)}</div>' if wakes else "")
        + '</div>' for title, ic, sub, items, wakes in watch)
    respond = f"""
  <div class="grid g2">
    <div class="card layer">
      <div class="layer-tag auto">Layer 1 · automated</div>
      <div class="layer-t">Onchain within one or two blocks</div>
      <p class="agent-b">Signals that leave no room for doubt trigger an automated action with no human approval needed, typically executed in under 30 seconds and scoped to the affected market or position, so everything else keeps earning. Exits are graded: a <b>soft shutdown</b> drains a market and can be undone after review; a <b>hard shutdown</b> also sets its caps to zero.</p>
      <div class="example"><b>On record:</b> when Cap paused its cUSD stablecoin on 9 June 2026, the exit agent on KPK USDC Yield started draining the affected markets about <a href="{esc(DOCS['cap_example_tx'])}" target="_blank" rel="noopener">15 seconds after the alert ↗</a>. Cap confirmed a false positive minutes later, and the markets were re-enabled after review.</div>
    </div>
    <div class="card layer">
      <div class="layer-tag human">Layer 2 · human-reviewed</div>
      <div class="layer-t">The risk team, paged when it matters</div>
      <p class="agent-b">Signals where acting automatically would cause false-positive disruption go to KPK's risk curation team, with a page for critical ones. A reviewer decides whether to force a rebalance, shut a market or do nothing. Monitoring is planned with each risk review, so the risks found on paper are the risks watched in production.</p>
      <div class="chips">{chips(["Hypernative", "Cyvers", "Pharos Watch", "KPK's own monitors", "incident-response accounts on X"])}</div>
    </div>
  </div>"""
    agents = [
        ("Health-factor manager", f"keeps HF between {band['repay_trigger']:.2f} and {band['borrow_trigger']:.2f}",
         f"Holds the BTC leg inside the band with a {band['borrow_target']:.2f} target. Monitoring reads the health factor every block and wakes it the moment HF leaves the band; it also runs every {RC['check_minutes']} minutes as a backstop. KPK sets the band itself: tighter when markets are volatile, looser when they are calm. Below the repay trigger it withdraws USDC from the liquid sleeve and repays; above the borrow trigger it borrows and deploys, split across the two vaults."),
        ("Exit agent (emergency unwind)", "unwinds the whole loan",
         f"Withdraws from the vaults and repays all the debt in one transaction. It fires on its own if HF falls below {RC['hf_exit_below']:.2f}, or if borrowing costs more than the vaults earn for {RC['spread_exit']}, and switches re-lever off so nothing borrows again until the team re-opens the position."),
        ("Agents on the KPK USDC vaults", "rebalance, exit, monitoring",
         "Each vault runs three agents through its own permissions layer. The rebalance agent reallocates across approved Morpho markets within tier and cap limits as APYs, flows and utilisation change. The exit agent de-risks a single market on a risk alert such as oracle staleness, liquidity stress, a collateral pause or price divergence. The monitoring agent detects forceDeallocate griefing and restores allocations."),
    ]
    agent_html = "".join(f'<details class="card agent"><summary><span class="stat-label">{esc(a)}</span><span class="agent-s">{esc(s)}</span></summary><p class="agent-b">{esc(b)}</p></details>' for a, s, b in agents)
    ev = "".join(f"<li>{x}</li>" for x in risk_evidence())
    return f"""
<section id="risk">
  <span class="label">How we manage risk</span>
  <h2>Automated, watched around the clock, bounded onchain</h2>
  <p class="lede">KPK runs the fund with deterministic agents. They act only within tightly defined onchain permissions and do not exercise discretion: when a predefined condition is met, they execute a predefined action. They cannot introduce new strategies, expand exposure beyond approved limits, or bypass risk constraints.</p>
  <div class="doc-links"><a href="{esc(DOCS['automation'])}" target="_blank" rel="noopener">How KPK automation works ↗</a><a href="{esc(DOCS['risk_framework'])}" target="_blank" rel="noopener">KPK Risk Framework ↗</a><a href="{esc(DOCS['investment_framework'])}" target="_blank" rel="noopener">Investment framework ↗</a></div>
  <div class="riskflow">{flow}</div>
  <div class="sub-h">What is watched</div>
  <div class="grid g3">{watch_html}</div>
  <div class="sub-h">How a signal is handled</div>
  {respond}
  <div class="sub-h">The agents</div>
  <div class="grid g3">{agent_html}</div>
  <div class="card">
    <div class="stat-label">Measured on this fund</div>
    <ul class="notes" style="margin-top:8px">{ev}</ul>
    <div class="stat-sub" style="margin-top:8px">Testing examples ran on a few dollars of KPK's own money; the transactions are listed under the automation section. Exit thresholds as set on {esc(RC['as_of'])}.</div>
  </div>
  {PERMISSIONS_CARD}
</section>
"""


RISKS = [
    ("BTC price", "Holders keep full BTC exposure; nothing is hedged.", "This is the product: carry on top of the BTC held, not a hedge.", False),
    ("Liquidation", "A BTC drop faster than the position can be deleveraged.",
     f"Monitoring reads the health factor every block. The agent repays from {band['repay_trigger']:.2f}, where BTC would still have to fall {(1 - 1 / band['repay_trigger']) * 100:.0f}% to reach liquidation, and the exit agent unwinds the whole loan below {RC['hf_exit_below']:.2f}. KPK tightens the band when markets turn volatile.", False),
    ("Negative carry", "Borrowing costs rise above what the vaults earn.", f"The exit agent unwinds if that lasts {RC['spread_exit']}.", False),
    ("Vault liquidity", "Thin liquidity in the Morpho vaults can slow deleveraging and redemptions.", f"At least {CONFIG['liquid_floor_pct']}% of the BTC-leg debt stays in the liquid sleeve, and the vault agents rebalance as liquidity changes.", False),
    ("Oracles and pegs", "Manipulation, staleness or failure of a price feed; WBTC or a stablecoin off its peg.", "Every price is checked against independent monitor feeds, and settlement pauses on a stale or irregular price.", False),
    ("Smart contracts", "Aave V4, Morpho, the stablecoins, the BTC wrappers (WBTC; cbBTC and cirBTC once live) and the fund's own contracts.", "Each one passes KPK's Risk Framework review: more than 150 checks across 12 risk dimensions.", False),
    ("WBTC custody", "WBTC is backed by BTC held by a custodian.", "Its price is watched like every other in the NAV; cbBTC and cirBTC are coming as further deposit assets.", False),
    ("Credit (future)", "Once the loop sleeve is live: a Maple borrower default can mark syrup tokens down.", "The loop is capped at 8x and kept to a share of the stablecoins; it is never relied on to deleverage the BTC leg.", True),
]
RISK_ROWS = "".join(
    f'<div class="risk-row{" muted-row" if fut else ""}"><div class="risk-n">{esc(n)}</div><div class="risk-d">{esc(d)}</div><div class="risk-h">{esc(h)}</div></div>'
    for n, d, h, fut in RISKS)

def mini(label, value, sub):
    return f'<div class="mini"><span class="m-label">{esc(label)}<em>{sub}</em></span><span class="m-value">{value}</span></div>'


REW_SHORT = {"Aave borrow rebate": "rebate", "USDC vault rewards": "vault rewards", "Other rewards": "other"}
rew_split = " · ".join(f"{esc(REW_SHORT.get(g['group'], g['group']))} {usd(g['claimable_usd'] + g['pending_usd'], 2)}" for g in rewards["groups"]) if rewards else ""
hero_side = "".join([
    mini("NAV", f"{n(chain['nav_wbtc'], 4)} WBTC", f"≈ {usd(chain['nav_usd'])}"),
    mini("Share price", f"{n(chain['share_price_wbtc'], 6)} WBTC", f"last settled {n(chain['last_settled_wbtc'], 6)}"),
    mini("Health factor", hf_txt(hf_now), f"band {band['repay_trigger']:.2f} · {band['borrow_target']:.2f} · {band['borrow_trigger']:.2f} (repay · target · re-lever)"),
    mini("LTV", f"{ltv * 100:.1f}%", f"collateral factor {cf * 100:.1f}% ÷ HF · liquidation at {usd(chain['liq_px'])}"),
    mini("Unclaimed rewards", usd(rewards["total_usd"], 2) if rewards else "n/a", (f"≈ {n(rewards['total_wbtc'], 5)} WBTC, not yet in NAV · {rew_split}") if rewards else "not read at this build"),
])
trail_bits = [f"{s['label']} {s['carry']:+.2f}%" for s in spreads if s["key"] != "live"]
trail_line = ("On trailing rates: " + " · ".join(trail_bits) + ". ") if trail_bits else ""
per_btc = usd(px * ltv * spread / 100 * (1 - PERF_FEE)) if spread is not None else "n/a"

spread_buttons = "".join(
    f'<button type="button" class="spread-btn" data-spread="{s["spread"]:.6f}" data-vault="{s["vault"]:.4f}" data-net="{s["borrow_net"]:.4f}" aria-current="{"true" if s["key"] == "live" else "false"}">'
    f'<i>{esc(s["label"])}</i><b>{s["spread"]:+.2f} pp</b></button>' for s in spreads)

def share_label(s):
    return "0% (today)" if s == 0 else f"{s * 100:.0f}%"


loop_html = ""
if loop_proj:
    lp = loop_proj
    rows_html = "".join(
        f'<tr><td>{share_label(r["share"])}</td><td class="num mono"><b>{r["carry"]:+.2f}%</b></td></tr>' for r in lp["rows"])
    first_draw = next((r for r in lp["rows"] if r["share"] > 0 and r.get("today_rate") is not None), None)
    caveat = (f" That needs more Aave room for the Maple spoke: the one USDG line with room today (Global Dollar hub, {musd(lp['gd_liq'])} free at {pct(lp['gd_now'])}) "
              f"would rise to {pct(first_draw['today_rate'])} for a {first_draw['share'] * 100:.0f}% loop at today's fund size, above the loop's {pct(lp['breakeven'])} break-even.") if first_draw else ""
    loop_html = (f'<div class="loop-proj"><div class="stat-label">What the loop would add <span class="pill">projection</span></div>'
                 f'<table class="data compact"><thead><tr><th>Loop share of the borrowed stablecoins</th><th class="num">Carry on BTC</th></tr></thead><tbody>{rows_html}</tbody></table>'
                 f'<div class="stat-sub">At today\'s rates: syrupUSDG yields {pct(lp["syrup"])} (7-day, Maple). Borrowing USDG at {pct(lp["borrow"])}, today\'s Core-hub rate, at {LOOP["leverage"]}x, the loop earns {pct(lp["yield"], 1)} on its equity; the rest of the stablecoins stay in the liquid sleeve.{caveat} '
                 f'At {LOOP["leverage"]}x the loop\'s own health factor is {lp["loop_hf"]:.3f}: a {(1 - 1 / lp["loop_hf"]) * 100:.1f}% markdown of syrupUSDG would liquidate it, which is why it is capped and partly unwound when borrow costs rise.</div></div>')

page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>BTC Carry Fund</title>
<meta name="description" content="KPK's BTC Carry Fund: BTC collateral on Aave V4, USDC borrowed and deployed in KPK's USDC vaults. Live position, agent activity and terms.">
<style>{fonts_css}{tokens_css}{CSS}</style>
</head>
<body>
<div class="page">

<header class="top">
  <div class="logo" aria-label="KPK">{logo_svg}</div>
  <div class="meta">
    <span>Ethereum mainnet</span><span aria-hidden="true">·</span>
    <span class="asof" id="asOf" data-built="{chain['read_at']}">As of {esc(as_of)}</span>
    <button type="button" class="theme-btn" id="themeBtn" title="Switch theme">Auto</button>
  </div>
</header>

<section class="hero" id="overview">
  <h1>BTC Carry Fund <span class="pill">live since {esc(utc(window_t0).strftime('%-d %b %Y'))}</span></h1>
  <p>The KPK BTC Carry Fund earns a carry on BTC through collateralised lending on blue-chip DeFi protocols. Wrapped BTC is supplied as collateral on Aave V4, USDC is borrowed against it at an automatically managed loan-to-value, and the proceeds are deployed into KPK-curated USDC vaults on Morpho. Holders stay fully exposed to BTC and earn the spread between stablecoin lending yields and borrow costs, with returns accruing in BTC.</p>
  <p>Deposits are in WBTC today; cbBTC and cirBTC deposits will be enabled next. The fund is live and currently funded by KPK, with no performance fee charged yet.</p>
  <div class="row">
    <a class="cta" href="{esc(app_url)}" target="_blank" rel="noopener">View the fund in the KPK app ↗</a>
    <a class="cta ghost" href="{esc(CONFIG['docs']['fund'])}" target="_blank" rel="noopener">Read the fund docs ↗</a>
    <span class="asof-note">Deposit assets:</span> {deposit_chips}
  </div>
  <div class="scope" aria-label="Scope">{scope_cards()}</div>
</section>
{stale_note}
<section id="numbers">
  <span class="label">Headline numbers</span>
  <h2>What the fund earns on BTC</h2>
  <div class="headline">
    <div class="card hero-main">
      <div class="stat-label">Carry on BTC <span class="pill live-pill">at live rates</span></div>
      <div class="stat-value hero-value good">{carry_txt}</div>
      <div class="stat-note">a year on the BTC held, at live rates. The fund borrows USDC at <b>{ltv * 100:.1f}%</b> LTV and earns a <b>{n(spread)} pp</b> spread on it, net of the {rebate_txt} Merkl borrow rebate and the 10% performance fee. <em>Not a promise: rates move.</em></div>
      <div class="stat-sub">{(f"<b>{carry / (1 - PERF_FEE):+.2f}% before the fee</b>, which is what KPK's own capital earns today. ") if carry is not None else ""}{esc(trail_line)}≈ {per_btc} a year per 1 BTC at today's price.</div>
      <div class="kv"><span>Realised</span><span class="mono">{'see below' if realised else 'from ' + (utc(realised_from).strftime('%-d %b') if realised_from else 'n/a')}</span></div>
      {realised_html}
    </div>
    <div class="side-stats">{hero_side}</div>
  </div>
  {math_strip}
  {f'<div class="note">{esc(nav_note)}</div>' if nav_note else ''}
</section>

<section id="structure">
  <span class="label">Position structure</span>
  <h2>How the position is built</h2>
  <div class="flow">
    <div class="card leg">
      <div class="leg-head"><span class="leg-kind">Collateral</span>{icon("aave")}</div>
      <div class="leg-amount">{n(chain['collateral_wbtc'], 8)} WBTC</div>
      <div class="kv"><span>value</span><span class="mono">≈ {usd(chain['collateral_usd'])}</span></div>
      <div class="kv"><span>venue</span><span><a href="{ETHERSCAN}/address/{L.SPOKE}" target="_blank" rel="noopener">Aave V4 Bluechip ↗</a></span></div>
      <div class="kv"><span>collateral factor</span><span class="mono">{cf * 100:.1f}%</span></div>
    </div>
    <div class="arrow" aria-hidden="true">→</div>
    <div class="card leg">
      <div class="leg-head"><span class="leg-kind">Borrow · Aave V4 Core hub</span>{icon("aave")}</div>
      <div class="leg-amount">{n(chain['debt_usdc'], 0)} USDC</div>
      <div class="kv"><span>gross rate</span><span class="mono">{pct(borrow_gross)}</span></div>
      <div class="kv"><span>Merkl rebate</span><span class="mono">−{rebate_txt}</span></div>
      <div class="kv"><span><b>net rate</b></span><span class="mono"><b>{net_borrow_txt}</b></span></div>
    </div>
    <div class="arrow" aria-hidden="true">→</div>
    <div class="card leg">
      <div class="leg-head"><span class="leg-kind">Liquid sleeve · Morpho</span>{icon("morpho")}</div>
      <div class="leg-amount">{n(sleeve_usdc, 0)} USDC</div>
      {legs_vaults}
      <div class="kv"><span><b>blended, 24h avg</b></span><span class="mono"><b>{pct(deploy_apy)}</b></span></div>
      {('<div class="kv"><span class="dim">of which Merkl rewards</span><span class="mono dim">+' + n(vault_rewards_pp, 2) + '%</span></div>') if vault_rewards_pp is not None else ''}
    </div>
  </div>
  <div class="card">
    <div class="stat-label">Share price in WBTC since inception</div>
    {share_chart()}
    <div class="stat-sub">{esc(CONFIG['milestones']['testing_phase']['note'][:1].upper() + CONFIG['milestones']['testing_phase']['note'][1:])} (shaded). At that size a single test deposit or redemption moves the share price by tens of parts per million: the two small dips. Deposits mint at the last settled price, and claimed rewards step NAV up once; both are marked.</div>
  </div>
  <div class="card loop">
    <div class="leg-kind">Loop sleeve · {'active' if loop_live else 'planned, not active'}</div>
    <div class="leg-sub" style="margin-top:6px">{esc(loop['what'][:1].upper() + loop['what'][1:])}, taking {esc(loop['share'])}. {esc(loop['limits'].replace('Leverage capped at 8x.', f"Leverage is capped at {LOOP['leverage']}x, well below the {1 / (1 - LOOP['collateral_factor']):.1f}x maximum the spoke's {LOOP['collateral_factor'] * 100:.0f}% collateral factor would allow."))}
    {'The fund now holds a position on that spoke.' if loop_live else 'It appears here as live once the fund holds a position on that spoke; until then all borrowed USDC sits in the liquid sleeve.'}</div>
    {loop_html}
  </div>
</section>

<section id="simulator">
  <span class="label">Carry simulator</span>
  <div class="card">
    <div class="sim-head">
      <div><h2>Carry per 1 BTC at a target health factor</h2>
      <p class="lede">The position is managed by health factor, so the simulator is too: HF sets LTV (collateral factor ÷ HF), LTV sets the carry. Live legs only; the loop sleeve is not included.</p></div>
      <div class="sim-out"><span class="big" id="simCarry">{carry_txt}</span><span class="muted">a year on BTC<br>at HF <b id="simHf">{hf_txt(hf_now)}</b>, after fee</span></div>
    </div>
    <div class="spread-picker" role="group" aria-label="Which spread to simulate with">
      <span class="picker-label">Spread</span>
      {spread_buttons}
      <span class="picker-note"><b>Live</b> is today's borrow rate against the vaults' 24-hour yield. The trailing options average the vault yield, the Core-hub USDC rate and the Merkl rebate over the same window, as a check on today's figure.{'' if len(spreads) > 1 else ' Trailing options appear once the rate history reaches back far enough.'}</span>
    </div>
    <div class="slider-wrap">
      <div class="ends"><span>HF 1.60, less carry</span><span class="hi">HF 1.05, more carry</span></div>
      <input type="range" id="hfSlider" step="1" aria-label="Target health factor">
      <div class="marks" id="hfMarks"></div>
    </div>
    <div class="note" id="simWarn" hidden>Below today's {MIN_HF:.2f} minimum: the fund would need its mandate changed to run here, and each step down leaves less room before liquidation.</div>
    <div class="sim-grid">
      <div class="sim-cell"><div class="k">LTV</div><div class="v" id="simLtv"></div></div>
      <div class="sim-cell"><div class="k">Borrowed per 1 BTC</div><div class="v" id="simBorrow"></div></div>
      <div class="sim-cell"><div class="k">Carry per 1 BTC</div><div class="v" id="simUsd"></div></div>
      <div class="sim-cell"><div class="k">BTC drop to liquidation</div><div class="v" id="simDrop"></div></div>
    </div>
    <div class="stat-sub" style="margin-top:12px">At live rates, BTC at {usd(px)}. The spread is held fixed: borrowing more pushes hub rates up, so the low-HF end reads high.</div>
  </div>
</section>

{risk_section()}
<section id="automation">
  <span class="label">The automation at work</span>
  <h2>Evidence that the agents do what we say</h2>
  <p class="lede">Every run of the fund's agents, including the many where the right answer was to do nothing, and every onchain transaction since the first subscription. <span class="asof-note">{esc(snap_label)}{'; live readings continue the charts after that' if snap else ''}.</span></p>
  {'<div class="note">' + esc(runs.get('note') or '') + '</div>' if runs.get('note') else ''}
  <div class="counters">{counters()}</div>
  <div class="card">
    <div class="stat-label">Health factor over time</div>
    <div class="legend"><span><i></i>health factor (log scale)</span><span><i class="band"></i>band in force</span><span><i class="liq"></i>liquidation</span><span><span class="shape" style="color:var(--c-band)">▲</span>re-lever</span><span><span class="shape" style="color:var(--c-liq)">▼</span>repay</span><span><span class="shape">◆</span>manual borrow</span><span><span class="shape">┊</span>deposit</span></div>
    {hf_chart()}
    <div class="stat-sub">{hf_caption}</div>
  </div>
  <div class="card">
    <div class="stat-label">BTC oracle price against agent rebalances</div>
    <div class="statrow">
      <div><div class="c-label">BTC oracle price</div><div class="c-value">{usd(px)}</div><div class="c-sub">{(pct((px / next(p['px'] for p in points if p.get('px') and p['t'] >= window_t0) - 1) * 100, 1, sign=True) + ' since the pre-pilot deposit') if any(p.get('px') and p['t'] >= window_t0 for p in points) else ''}</div></div>
      <div><div class="c-label">Agent rebalances</div><div class="c-value">{len([t for t in window_txs if t['kind'] in ('relever', 'derisk')])}</div><div class="c-sub">{len([t for t in window_txs if t['kind'] == 'relever'])} re-lever · {len([t for t in window_txs if t['kind'] == 'derisk'])} repay · plus {len([t for t in test_txs if t['kind'] in ('relever', 'derisk')])} in testing</div></div>
      <div><div class="c-label">Lowest HF</div><div class="c-value">{f"{lowest['hf']:.4f}" if lowest else 'n/a'}</div><div class="c-sub">{(short(lowest['t']) + f": BTC was {lowest['headroom']:.1f}% above the level where the agent starts repaying, {lowest['to_liq']:.1f}% above liquidation") if lowest else 'after the first re-lever'}</div></div>
      <div><div class="c-label">Buffer to liquidation</div><div class="c-value">{pct(liq_buffer, 1)}</div><div class="c-sub">liquidation price {usd(chain['liq_px'])}</div></div>
    </div>
    <div class="legend"><span><i></i>BTC oracle price (Aave V4)</span><span><i class="band"></i>no-action corridor</span><span><i class="liq"></i>liquidation price</span></div>
    {price_chart()}
    <div class="stat-sub">The corridor is the liquidation price × [repay trigger, borrow trigger], using the band in force at each moment. Inside it the agent does nothing; it moves whenever the agent borrows or repays, or the band is retuned. While re-lever is off the agent only repays, so the corridor has no upper edge.</div>
  </div>
  <div class="card">
    <details class="fold">
      <summary>Step by step: each stretch of time and what the agent did</summary>
      <p class="lede">Each row is either an action or the quiet stretch between two actions. In the quiet stretches the agent checks the position every 5 minutes and correctly does nothing. HF moves with the BTC price: a rally lifts it toward the re-lever trigger, a sell-off pushes it toward the repay trigger.</p>
      {legs_table()}
    </details>
  </div>
  <div class="card">
    <details class="fold">
      <summary>How it works: the fund's timeline, step by step</summary>
      <ul class="timeline">{''.join(f'<li><span class="when">{esc(w)}</span><span>{x}</span></li>' for w, x in narrative()) if snap else '<li>' + esc(snap_label.capitalize()) + '.</li>'}</ul>
    </details>
  </div>
  <div class="card">
    <details class="fold">
      <summary>Every onchain action: {len(window_txs)} since the pre-pilot deposit{f", {len(test_txs)} in testing" if test_txs else ""}</summary>
      <p class="lede">One row per transaction, each linked to Etherscan. HF is read onchain one block before and at the transaction's block.</p>
      {tx_table()}
    </details>
  </div>
</section>

<section id="scaling">
  <span class="label">How the fund scales</span>
  <h2>Sized by fund size, not by date</h2>
  <p class="lede">{esc(CONFIG['scaling']['intro'])}</p>
  <div class="card"><div class="table-scroll"><table class="data">
    <thead><tr><th>Stage (fund size)</th><th>What the strategy adds</th><th>What it needs</th><th>Status</th></tr></thead>
    <tbody>{scaling_rows()}</tbody></table></div>
    <div class="stat-sub" style="margin-top:12px"><b>Live capacity, block {chain['block']:,}:</b> {capacity_line()}</div>
  </div>
</section>

<section id="terms">
  <span class="label">Risks and terms</span>
  <h2>Main risks, and how they are handled</h2>
  <div class="card">
    <div class="risk-list">
      {RISK_ROWS}
    </div>
  </div>
  <div class="grid g2">
    <div class="card">
      <div class="stat-label">Redemptions</div>
      <div class="redeem">
        <div class="redeem-tile"><div class="redeem-v">Minutes to hours</div><div class="redeem-k">for redemptions up to what the liquid sleeve covers, paid at the next settlement cycle</div></div>
        <div class="redeem-tile strong"><div class="redeem-v">Up to ~3 days</div><div class="redeem-k">above that: the full settlement window, never longer</div></div>
      </div>
      <div class="stat-sub" style="margin-top:10px">Paid in the asset requested. No redemption fee.</div>
    </div>
    <div class="card">
      <div class="stat-label">Limits and fees</div>
      <div class="kv"><span>Health-factor band today</span><span class="mono">{band['repay_trigger']:.2f} · {band['borrow_target']:.2f} · {band['borrow_trigger']:.2f}</span></div>
      <div class="kv"><span>Minimum health factor</span><span class="mono">{MIN_HF:.2f}, a floor, not a target</span></div>
      <div class="kv"><span>Liquid floor</span><span class="mono">≥ {CONFIG['liquid_floor_pct']}% of the BTC-leg debt</span></div>
      <div class="kv"><span>Performance fee</span><span class="mono">10% above a WBTC high watermark</span></div>
      <div class="kv"><span>Management and redemption fees</span><span class="mono">none</span></div>
      <div class="stat-sub" style="margin-top:8px">KPK sets the band with discretion: tighter, at a higher health factor, when markets are volatile, and looser when they are calm, never below the {MIN_HF:.2f} floor. Once the performance fee is switched on it applies to every holder, KPK included.</div>
    </div>
  </div>
</section>

<footer id="method">
  <p><a class="cta" href="{esc(app_url)}" target="_blank" rel="noopener">View the fund in the KPK app ↗</a></p>
  <p><b>Data.</b> Live figures are read from Ethereum mainnet at block {chain['block']:,} ({esc(as_of)}) through a public RPC: the Aave V4 Bluechip spoke and Core hub, the fund's NAV calculator and shares contract, and the two KPK USDC vaults. Vault APYs come from the Morpho API as 24-hour averages, blended by the live allocation. The borrow rebate and unclaimed rewards come from the Merkl API. The BTC price is the Aave V4 oracle price, read as collateral value ÷ collateral amount.</p>
  <p><b>Agent activity.</b> From KPK's agent run records and the fund's onchain transactions, {esc(snap_label)}. Shown from the first subscription on {esc(utc(window_t0).strftime('%-d %b %Y'))}; earlier setup and small test transactions are on Etherscan. HF before and after a transaction is read onchain one block before and at its block; hourly readings fill the charts in between.</p>
  <p><b>Reading the numbers.</b> ≈ marks a derived or rounded figure: USD values at the oracle price, WBTC equivalents of USD amounts. Carry figures use live rates read at each build, not a backtest, and are not promises; the loop table is a projection. Carry = LTV × (vault yield − net borrow cost) × (1 − 10% performance fee), on the BTC held.</p>
  <p><b>Updates.</b> Live figures every hour; agent activity at each sync. Contracts: <a href="{ETHERSCAN}/address/{L.SHARES}" target="_blank" rel="noopener">shares</a> · <a href="{ETHERSCAN}/address/{L.SAFE}" target="_blank" rel="noopener">portfolio Safe</a> · <a href="{ETHERSCAN}/address/0x5d48084481660C97DecDEFE65482fD091d7FDe81" target="_blank" rel="noopener">Manager Safe</a> · <a href="{ETHERSCAN}/address/{L.NAV_CALC}" target="_blank" rel="noopener">NAV calculator</a>.</p>
</footer>

</div>
<script>window.SIM = {json.dumps(sim)};</script>
<script>{JS}</script>
</body>
</html>
"""

OUT.write_text(page)

# One reading per build, appended only after the page is written.
if not live_samples or this_sample["b"] > max(s.get("b", 0) for s in live_samples):
    with SAMPLES_PATH.open("a") as fh:
        fh.write(json.dumps(this_sample, separators=(",", ":")) + "\n")

soft = [x for x in (apys_err and f"vault APYs: {apys_err}", rebate_err and f"rebate: {rebate_err}", rewards_err and f"rewards: {rewards_err}") if x]
print(json.dumps({"wrote": OUT.name, "bytes": len(page), "block": chain["block"], "hf": hf_now, "carry_pct": carry, "snapshot": bool(snap), "soft_failures": soft}))
