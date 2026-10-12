# BTC Carry Fund board

Static page for KPK's BTC Carry Fund: BTC collateral on Aave V4, USDC borrowed against it and deployed in KPK's USDC vaults. It shows the live position, what the fund earns on BTC, the agents' activity and the fund's terms.

`index.html` is generated. Do not edit it here.

## How it updates

- **Every 4 hours**, `.github/workflows/refresh.yml` runs `python3 build_dashboard.py index.html`. It reads public, keyless sources and appends one reading to `live_samples.jsonl`. No secrets are involved.
- **Agent activity and history** come from `public_snapshot.json`, which KPK syncs from its own tooling. The page builds without it; the history-based parts then say so.

| Source | Used for |
|---|---|
| Ethereum mainnet, public RPC | position, health factor, NAV, share price, hub rates and capacity |
| `api.morpho.org/graphql` | the KPK USDC vaults' 24-hour average APYs |
| `api.merkl.xyz` | the Aave V4 borrow rebate and the fund's unclaimed rewards |

## Files

| File | |
|---|---|
| `index.html` | the published page (generated) |
| `build_dashboard.py` | renders the page; stdlib only |
| `live_reads.py` | the live reads; stdlib only |
| `config.json` | editable copy: the scaling stages, the loop sleeve wording, the app link |
| `public_snapshot.json` | agent activity and history, synced by KPK |
| `live_samples.jsonl` | one reading per build, which extends the charts past the last sync |
| `assets/` | KPK design tokens, fonts and logo, inlined at build time |

To change the scaling stages, edit `config.json` and run the workflow from the Actions tab.
