"""Enrich funds with EPF/KWSP MIS eligibility and top-5 holdings."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_PATH
from client import PMClient
from db import connect
from parse_holdings import parse_report

MYT = ZoneInfo("Asia/Kuala_Lumpur")
EXPLORER_URL = "https://www.publicmutual.com.my/Fund-Explorer-List"
PM_EPF_LIST = "https://www.publicmutual.com.my/pmb/EPF-Investment-Scheme-List"
KWSP_PDF = "https://www.kwsp.gov.my/documents/d/guest/pb-mutual-english"
UT_REPORTS = "https://www.publicmutual.com.my/funds-report-ut"
PRS_REPORTS = "https://www.publicmutual.com.my/funds-report-prs"


def now_iso() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S %Z")


def _parse_report_date(s: str):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    return None


def pick_latest_english_report(reports: list[dict]) -> dict | None:
    best = None
    best_d = None
    for r in reports or []:
        if not r.get("E_DocId"):
            continue
        d = _parse_report_date(r.get("Date") or "")
        if not d:
            continue
        if best_d is None or d > best_d:
            best, best_d = r, d
    return best


def format_top_holdings(top5: list[tuple[str, float]]) -> str:
    return "; ".join(f"{n} ({p:.1f}%)" for n, p in top5)


def apply_epf_status(client: PMClient, conn, gaps: list) -> dict:
    data = client.fund_explorer_data("")
    rd = data.get("ResultData") or []
    if data.get("ResponseCode") != 1 or not rd:
        gaps.append((None, "epf_explorer_empty", json.dumps({"RC": data.get("ResponseCode"), "Message": data.get("Message")})[:300], now_iso()))
        return {}

    epf_map = {}
    for r in rd:
        code = str(r["FundCode"])
        is_prs = (r.get("FundType") or "").upper() == "PRS"
        flag = r.get("IsEpfQualified")
        if is_prs:
            status = "N/A (PRS)"
            src = (
                f"{EXPLORER_URL} (IsEpfQualified={flag}); "
                "PRS is not part of the EPF Members Investment Scheme (i-Invest / EPF-MIS)"
            )
        elif flag is True:
            status = "Yes"
            src = (
                f"{EXPLORER_URL} IsEpfQualified=true; "
                f"also listed on {PM_EPF_LIST} (effective 1 April 2024, subject to KWSP annual review); "
                f"KWSP 2024/2025 active list {KWSP_PDF}"
            )
        elif flag is False:
            status = "No"
            src = (
                f"{EXPLORER_URL} IsEpfQualified=false "
                f"(not on Public Mutual EPF-qualified list {PM_EPF_LIST})"
            )
        else:
            status = None
            src = f"{EXPLORER_URL} IsEpfQualified={flag!r} (unrecognised)"
            gaps.append((code, "epf_flag_unrecognised", str(flag), now_iso()))
        epf_map[code] = (status, src)
        conn.execute(
            "UPDATE funds SET epf_approved=?, epf_source=? WHERE fund_code=?",
            (status, src, code),
        )

    # Funds in DB but not in explorer
    db_codes = [r[0] for r in conn.execute("SELECT fund_code, fund_type FROM funds")]
    types = {r[0]: r[1] for r in conn.execute("SELECT fund_code, fund_type FROM funds")}
    for code, ftype in types.items():
        if code in epf_map:
            continue
        if ftype == "PRS":
            status = "N/A (PRS)"
            src = "Fund type is PRS; not under EPF-MIS. Not present in Fund Explorer list."
        else:
            status = None
            src = None
            gaps.append(
                (
                    code,
                    "epf_not_in_explorer",
                    "UT fund missing from Fund Explorer; EPF status left blank (not assumed No)",
                    now_iso(),
                )
            )
        conn.execute(
            "UPDATE funds SET epf_approved=?, epf_source=? WHERE fund_code=?",
            (status, src, code),
        )
    conn.commit()
    return epf_map


def enrich_holdings(client: PMClient, conn, gaps: list, force: bool = False):
    funds = list(conn.execute("SELECT fund_code, name, abbr, fund_type FROM funds ORDER BY fund_type, name"))
    n = len(funds)
    ok = 0
    for i, f in enumerate(funds, 1):
        code, name, abbr, ftype = f["fund_code"], f["name"], f["abbr"], f["fund_type"]
        try:
            reports_json = client.fund_reports(code, ftype)
            reports = reports_json.get("ResultData") or []
            if reports_json.get("ResponseCode") != 1 or not reports:
                gaps.append((code, "holdings_no_reports", reports_json.get("Message") or "no reports", now_iso()))
                print(f"  [{i}/{n}] {code} {abbr} no reports", flush=True)
                continue
            picked = pick_latest_english_report(reports)
            if not picked:
                gaps.append((code, "holdings_no_english_report", json.dumps(reports)[:400], now_iso()))
                print(f"  [{i}/{n}] {code} {abbr} no EN report", flush=True)
                continue
            doc_id = picked["E_DocId"]
            pdf = client.download_report_pdf(doc_id, ftype, force=force)
            parsed = parse_report(pdf, picked.get("Date"))
            top5 = parsed.get("top5") or []
            if not top5:
                gaps.append(
                    (
                        code,
                        "holdings_parse_empty",
                        f"report {picked.get('Type')} {picked.get('Date')} method={parsed.get('method')} n={parsed.get('n_parsed')}",
                        now_iso(),
                    )
                )
                print(f"  [{i}/{n}] {code} {abbr} PARSE EMPTY {picked.get('Type')} {picked.get('Date')}", flush=True)
                # still store as-at and source so the gap is visible
            as_at = parsed.get("as_at")
            page = PRS_REPORTS if ftype == "PRS" else UT_REPORTS
            src = (
                f"{page} {picked.get('Type')} report dated {picked.get('Date')} "
                f"(parsed {parsed.get('method')}; docId={doc_id})"
            )
            fields = {
                "top1_name": None, "top1_pct": None,
                "top2_name": None, "top2_pct": None,
                "top3_name": None, "top3_pct": None,
                "top4_name": None, "top4_pct": None,
                "top5_name": None, "top5_pct": None,
            }
            for idx, (hname, pct) in enumerate(top5, 1):
                fields[f"top{idx}_name"] = hname
                fields[f"top{idx}_pct"] = pct
            top_text = format_top_holdings(top5) if top5 else None
            conn.execute(
                """UPDATE funds SET
                    top1_name=?, top1_pct=?, top2_name=?, top2_pct=?,
                    top3_name=?, top3_pct=?, top4_name=?, top4_pct=?,
                    top5_name=?, top5_pct=?, top_holdings=?,
                    holdings_as_at=?, holdings_source=?, holdings_report_type=?
                WHERE fund_code=?""",
                (
                    fields["top1_name"], fields["top1_pct"],
                    fields["top2_name"], fields["top2_pct"],
                    fields["top3_name"], fields["top3_pct"],
                    fields["top4_name"], fields["top4_pct"],
                    fields["top5_name"], fields["top5_pct"],
                    top_text, as_at, src, picked.get("Type"),
                    code,
                ),
            )
            conn.commit()
            if top5:
                ok += 1
            print(
                f"  [{i}/{n}] {code} {abbr} {picked.get('Type')} {as_at} "
                f"{parsed.get('method')} n={parsed.get('n_parsed')} top={top_text}",
                flush=True,
            )
        except Exception as e:
            gaps.append((code, "holdings_error", str(e)[:500], now_iso()))
            print(f"  [{i}/{n}] {code} {abbr} FAIL {e}", flush=True)
    return ok


def run_holdings_and_epf(force: bool = False) -> dict:
    client = PMClient()
    conn = connect(DB_PATH)
    gaps: list = []

    print("== EPF / KWSP MIS status ==", flush=True)
    apply_epf_status(client, conn, gaps)

    print("== Top 5 holdings from latest EN fund reports ==", flush=True)
    ok = enrich_holdings(client, conn, gaps, force=force)

    if gaps:
        conn.executemany(
            "INSERT INTO data_gaps(fund_code,gap_type,detail,recorded_at) VALUES (?,?,?,?)",
            gaps,
        )
        conn.commit()

    stats = dict(
        conn.execute(
            """
            SELECT
              SUM(CASE WHEN epf_approved='Yes' THEN 1 ELSE 0 END) epf_yes,
              SUM(CASE WHEN epf_approved='No' THEN 1 ELSE 0 END) epf_no,
              SUM(CASE WHEN epf_approved LIKE 'N/A%' THEN 1 ELSE 0 END) epf_na,
              SUM(CASE WHEN epf_approved IS NULL THEN 1 ELSE 0 END) epf_blank,
              SUM(CASE WHEN top1_name IS NOT NULL THEN 1 ELSE 0 END) with_holdings,
              MIN(holdings_as_at) holdings_from,
              MAX(holdings_as_at) holdings_to,
              COUNT(*) funds
            FROM funds
            """
        ).fetchone()
    )
    print("Coverage", stats, "new_gaps", len(gaps), "parsed_ok", ok, flush=True)
    conn.close()
    return {"stats": stats, "gaps": len(gaps), "parsed_ok": ok}
