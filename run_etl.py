#!/usr/bin/env python3
"""
Public Mutual UT+PRS ETL: 5y distribution-inclusive (total) returns.

Primary TR source: Fund Overview performance chart (Lipper Scope cumulative %).
Also stores: fund master, daily NAV from chart, distribution history (API truncated),
month-end TR index / price vs total returns, 1/3/5y annualised.

Usage:
  python run_etl.py              # full run (uses cache)
  python run_etl.py --force      # ignore cache
  python run_etl.py --export-only
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# allow running from this folder
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    DB_PATH,
    XLSX_PATH,
    WINDOW_START,
    WINDOW_END,
    chart_date,
    price_date,
    CACHE_DIR,
)
from client import PMClient
from db import connect, upsert_many
from compute_returns import (
    month_ends_in_range,
    build_monthly_from_chart,
    annualised_from_series,
)
from export_xlsx import export as export_xlsx
from holdings_epf import run_holdings_and_epf

MYT = ZoneInfo("Asia/Kuala_Lumpur")
DIST_SRC = "https://www.publicmutual.com.my/home#FundDistribution"
PERF_SRC = "https://www.publicmutual.com.my/Fund-Overview"
PRICE_UT = "https://www.publicmutual.com.my/Fund-Price-UT"
PRICE_PRS = "https://www.publicmutual.com.my/fund-price-prs"


def now_iso() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S %Z")


def classify_type(name: str, code: str, prs_codes: set[str]) -> str:
    if code in prs_codes:
        return "PRS"
    n = (name or "").upper()
    if " PRS " in f" {n} " or n.startswith("PUBLIC MUTUAL PRS") or "PRS-" in n:
        return "PRS"
    return "UT"


def step_back_business_days(iso_date: str, max_back: int = 10) -> list[str]:
    """Candidates from iso_date stepping back calendar days (caller skips empty)."""
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    out = []
    for i in range(max_back + 1):
        out.append((d - timedelta(days=i)).strftime("%Y-%m-%d"))
    return out


def load_universe(client: PMClient) -> tuple[list[dict], set[str], dict]:
    """Return fund records from overview + UT/PRS price lists for type/category."""
    ut_cats = {
        c["CategoryCode"]: c["CategoryName"]
        for c in (client.ut_categories().get("ResultData") or [])
    }
    prs_cats = {
        c["CategoryCode"]: c["CategoryName"]
        for c in (client.prs_categories().get("ResultData") or [])
    }

    # latest prices for type tagging + category
    ut_latest = client.latest_ut_prices("").get("ResultData") or []
    prs_latest = client.latest_prs_prices("").get("ResultData") or []
    prs_codes = {r["FundCode"] for r in prs_latest}
    price_meta = {}
    for r in ut_latest:
        price_meta[r["FundCode"]] = {
            "type": "UT",
            "category": ut_cats.get(r.get("FundCategoryCode"), r.get("FundCategoryCode")),
            "is_islamic": 1 if r.get("IsIslamic") else 0,
            "foreign_exposure": r.get("ForeignExposureCode"),
            "name": r.get("FundDesc"),
            "abbr": r.get("FundAbbr"),
        }
    for r in prs_latest:
        price_meta[r["FundCode"]] = {
            "type": "PRS",
            "category": prs_cats.get(r.get("FundCategoryCode"), r.get("FundCategoryCode")),
            "is_islamic": 1 if r.get("IsIslamic") else 0,
            "foreign_exposure": r.get("ForeignExposureCode"),
            "name": r.get("FundDesc"),
            "abbr": r.get("FundAbbr"),
        }

    ov = client.overview_fund_list().get("ResultData") or []
    funds = []
    seen = set()
    for item in ov:
        code = str(item["SchemeCode"])
        seen.add(code)
        meta = price_meta.get(code, {})
        funds.append(
            {
                "fund_code": code,
                "name": meta.get("name") or item.get("FundDescAbbr") or code,
                "abbr": meta.get("abbr"),
                "fund_type": meta.get("type")
                or classify_type(item.get("FundDescAbbr", ""), code, prs_codes),
                "category": meta.get("category"),
                "is_islamic": meta.get("is_islamic"),
                "foreign_exposure": meta.get("foreign_exposure"),
            }
        )
    # any price-list funds missing from overview
    for code, meta in price_meta.items():
        if code not in seen:
            funds.append(
                {
                    "fund_code": code,
                    "name": meta["name"],
                    "abbr": meta["abbr"],
                    "fund_type": meta["type"],
                    "category": meta["category"],
                    "is_islamic": meta["is_islamic"],
                    "foreign_exposure": meta["foreign_exposure"],
                }
            )
    return funds, prs_codes, price_meta


def enrich_fund_info(client: PMClient, funds: list[dict], gaps: list):
    for i, f in enumerate(funds, 1):
        code = f["fund_code"]
        try:
            info = client.fund_info(code)
            rd = info.get("ResultData") or {}
            if not rd:
                gaps.append((code, "fund_info_empty", json.dumps(info)[:300], now_iso()))
                continue
            f["name"] = rd.get("SchemeDesc") or f["name"]
            f["abbr"] = rd.get("SchemeMnemonic") or f["abbr"]
            f["category"] = rd.get("SchCategoryName") or f["category"]
            islamic = rd.get("IslamicDesc")
            if islamic is not None:
                f["is_islamic"] = 1 if str(islamic).upper().startswith("Y") else 0
            f["foreign_exposure"] = rd.get("ForeignExposureEn") or f.get("foreign_exposure")
            f["dist_policy"] = rd.get("DistPolicyEn")
            ld = rd.get("SchemeLaunchDate")
            f["launch_date"] = ld[:10] if ld else None
            f["risk_level"] = rd.get("RiskLevel")
            f["currency"] = rd.get("CurrencyCode") or "MYR"
            print(f"  [{i}/{len(funds)}] info {code} {f['abbr']}", flush=True)
        except Exception as e:
            gaps.append((code, "fund_info_error", str(e), now_iso()))
            print(f"  [{i}/{len(funds)}] INFO FAIL {code}: {e}", flush=True)


def fetch_performance(
    client: PMClient, funds: list[dict], gaps: list
) -> dict[str, list[dict]]:
    start = chart_date(WINDOW_START)
    end = chart_date(WINDOW_END)
    out = {}
    for i, f in enumerate(funds, 1):
        code = f["fund_code"]
        try:
            data = client.performance_chart(code, start, end)
            if data.get("ResponseCode") != 1:
                gaps.append(
                    (
                        code,
                        "perf_no_data",
                        data.get("Message") or "no data",
                        now_iso(),
                    )
                )
                print(f"  [{i}/{len(funds)}] PERF empty {code}", flush=True)
                continue
            series = (data.get("ResultData") or {}).get("FundIndex") or []
            if not series:
                gaps.append((code, "perf_empty_series", "FundIndex empty", now_iso()))
                continue
            out[code] = series
            print(
                f"  [{i}/{len(funds)}] perf {code} n={len(series)} "
                f"{series[0]['Date']}..{series[-1]['Date']} "
                f"scope={series[-1]['Scope']:.2f}%",
                flush=True,
            )
        except Exception as e:
            gaps.append((code, "perf_error", str(e), now_iso()))
            print(f"  [{i}/{len(funds)}] PERF FAIL {code}: {e}", flush=True)
    return out


def fetch_distributions(client: PMClient, funds: list[dict], gaps: list) -> list[dict]:
    rows = []
    for i, f in enumerate(funds, 1):
        code = f["fund_code"]
        try:
            data = client.distribution_history(code)
            rd = data.get("ResultData") or []
            if data.get("ResponseCode") != 1 and not rd:
                # many funds truly have no recent distributions
                print(f"  [{i}/{len(funds)}] dist none {code}", flush=True)
                continue
            # API returns only latest ~5; flag if DistNo suggests older exist
            if rd:
                max_no = max((r.get("DistNo") or 0) for r in rd)
                if max_no > len(rd):
                    gaps.append(
                        (
                            code,
                            "dist_history_truncated",
                            f"API returned {len(rd)} rows but DistNo max={max_no}; older distributions missing",
                            now_iso(),
                        )
                    )
            for r in rd:
                ex = (r.get("DistDate") or "")[:10]
                if not ex:
                    continue
                sen = float(r.get("GrossDistSen") or 0)
                rows.append(
                    {
                        "fund_code": code,
                        "ex_date": ex,
                        "declare_date": (r.get("DeclareDate") or "")[:10] or None,
                        "amount_per_unit_sen": sen,
                        "amount_per_unit_rm": sen / 100.0,
                        "dist_type": r.get("DistType"),
                        "income_pct": r.get("Income"),
                        "capital_pct": r.get("Capital"),
                        "for_year_end": r.get("ForYearEnd"),
                        "dist_no": r.get("DistNo"),
                        "unit_split": None,  # not published in this API
                        "source": DIST_SRC + f" / GetFundDistributionHistory?schemeCode={code}",
                    }
                )
            print(f"  [{i}/{len(funds)}] dist {code} n={len(rd)}", flush=True)
        except Exception as e:
            gaps.append((code, "dist_error", str(e), now_iso()))
            print(f"  [{i}/{len(funds)}] DIST FAIL {code}: {e}", flush=True)
    return rows


def fetch_monthend_nav_snapshots(client: PMClient, gaps: list) -> list[tuple]:
    """
    Optional bulk month-end NAV via date API for UT+PRS.
    Steps back up to 7 days if a calendar month-end has no record.
    """
    months = month_ends_in_range(WINDOW_START, WINDOW_END)
    rows = []
    for me in months:
        got = False
        for cand in step_back_business_days(me, 7):
            dparam = price_date(cand)
            try:
                ut = client.latest_ut_prices(dparam)
                prs = client.latest_prs_prices(dparam)
            except Exception as e:
                gaps.append((None, "nav_snapshot_error", f"{cand}: {e}", now_iso()))
                continue
            ut_rd = ut.get("ResultData") if ut.get("ResponseCode") == 1 else None
            prs_rd = prs.get("ResultData") if prs.get("ResponseCode") == 1 else None
            if not ut_rd and not prs_rd:
                continue
            for r in ut_rd or []:
                rows.append(
                    (
                        r["FundCode"],
                        (r.get("Date") or cand)[:10],
                        float(r["Nav"]),
                        PRICE_UT + f" date={dparam}",
                    )
                )
            for r in prs_rd or []:
                rows.append(
                    (
                        r["FundCode"],
                        (r.get("Date") or cand)[:10],
                        float(r["Nav"]),
                        PRICE_PRS + f" date={dparam}",
                    )
                )
            print(f"  NAV snapshot {me} using {cand}: UT={len(ut_rd or [])} PRS={len(prs_rd or [])}", flush=True)
            got = True
            break
        if not got:
            gaps.append((None, "nav_month_missing", f"No UT/PRS prices near {me}", now_iso()))
            print(f"  NAV snapshot MISSING {me}", flush=True)
    return rows


def persist(
    funds: list[dict],
    perf: dict[str, list[dict]],
    dists: list[dict],
    nav_snap_rows: list[tuple],
    gaps: list,
):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    started = now_iso()
    conn.execute(
        "INSERT INTO etl_runs(started_at, notes) VALUES (?,?)",
        (started, "full rebuild"),
    )
    conn.commit()

    fund_rows = []
    for f in funds:
        fund_rows.append(
            (
                f["fund_code"],
                f.get("name"),
                f.get("abbr"),
                f.get("fund_type") or "UT",
                f.get("category"),
                f.get("is_islamic"),
                f.get("foreign_exposure"),
                f.get("dist_policy"),
                f.get("launch_date"),
                f.get("risk_level"),
                f.get("currency") or "MYR",
                PERF_SRC,
                now_iso(),
            )
        )
    upsert_many(
        conn,
        """INSERT OR REPLACE INTO funds(
            fund_code,name,abbr,fund_type,category,is_islamic,foreign_exposure,
            dist_policy,launch_date,risk_level,currency,source_url,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        fund_rows,
    )

    # NAV from chart series
    nav_rows = []
    for code, series in perf.items():
        for pt in series:
            if pt.get("Nav") is None:
                continue
            nav_rows.append(
                (code, pt["Date"][:10], float(pt["Nav"]), PERF_SRC + " FundIndex.Nav")
            )
    # also month-end snapshots (may duplicate; PK keeps one — prefer chart if same date by inserting chart first then snapshots with INSERT OR IGNORE)
    upsert_many(
        conn,
        "INSERT OR REPLACE INTO nav_daily(fund_code,date,nav,source) VALUES (?,?,?,?)",
        nav_rows,
    )
    if nav_snap_rows:
        upsert_many(
            conn,
            "INSERT OR IGNORE INTO nav_daily(fund_code,date,nav,source) VALUES (?,?,?,?)",
            nav_snap_rows,
        )

    dist_rows = [
        (
            d["fund_code"],
            d["ex_date"],
            d.get("declare_date"),
            d["amount_per_unit_sen"],
            d["amount_per_unit_rm"],
            d.get("dist_type"),
            d.get("income_pct"),
            d.get("capital_pct"),
            d.get("for_year_end"),
            d.get("dist_no"),
            d.get("unit_split"),
            d["source"],
        )
        for d in dists
    ]
    upsert_many(
        conn,
        """INSERT OR REPLACE INTO distributions(
            fund_code,ex_date,declare_date,amount_per_unit_sen,amount_per_unit_rm,
            dist_type,income_pct,capital_pct,for_year_end,dist_no,unit_split,source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        dist_rows,
    )

    months = month_ends_in_range(WINDOW_START, WINDOW_END)
    mr_rows = []
    ann_rows = []
    as_of = WINDOW_END
    for code, series in perf.items():
        monthly = build_monthly_from_chart(
            code, series, months, source=PERF_SRC + " FundIndex.Scope"
        )
        for m in monthly:
            mr_rows.append(
                (
                    m["fund_code"],
                    m["month_end"],
                    m["nav"],
                    m["tr_index"],
                    m["price_return_pct"],
                    m["total_return_pct"],
                    m["cumulative_tr_pct"],
                    m["source"],
                )
            )
        # annualised 1/3/5y as of last chart point
        last = sorted(series, key=lambda x: x["Date"])[-1]["Date"][:10]
        for yrs in (1, 3, 5):
            a = annualised_from_series(series, last, yrs)
            if not a:
                gaps.append(
                    (code, f"annualised_{yrs}y_unavailable", f"as_of={last}", now_iso())
                )
                continue
            ann_rows.append(
                (
                    code,
                    a["as_of"],
                    a["years"],
                    a["total_return_pct"],
                    a["arithmetic_avg_annual_pct"],
                    a["cagr_pct"],
                    PERF_SRC + " FundIndex.Scope rebased",
                )
            )

    upsert_many(
        conn,
        """INSERT OR REPLACE INTO monthly_returns(
            fund_code,month_end,nav,tr_index,price_return_pct,total_return_pct,
            cumulative_tr_pct,source
        ) VALUES (?,?,?,?,?,?,?,?)""",
        mr_rows,
    )
    upsert_many(
        conn,
        """INSERT OR REPLACE INTO annualised_returns(
            fund_code,as_of,years,total_return_pct,arithmetic_avg_annual_pct,cagr_pct,source
        ) VALUES (?,?,?,?,?,?,?)""",
        ann_rows,
    )

    if gaps:
        upsert_many(
            conn,
            "INSERT INTO data_gaps(fund_code,gap_type,detail,recorded_at) VALUES (?,?,?,?)",
            gaps,
        )

    conn.execute(
        "UPDATE etl_runs SET finished_at=?, notes=? WHERE id=(SELECT MAX(id) FROM etl_runs)",
        (
            now_iso(),
            f"funds={len(funds)} perf={len(perf)} dist={len(dists)} monthly={len(mr_rows)} ann={len(ann_rows)} gaps={len(gaps)}",
        ),
    )
    conn.commit()
    conn.close()
    print(
        f"DB written {DB_PATH}: funds={len(funds)} perf_series={len(perf)} "
        f"nav_chart={len(nav_rows)} nav_snap={len(nav_snap_rows)} dist={len(dists)} "
        f"monthly={len(mr_rows)} ann={len(ann_rows)} gaps={len(gaps)}",
        flush=True,
    )


def sanity_checks():
    """Compare a few funds vs published PHS numbers; print report."""
    client = PMClient()
    checks = []
    # PIMMF FYE 30 Jun 2026: published 1y 3.07, 3y 3.36, 5y 3.01 (arithmetic avg annual)
    cases = [
        {
            "code": "36",
            "name": "PIMMF-A",
            "end": "30-Jun-2026",
            "start_5": "30-Jun-2021",
            "published": {1: 3.07, 3: 3.36, 5: 3.01},
            "note": "PHS average annual returns ended 30 Jun 2026",
        },
        {
            "code": "03",
            "name": "PIX",
            "end": "31-Jan-2026",
            "start_5": "31-Jan-2021",
            "published": {1: 15.68, 3: 8.48, 5: 5.32},
            "note": "PHS average annual returns ended 31 Jan 2026",
        },
        {
            "code": "14",
            "name": "PIN BOND",
            "end": "30-Apr-2026",
            "start_5": "30-Apr-2021",
            "published": {1: 3.17, 3: 3.98, 5: 3.50},
            "note": "PHS average annual returns ended 30 Apr 2026",
        },
    ]
    for c in cases:
        try:
            data = client.performance_chart(c["code"], c["start_5"], c["end"])
            series = (data.get("ResultData") or {}).get("FundIndex") or []
            if not series:
                checks.append({**c, "error": "no series"})
                continue
            row = {"code": c["code"], "name": c["name"], "note": c["note"], "pairs": []}
            last = series[-1]["Date"][:10]
            for yrs, pub in c["published"].items():
                a = annualised_from_series(series, last, yrs)
                if not a:
                    row["pairs"].append({"years": yrs, "published": pub, "ours": None})
                else:
                    row["pairs"].append(
                        {
                            "years": yrs,
                            "published_arith": pub,
                            "ours_arith": a["arithmetic_avg_annual_pct"],
                            "ours_cagr": a["cagr_pct"],
                            "ours_total": a["total_return_pct"],
                            "diff_arith": round(a["arithmetic_avg_annual_pct"] - pub, 4),
                        }
                    )
            checks.append(row)
        except Exception as e:
            checks.append({**c, "error": str(e)})
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Ignore HTTP cache")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--skip-nav-snapshots", action="store_true", help="Skip bulk month-end price API (faster)")
    ap.add_argument("--sanity-only", action="store_true")
    ap.add_argument("--holdings", action="store_true", help="Only refresh EPF status + top-5 holdings, then re-export xlsx")
    ap.add_argument("--skip-holdings", action="store_true", help="Skip EPF/holdings enrichment on a full run")
    args = ap.parse_args()

    if args.sanity_only:
        print(json.dumps(sanity_checks(), indent=2))
        return

    if args.export_only:
        export_xlsx(DB_PATH, XLSX_PATH)
        print(f"Exported {XLSX_PATH}")
        return

    if args.holdings:
        print("== Holdings / EPF only ==", flush=True)
        run_holdings_and_epf(force=args.force)
        export_xlsx(DB_PATH, XLSX_PATH)
        print(f"Done. DB={DB_PATH} XLSX={XLSX_PATH}", flush=True)
        return

    if args.force:
        # clear caches
        import shutil

        for sub in ("perf", "dist", "fundinfo", "nav", "reports"):
            p = CACHE_DIR / sub
            if p.exists():
                shutil.rmtree(p)
                p.mkdir(parents=True, exist_ok=True)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = PMClient()
    gaps: list = []

    print("== 1. Universe ==", flush=True)
    funds, prs_codes, _ = load_universe(client)
    print(f"Funds: {len(funds)} (PRS codes from price list: {len(prs_codes)})", flush=True)

    print("== 2. Fund info ==", flush=True)
    enrich_fund_info(client, funds, gaps)

    print("== 3. Performance charts (primary TR) ==", flush=True)
    perf = fetch_performance(client, funds, gaps)

    print("== 4. Distributions ==", flush=True)
    dists = fetch_distributions(client, funds, gaps)

    nav_snaps = []
    if not args.skip_nav_snapshots:
        print("== 5. Month-end NAV snapshots (price API) ==", flush=True)
        nav_snaps = fetch_monthend_nav_snapshots(client, gaps)
    else:
        print("== 5. Skipping month-end NAV snapshots ==", flush=True)

    print("== 6. Persist SQLite ==", flush=True)
    persist(funds, perf, dists, nav_snaps, gaps)

    if not args.skip_holdings:
        print("== 6b. EPF status and top-5 holdings ==", flush=True)
        run_holdings_and_epf(force=args.force)
    else:
        print("== 6b. Skipping EPF/holdings ==", flush=True)

    print("== 7. Export xlsx ==", flush=True)
    export_xlsx(DB_PATH, XLSX_PATH)

    print("== 8. Sanity checks ==", flush=True)
    checks = sanity_checks()
    sanity_path = Path(__file__).resolve().parent / "logs" / "sanity_checks.json"
    sanity_path.parent.mkdir(parents=True, exist_ok=True)
    sanity_path.write_text(json.dumps(checks, indent=2))
    print(json.dumps(checks, indent=2))
    print(f"Done. DB={DB_PATH} XLSX={XLSX_PATH}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
