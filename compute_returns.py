"""Derive month-end TR index / returns from daily FundIndex chart series."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def month_ends_in_range(start_iso: str, end_iso: str) -> list[str]:
    """Calendar month-end dates from start month through end month."""
    s = datetime.strptime(start_iso[:7] + "-01", "%Y-%m-%d")
    e = datetime.strptime(end_iso[:7] + "-01", "%Y-%m-%d")
    out = []
    cur = s
    while cur <= e:
        # last day of month
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        me = nxt - timedelta(days=1)
        out.append(me.strftime("%Y-%m-%d"))
        cur = nxt
    return out


def last_obs_on_or_before(series: list[dict], target_iso: str) -> Optional[dict]:
    """series items have Date (YYYY-MM-DD), Scope, Nav. Find last <= target."""
    best = None
    for row in series:
        d = row["Date"][:10]
        if d <= target_iso:
            best = row
        else:
            break
    return best


def build_monthly_from_chart(
    fund_code: str,
    series: list[dict],
    month_ends: list[str],
    source: str,
) -> list[dict]:
    """
    series Scope is cumulative total return % from chart start (0 at start).
    TR index = 100 * (1 + Scope/100), rebased so first available month = 100.
    """
    if not series:
        return []
    series = sorted(series, key=lambda x: x["Date"])

    points = []
    for me in month_ends:
        obs = last_obs_on_or_before(series, me)
        if not obs:
            continue
        # skip if observation is more than ~10 calendar days before month-end
        # (fund not yet launched / no data that month)
        obs_d = datetime.strptime(obs["Date"][:10], "%Y-%m-%d")
        me_d = datetime.strptime(me, "%Y-%m-%d")
        if (me_d - obs_d).days > 10:
            continue
        scope = float(obs["Scope"])
        nav = float(obs["Nav"]) if obs.get("Nav") is not None else None
        points.append(
            {
                "fund_code": fund_code,
                "month_end": me,
                "nav": nav,
                "scope": scope,
                "obs_date": obs["Date"][:10],
            }
        )

    if not points:
        return []

    base = 100.0 * (1.0 + points[0]["scope"] / 100.0)
    rows = []
    prev_tr = None
    prev_nav = None
    for p in points:
        tr_raw = 100.0 * (1.0 + p["scope"] / 100.0)
        tr_index = 100.0 * tr_raw / base
        if prev_tr is None:
            tr_pct = None
            px_pct = None
        else:
            tr_pct = (tr_index / prev_tr - 1.0) * 100.0
            if prev_nav and p["nav"]:
                px_pct = (p["nav"] / prev_nav - 1.0) * 100.0
            else:
                px_pct = None
        rows.append(
            {
                "fund_code": fund_code,
                "month_end": p["month_end"],
                "nav": p["nav"],
                "tr_index": round(tr_index, 6),
                "price_return_pct": None if px_pct is None else round(px_pct, 6),
                "total_return_pct": None if tr_pct is None else round(tr_pct, 6),
                "cumulative_tr_pct": round(p["scope"], 6),
                "source": source,
            }
        )
        prev_tr = tr_index
        prev_nav = p["nav"]
    return rows


def annualised_from_series(
    series: list[dict], as_of: str, years: int
) -> Optional[dict]:
    """
    Total return over `years` ending at as_of, using Scope rebase within series.
    Returns total, arithmetic avg (PHS-style), and CAGR.
    """
    if not series:
        return None
    series = sorted(series, key=lambda x: x["Date"])
    end = last_obs_on_or_before(series, as_of)
    if not end:
        return None
    end_d = datetime.strptime(end["Date"][:10], "%Y-%m-%d")
    # approximate start = end - years
    try:
        start_target = end_d.replace(year=end_d.year - years)
    except ValueError:
        start_target = end_d.replace(year=end_d.year - years, day=28)
    start = last_obs_on_or_before(series, start_target.strftime("%Y-%m-%d"))
    if not start:
        # series may begin a few days after exact anniversary (chart window)
        first = series[0]
        first_d = datetime.strptime(first["Date"][:10], "%Y-%m-%d")
        if abs((first_d - start_target).days) <= 14:
            start = first
        else:
            return None
    # require start close to target
    start_d = datetime.strptime(start["Date"][:10], "%Y-%m-%d")
    if abs((start_d - start_target).days) > 14:
        return None
    s0 = 1.0 + float(start["Scope"]) / 100.0
    s1 = 1.0 + float(end["Scope"]) / 100.0
    total = (s1 / s0 - 1.0) * 100.0
    yrs = max((end_d - start_d).days / 365.25, 0.01)
    cagr = ((1.0 + total / 100.0) ** (1.0 / yrs) - 1.0) * 100.0
    arith = total / years
    return {
        "as_of": end["Date"][:10],
        "years": years,
        "total_return_pct": round(total, 4),
        "arithmetic_avg_annual_pct": round(arith, 4),
        "cagr_pct": round(cagr, 4),
        "start_date": start["Date"][:10],
    }


def diy_tr_from_nav_and_dist(
    nav_by_date: dict[str, float],
    distributions: list[dict],
    month_ends: list[str],
) -> list[dict]:
    """
    DIY total-return index: on each ex-date, multiply factor = 1 + dist_rm / nav_ex.
    distributions: {ex_date, amount_per_unit_rm, unit_split}
    Returns monthly rows or empty if insufficient NAV coverage for dist dates.
    """
    if not nav_by_date:
        return []
    dates = sorted(nav_by_date.keys())
    if not dates:
        return []

    # daily TR walk over available NAV dates
    factor = 1.0
    tr_by_date = {}
    dist_by_date = {}
    for d in distributions:
        dist_by_date.setdefault(d["ex_date"], 0.0)
        dist_by_date[d["ex_date"]] += float(d["amount_per_unit_rm"])
        split = d.get("unit_split")
        if split and split not in (1, 1.0, None):
            # apply split as additional unit multiplication on ex-date
            dist_by_date[d["ex_date"]] += 0  # tracked separately below
    split_by_date = {}
    for d in distributions:
        sp = d.get("unit_split")
        if sp and sp not in (1, 1.0, None):
            split_by_date[d["ex_date"]] = float(sp)

    prev_nav = None
    units = 1.0  # synthetic units held, grows with reinvestment + splits
    for d in dates:
        nav = nav_by_date[d]
        if prev_nav is None:
            # start
            tr_by_date[d] = 100.0
            prev_nav = nav
            continue
        # price move on existing units
        value = units * nav
        # distribution reinvested at ex-date NAV (after drop)
        if d in dist_by_date and nav > 0:
            cash = units * dist_by_date[d]
            units += cash / nav
            value = units * nav
        if d in split_by_date:
            units *= split_by_date[d]
            value = units * nav
        # index relative to initial nav*1 unit
        tr_by_date[d] = 100.0 * value / (dates and nav_by_date[dates[0]] or nav)
        prev_nav = nav

    # month-ends
    rows = []
    prev_tr = None
    prev_nav_m = None
    for me in month_ends:
        # last nav date <= me
        obs = None
        for d in dates:
            if d <= me:
                obs = d
            else:
                break
        if not obs:
            continue
        od = datetime.strptime(obs, "%Y-%m-%d")
        md = datetime.strptime(me, "%Y-%m-%d")
        if (md - od).days > 10:
            continue
        tr = tr_by_date.get(obs)
        nav = nav_by_date[obs]
        if tr is None:
            continue
        if prev_tr is None:
            tr_pct = None
            px_pct = None
        else:
            tr_pct = (tr / prev_tr - 1.0) * 100.0
            px_pct = (nav / prev_nav_m - 1.0) * 100.0 if prev_nav_m else None
        rows.append(
            {
                "month_end": me,
                "nav": nav,
                "tr_index": round(tr, 6),
                "price_return_pct": None if px_pct is None else round(px_pct, 6),
                "total_return_pct": None if tr_pct is None else round(tr_pct, 6),
            }
        )
        prev_tr = tr
        prev_nav_m = nav
    return rows
