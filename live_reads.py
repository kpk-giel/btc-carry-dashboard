"""Live reads for the BTC Carry Fund board. Stdlib only, keyless, latest block only.

Every figure here is read from a public source at build time:

- Ethereum mainnet through a public RPC (the Aave V4 spoke and hubs, the NAV
  calculator, the shares contract and the two KPK USDC vaults);
- the Morpho API for the vaults' 24-hour average APYs;
- the Merkl API for the Aave V4 borrow rebate and for the fund's unclaimed rewards.

Selectors are hardcoded because the stdlib has no keccak. Each one is the first four
bytes of keccak256 of the signature beside it.
"""
import json
import time
import urllib.request

RPC = "https://ethereum-rpc.publicnode.com"
MORPHO = "https://api.morpho.org/graphql"
MERKL = "https://api.merkl.xyz/v4"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/json",
}

SAFE = "0x0295E5d0530C23E560CdbFc097C488a0A0D7752D"
SHARES = "0xf180800f239f0ea0ED2F8156581fac2984cEe389"
NAV_CALC = "0x54EaD2A1dB7456cA917675Ea8908ec8A997c6214"
SPOKE = "0x973a023A77420ba610f06b3858aD991Df6d85A08"
MAPLE_SPOKE = "0x774b9655413c34809c1f1b16b654465A89EBE989"
CORE = "0xCca852Bc40e560adC3b1Cc58CA5b55638ce826c9"
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
VAULTS = [
    ("KPK USDC Yield", "0xD5cCe260E7a755DDf0Fb9cdF06443d593AaeaA13"),
    ("KPK USDC Yield RWA", "0x7a72bcD2c3F7F7e4D6679170a0625bAB15D7DDa1"),
]
GLOBAL_DOLLAR = "0x62d63197660c080236193CA60b70E49A08E90368"
SYRUP_USDG = "0x87b65c4aaffa76881f9e96f3e7ed945ddfc3cd7a"
MAPLE_API = "https://api.maple.finance/v2/graphql"
GD_USDG_ASSET = 3  # the USDG line the Maple spoke draws from on the Global Dollar hub
RESERVE_WBTC = 1
RESERVE_USDC_CORE = 7
CORE_USDC_ASSET = 5
CORE_USDG_ASSET = 8
PRIME = "0x943827DCA022D0F354a8a8c332dA1e5Eb9f9F931"
PRIME_USDC_ASSET = 4

SEL = {
    "getUserAccountData(address)": "0xbf92857c",
    "getUserSuppliedAssets(uint256,address)": "0xf1568a89",
    "getUserTotalDebt(uint256,address)": "0x9b7172a6",
    "getAsset(uint256)": "0xeac8f5b8",
    "getAssetOwed(uint256)": "0x152ee0d3",
    "getAddedAssets(uint256)": "0x24ba667f",
    "getSpokeConfig(uint256,address)": "0xf701f06e",
    "getSpokeTotalOwed(uint256,address)": "0xefdba7c0",
    "getInterestRateData(uint256)": "0x5979378b",
    "getAccountNav(address,address)": "0xc3a28a14",
    "totalSupply()": "0x18160ddd",
    "getLastSettledPrice(address)": "0xbce46c2c",
    "balanceOf(address)": "0x70a08231",
    "convertToAssets(uint256)": "0x07a2d13a",
    "calculateInterestRate(uint256,uint256,uint256,uint256,uint256)": "0xf5fe86ee",
}
NO_DEBT = 2**256 - 1
SECONDS_PER_YEAR = 365 * 24 * 3600
ZERO = "0x0000000000000000000000000000000000000000"


def _addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def _uint(n):
    return hex(n)[2:].rjust(64, "0")


def _data(sig, *args):
    out = SEL[sig]
    for a in args:
        out += _addr(a) if isinstance(a, str) else _uint(a)
    return out


def _words(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _post(url, payload, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def eth_calls(calls, block="latest", attempts=3):
    """One JSON-RPC batch of eth_calls at one block. Returns hex results in order; raises on any error."""
    body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, block]} for i, (to, data) in enumerate(calls)]
    last = None
    for attempt in range(attempts):
        try:
            rows = _post(RPC, body)
            if not isinstance(rows, list) or len(rows) != len(calls):
                raise RuntimeError("rpc batch returned the wrong number of results")
            rows.sort(key=lambda r: r["id"])
            errors = [r for r in rows if "error" in r or not str(r.get("result", "")).startswith("0x")]
            if errors:
                raise RuntimeError(f"rpc error on call {errors[0]['id']}: {str(errors[0].get('error'))[:120]}")
            return [r["result"] for r in rows]
        except Exception as exc:  # network blips: retry, then fail loudly
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"chain read failed: {last}")


