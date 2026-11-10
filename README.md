# Public Mutual Return Trend Review

> **Disclaimer:** Web scraping a website without consent may have legal implications, so use with care and at your own risk. This project is not affiliated with or endorsed by Public Mutual Berhad. Figures are for reference only and are not investment advice.

Builds `output/pm_funds.db` (SQLite) and `output/pm_5y_total_returns.xlsx` with
distribution-inclusive (total) returns for Public Mutual UT and PRS funds.

## Sources

| Data | Source |
|------|--------|
| Total return (primary) | `FundOverview/GetFundPerformanceChartData` — Lipper NAV-to-NAV **Scope** cumulative % (same series as the Fund Overview chart) |
| Daily NAV | `FundIndex.Nav` from the same chart response |
| Month-end NAV snapshot | `FundPriceUT/GetAllUTFundPriceByDate` / `FundPricePRS/...` with `dd-MM-yyyy` (steps back if holiday) |
| Distributions | `FundDistribution/GetFundDistributionHistory?schemeCode=` (homepage Fund Distribution widget) — **returns only the latest ~5 records** |
| Fund master | `FundOverview/GetFundInfo` + latest price lists for UT/PRS type |

## Monthly update

```bash
pip install -r requirements.txt
python run_etl.py                  # uses HTTP cache under cache/
python run_etl.py --force          # re-download everything
python run_etl.py --export-only    # rebuild xlsx from existing DB
python run_etl.py --sanity-only    # PHS comparison only
```

Edit `WINDOW_START` / `WINDOW_END` in `config.py` when rolling the window forward
(e.g. set end to latest completed month-end).

Throttle defaults to 1.2s between API calls. Cache lives in `cache/`.

## Schema (SQLite)

- `funds` — master
- `nav_daily` — daily/month-end NAV
- `distributions` — sen/RM per unit, ex-date (`DistDate`), type; `unit_split` NULL (not published)
- `monthly_returns` — month-end NAV, TR index (100 at first month), price vs total monthly %
- `annualised_returns` — 1/3/5y total, arithmetic avg (PHS-style), CAGR
- `data_gaps` — truncated dist history, missing series, etc.

## Caveats

1. Distribution API truncates to ~5 rows — DIY reinvestment TR over 5y is incomplete for frequent distributors; **official chart Scope is the authoritative TR**.
2. No unit-split feed found on the public site; column reserved as NULL.
3. PHS “Average Annual Returns” = total return / years (arithmetic), not CAGR.
4. Foreign/MIX fund NAVs publish with a lag; chart `ReturnComputeEnddate` may trail calendar today.

## Disclaimer

Web scraping a website without consent may have legal implications, so use with care and at your own risk. Keep the request throttle in place and respect the website's terms of use.