def block_number():
    r = _post(RPC, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    return int(r["result"], 16)


def apr_to_apy_pct(apr):
    """Annual APR (fraction) as a per-second-compounded APY in percent, as Aave quotes it."""
    return ((1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100


def _asset(words):
    """Hub.getAsset struct, a flat static tuple of 17 words."""
    return {
        "liquidity": words[0],
        "swept": words[4],
        "liquidity_fee": words[8] / 10_000,
        "drawn_rate_ray": words[10],
        "underlying": "0x" + hex(words[12])[2:].rjust(40, "0"),
        "ir_strategy": "0x" + hex(words[13])[2:].rjust(40, "0"),
        "deficit": words[16] // 10**27,
    }


def _nav(words):
    """getAccountNav's struct: (value, quoteAsset, timestamp, stale[], sequencerDown, quoteAssetStale, irregular[], quoteAssetIrregular, monitors[]).

    Returns (value, healthy). A read with a stale or irregular price is not a NAV: when the
    WBTC feed sits past its heartbeat the calculator answers ~0.4 WBTC for a ~4.78 WBTC fund.
    """
    base = words[0] // 32
    head = words[base:base + 9]
    stale = words[base + head[3] // 32]
    irregular = words[base + head[6] // 32]
    healthy = not head[4] and not head[5] and not head[7] and stale == 0 and irregular == 0
    return head[0], healthy


def read_chain(retries=2):
    """Position, NAV, share price, hub rates and capacity, all at the latest block.

    Re-reads at a later block when the NAV calculator flags a stale price, so a feed
    between heartbeats does not print as a NAV collapse.
    """
    for attempt in range(retries + 1):
        out = _read_chain_once()
        if out["nav_healthy"] or attempt == retries:
            return out
        time.sleep(15)


def _read_chain_once():
    blk = block_number()
    tag = hex(blk)
    calls = [
        (SPOKE, _data("getUserAccountData(address)", SAFE)),                       # 0
        (SPOKE, _data("getUserSuppliedAssets(uint256,address)", RESERVE_WBTC, SAFE)),  # 1
        (SPOKE, _data("getUserTotalDebt(uint256,address)", RESERVE_USDC_CORE, SAFE)),  # 2
        (NAV_CALC, _data("getAccountNav(address,address)", SAFE, WBTC)),             # 3
        (NAV_CALC, _data("getAccountNav(address,address)", SAFE, ZERO)),             # 4
        (SHARES, _data("totalSupply()")),                                              # 5
        (SHARES, _data("getLastSettledPrice(address)", WBTC)),                         # 6
        (CORE, _data("getAsset(uint256)", CORE_USDC_ASSET)),                          # 7
        (CORE, _data("getAssetOwed(uint256)", CORE_USDC_ASSET)),                      # 8
        (CORE, _data("getAddedAssets(uint256)", CORE_USDC_ASSET)),                    # 9
        (CORE, _data("getAsset(uint256)", CORE_USDG_ASSET)),                          # 10
        (CORE, _data("getAssetOwed(uint256)", CORE_USDG_ASSET)),                      # 11
        (CORE, _data("getSpokeConfig(uint256,address)", CORE_USDC_ASSET, SPOKE)),     # 12
        (CORE, _data("getSpokeTotalOwed(uint256,address)", CORE_USDC_ASSET, SPOKE)),  # 13
        (CORE, _data("getSpokeConfig(uint256,address)", CORE_USDG_ASSET, SPOKE)),     # 14
        (CORE, _data("getSpokeTotalOwed(uint256,address)", CORE_USDG_ASSET, SPOKE)),  # 15
        (MAPLE_SPOKE, _data("getUserAccountData(address)", SAFE)),                   # 16
        (USDC, _data("balanceOf(address)", SAFE)),                                     # 17
        (WBTC, _data("balanceOf(address)", SAFE)),                                     # 18
    ] + [(addr, _data("balanceOf(address)", SAFE)) for _, addr in VAULTS]              # 19, 20
    res = eth_calls(calls, tag)
    w = [_words(r) for r in res]

    acct = w[0]
    supplied = w[1][0] / 1e8
    debt = w[2][0] / 1e6
    cf = acct[1] / 1e18
    hf = None if acct[2] == NO_DEBT else acct[2] / 1e18
    coll_value = acct[3] / 1e26
    debt_value = acct[4] / 1e53
    btc_px = coll_value / supplied if supplied > 0 else None
    nav_raw_wbtc, healthy_wbtc = _nav(w[3])
    nav_raw_usd, healthy_usd = _nav(w[4])
    nav_wbtc = nav_raw_wbtc / 1e8
    nav_usd = nav_raw_usd / 1e8
    supply = w[5][0] / 1e18

    usdc = _asset(w[7])
    usdc_drawn, usdc_premium = w[8][0], w[8][1]
    usdc_added = w[9][0]
    usdg = _asset(w[10])
    usdg_drawn, usdg_premium = w[11][0], w[11][1]

    second = [(usdc["ir_strategy"], _data("getInterestRateData(uint256)", CORE_USDC_ASSET))]
    second += [(addr, _data("convertToAssets(uint256)", w[19 + i][0])) for i, (_, addr) in enumerate(VAULTS)]
    res2 = eth_calls(second, tag)
    optimal = _words(res2[0])[0] / 10_000
    vault_assets = [_words(r)[0] / 1e6 for r in res2[1:]]

    total = usdc["liquidity"] + usdc_drawn + usdc["swept"]
    at_kink = total * round(optimal * 10_000) // 10_000
    room_to_kink = max(0, at_kink - usdc_drawn) / 1e6

    def cap_room(cfg, owed):
        return max(0.0, cfg[1] - owed[0] / 1e6)

    maple = w[16]
    return {
        "block": blk,
        "read_at": int(time.time()),
        "hf": hf,
        "cf": cf,
        "risk_premium_bps": acct[0],
        "collateral_wbtc": supplied,
        "debt_usdc": debt,
        "collateral_usd": coll_value,
        "debt_usd": debt_value,
        "btc_px": btc_px,
        "liq_px": debt_value / (supplied * cf) if supplied > 0 and cf > 0 and debt_value > 0 else None,
        "ltv": debt_value / coll_value if coll_value > 0 else None,
        "nav_healthy": healthy_wbtc and healthy_usd,
        "nav_wbtc": nav_wbtc,
        "nav_usd": nav_usd,
        "supply": supply,
        "share_price_wbtc": nav_wbtc / supply if supply > 0 else None,
        "last_settled_wbtc": w[6][0] / 1e8,
        "idle_usdc": w[17][0] / 1e6,
        "idle_wbtc": w[18][0] / 1e8,
        "vaults": [{"name": n, "address": a, "usdc": vault_assets[i]} for i, (n, a) in enumerate(VAULTS)],
        "core_usdc": {
            "borrow_apy": apr_to_apy_pct(usdc["drawn_rate_ray"] / 1e27),
            "liquidity": usdc["liquidity"] / 1e6,
            "drawn": usdc_drawn / 1e6,
            "utilisation": (usdc_drawn + usdc_premium) / usdc_added if usdc_added else None,
            "optimal_usage": optimal,
            "room_to_kink": room_to_kink,
            "bluechip_room_under_cap": cap_room(w[12], w[13]),
        },
        "core_usdg": {
            "borrow_apy": apr_to_apy_pct(usdg["drawn_rate_ray"] / 1e27),
            "liquidity": usdg["liquidity"] / 1e6,
            "bluechip_room_under_cap": cap_room(w[14], w[15]),
        },
        # The loop sleeve counts as live once the fund holds anything on the Maple spoke.
        "maple_active": maple[3] > 0 or maple[6] > 0,
    }


def read_vault_apys():
    """24-hour and 7-day average net APYs (percent) for both vaults. A vault that reports nothing is None."""
    fields = "name netApy a1: avgNetApy(lookback: ONE_DAY) a7: avgNetApy(lookback: SEVEN_DAYS) a30: avgNetApy(lookback: THIRTY_DAYS) x1: avgNetApyExcludingRewards(lookback: ONE_DAY)"
    q = "query { " + " ".join(f'v{i}: vaultV2ByAddress(address: "{a}", chainId: 1) {{ {fields} }}' for i, (_, a) in enumerate(VAULTS)) + " }"
    data = _post(MORPHO, {"query": q})
    if data.get("errors"):
        raise RuntimeError(f"morpho: {str(data['errors'])[:160]}")
    out = []
    for i, (name, addr) in enumerate(VAULTS):
        v = (data.get("data") or {}).get(f"v{i}") or {}
        pct = lambda x: None if x is None else x * 100
        out.append({"name": name, "address": addr, "apy_24h": pct(v.get("a1")), "apy_7d": pct(v.get("a7")), "apy_30d": pct(v.get("a30")), "apy_24h_ex_rewards": pct(v.get("x1")), "apy_now": pct(v.get("netApy"))})
    return out


def read_borrow_rebate():
    """The Merkl rebate on Core-hub USDC borrows, as Merkl pays it now (percent): daily rewards over eligible TVL, capped at the campaign APR."""
    camps = _get(f"{MERKL}/campaigns?chainId=1&type=AAVE_V4_HUB_NET_BORROWING&status=LIVE&items=100")
    if not isinstance(camps, list):
        raise RuntimeError("merkl campaigns: unexpected response")
    total = 0.0
    found = []
    for c in camps:
        hubs = ((c.get("params") or {}).get("hubs") or [{}])
        hub = (hubs[0] or {}).get("hubAddress", "")
        ids = [str(x) for x in (hubs[0] or {}).get("borrowAssetIds", [])]
        if hub.lower() != CORE.lower() or str(CORE_USDC_ASSET) not in ids:
            continue
        settings = ((c.get("params") or {}).get("distributionMethodParameters") or {}).get("distributionSettings") or {}
        cap = float(settings.get("apr") or 0)
        opp = _get(f"{MERKL}/opportunities/{c['opportunityId']}")
        tvl = opp.get("tvl")
        daily = c.get("dailyRewards")
        if not isinstance(tvl, (int, float)) or tvl <= 0 or not isinstance(daily, (int, float)):
            raise RuntimeError("merkl rebate campaign without TVL or daily rewards")
        paid = daily * 365 / tvl
        total += min(paid, cap) if cap > 0 else paid
        found.append({"cap_pct": cap * 100, "paid_pct": paid * 100, "ends": c.get("endTimestamp"), "opportunity": str(c["opportunityId"])})
    return {"rebate_pct": total * 100, "campaigns": found}


def read_rewards(btc_px):
    """Claimable and pending Merkl rewards at the Portfolio Safe, which NAV does not count until claimed."""
    rows = _get(f"{MERKL}/users/{SAFE}/rewards?chainId=1")
    groups = {"Aave borrow rebate": {"claimable": 0.0, "pending": 0.0}, "USDC vault rewards": {"claimable": 0.0, "pending": 0.0}, "Other rewards": {"claimable": 0.0, "pending": 0.0}}
    for chain in rows if isinstance(rows, list) else []:
        for rw in chain.get("rewards", []):
            tok = rw.get("token") or {}
            dec = int(tok.get("decimals") or 18)
            price = float(tok.get("price") or 0)
            for b in rw.get("breakdowns") or []:
                reason = b.get("reason") or ""
                g = "Aave borrow rebate" if reason.startswith(("AaveV4", "MultiLogProcessor")) else "USDC vault rewards" if reason.startswith("Morpho") else "Other rewards"
                claimable = (int(b.get("amount") or 0) - int(b.get("claimed") or 0)) / 10**dec
                pending = int(b.get("pending") or 0) / 10**dec
                groups[g]["claimable"] += claimable * price
                groups[g]["pending"] += pending * price
    out = [{"group": k, "claimable_usd": v["claimable"], "pending_usd": v["pending"]} for k, v in groups.items() if v["claimable"] or v["pending"]]
    usd = sum(g["claimable_usd"] + g["pending_usd"] for g in out)
    return {"groups": out, "total_usd": usd, "total_wbtc": usd / btc_px if btc_px else None, "claimable_usd": sum(g["claimable_usd"] for g in out), "pending_usd": sum(g["pending_usd"] for g in out)}


def read_loop_inputs(draws_usdg):
    """Inputs for the planned loop sleeve: syrupUSDG's yield (Maple API, percent) and the USDG borrow
    rate on the Maple spoke's Global Dollar line, now and after each draw in `draws_usdg` (whole USDG),
    priced by the hub's own rate strategy with supply held constant."""
    q = '{ poolV2(id: "%s") { weeklyApy monthlyApy } }' % SYRUP_USDG
    data = _post(MAPLE_API, {"query": q})
    pool = ((data or {}).get("data") or {}).get("poolV2") or {}
    if pool.get("weeklyApy") is None:
        raise RuntimeError("maple: no syrupUSDG yield")
    syrup_7d = int(pool["weeklyApy"]) / 1e28
    syrup_30d = int(pool["monthlyApy"]) / 1e28 if pool.get("monthlyApy") is not None else None
    res = eth_calls([(GLOBAL_DOLLAR, _data("getAsset(uint256)", GD_USDG_ASSET)), (GLOBAL_DOLLAR, _data("getAssetOwed(uint256)", GD_USDG_ASSET))])
    a = _asset(_words(res[0]))
    drawn = _words(res[1])[0]
    after = {}
    calls = []
    for d in draws_usdg:
        raw = int(d) * 10**6
        if raw > a["liquidity"]:
            continue
        calls.append((d, (a["ir_strategy"], _data("calculateInterestRate(uint256,uint256,uint256,uint256,uint256)", GD_USDG_ASSET, a["liquidity"] - raw, drawn + raw, a["deficit"], a["swept"]))))
    if calls:
        out = eth_calls([c for _, c in calls])
        for (d, _), r in zip(calls, out):
            after[d] = apr_to_apy_pct(_words(r)[0] / 1e27)
    return {"syrup_7d": syrup_7d, "syrup_30d": syrup_30d, "borrow_now": apr_to_apy_pct(a["drawn_rate_ray"] / 1e27), "liquidity": a["liquidity"] / 1e6, "borrow_after": after}



def read_borrow_lines(draws):
    """Borrow APY (percent, gross, before any Merkl rebate) now and after the fund's own draw on each
    line it can borrow from the Bluechip spoke: USDG and USDC on the Core hub, USDC on the Prime hub.
    `draws` maps a line to whole tokens; each hub's rate strategy prices the draw with supply held
    constant."""
    lines = [("usdg_core", CORE, CORE_USDG_ASSET), ("usdc_core", CORE, CORE_USDC_ASSET), ("usdc_prime", PRIME, PRIME_USDC_ASSET)]
    reads = []
    for _, hub, aid in lines:
        reads += [(hub, _data("getAsset(uint256)", aid)), (hub, _data("getAssetOwed(uint256)", aid))]
    res = eth_calls(reads)
    out, calls = {}, []
    for i, (key, hub, aid) in enumerate(lines):
        a = _asset(_words(res[2 * i]))
        drawn = _words(res[2 * i + 1])[0]
        now_apy = apr_to_apy_pct(a["drawn_rate_ray"] / 1e27)
        out[key] = {"now": now_apy, "liquidity": a["liquidity"] / 1e6, "after": now_apy}
        raw = int(draws.get(key, 0)) * 10**6
        if raw > a["liquidity"]:
            out[key]["after"] = None
        elif raw > 0:
            calls.append((key, (a["ir_strategy"], _data("calculateInterestRate(uint256,uint256,uint256,uint256,uint256)", aid, a["liquidity"] - raw, drawn + raw, a["deficit"], a["swept"]))))
    if calls:
        for (key, _), r in zip(calls, eth_calls([c for _, c in calls])):
            out[key]["after"] = apr_to_apy_pct(_words(r)[0] / 1e27)
    return out


def read_rebate_history(opportunities, days=31):
    """Daily average rebate (percent) per UTC day for the Core-hub USDC borrow opportunities, summed
    across them. A day with no record paid no rebate on this hub (it moved here on 1 Oct 2026)."""
    since = int(time.time()) - days * 86400
    per_day = {}
    for opp in dict.fromkeys(opportunities):
        rows = _get(f"{MERKL}/opportunities/{opp}/apr-records?from={since}&items=200")
        for r in rows if isinstance(rows, list) else []:
            day = int(r["timestamp"])
            per_day[day] = per_day.get(day, 0.0) + float(r.get("avg") or r.get("apr") or 0)
    return sorted(per_day.items())
