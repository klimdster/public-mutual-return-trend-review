"""Export wide TR-index sheet + 1/3/5y annualised returns."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


def export(db_path: Path, xlsx_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    funds = list(conn.execute(
        "SELECT fund_code, name, abbr, fund_type, category, is_islamic FROM funds ORDER BY fund_type, name"
    ))
    months = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT month_end FROM monthly_returns ORDER BY month_end"
        )
    ]

    # wide matrix fund x month
    tr = {}
    for r in conn.execute(
        "SELECT fund_code, month_end, tr_index FROM monthly_returns"
    ):
        tr[(r["fund_code"], r["month_end"])] = r["tr_index"]

    ann = {}
    for r in conn.execute(
        "SELECT fund_code, years, arithmetic_avg_annual_pct, cagr_pct, total_return_pct, as_of FROM annualised_returns"
    ):
        ann[(r["fund_code"], r["years"])] = r

    wb = Workbook()

    # ---- Summary ----
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Public Mutual 5-year distribution-inclusive (total) returns"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Primary source"
    ws["B2"] = "publicmutual.com.my Fund Overview performance chart (Lipper NAV-to-NAV total return, Scope)"
    ws["A3"] = "Distributions source"
    ws["B3"] = "FundDistribution/GetFundDistributionHistory (last ~5 records per fund; incomplete for monthly distributors)"
    ws["A4"] = "Funds"
    ws["B4"] = len(funds)
    ws["A5"] = "Month-ends"
    ws["B5"] = len(months)
    if months:
        ws["A6"] = "Window"
        ws["B6"] = f"{months[0]} to {months[-1]}"
    n_mr = conn.execute("SELECT COUNT(*) FROM monthly_returns").fetchone()[0]
    n_dist = conn.execute("SELECT COUNT(*) FROM distributions").fetchone()[0]
    n_nav = conn.execute("SELECT COUNT(*) FROM nav_daily").fetchone()[0]
    ws["A7"] = "monthly_returns rows"
    ws["B7"] = n_mr
    ws["A8"] = "distribution rows"
    ws["B8"] = n_dist
    ws["A9"] = "nav_daily rows"
    ws["B9"] = n_nav
    ws["A11"] = "Notes"
    ws["A12"] = (
        "TR index is rebased to 100 at each fund's first available month-end in the window. "
        "PHS 'Average Annual Returns' ≈ arithmetic (total return / years), not CAGR. "
        "DIY reinvestment TR is not used as primary because distribution API truncates history."
    )
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 100

    # ---- TR Index wide ----
    ws2 = wb.create_sheet("TR_Index_Monthly")
    header = ["fund_code", "abbr", "name", "fund_type", "category"] + months
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    for col, h in enumerate(header, 1):
        cell = ws2.cell(1, col, h)
        cell.fill = header_fill
        cell.font = header_font
    for i, f in enumerate(funds, 2):
        ws2.cell(i, 1, f["fund_code"])
        ws2.cell(i, 2, f["abbr"])
        ws2.cell(i, 3, f["name"])
        ws2.cell(i, 4, f["fund_type"])
        ws2.cell(i, 5, f["category"])
        for j, m in enumerate(months, 6):
            val = tr.get((f["fund_code"], m))
            if val is not None:
                cell = ws2.cell(i, j, val)
                cell.number_format = "0.00"
    ws2.freeze_panes = "F2"
    ws2.auto_filter.ref = ws2.dimensions

    # ---- Annualised ----
    ws3 = wb.create_sheet("Annualised_1_3_5Y")
    h3 = [
        "fund_code", "abbr", "name", "fund_type", "category", "as_of",
        "1y_total_pct", "1y_arith_ann_pct", "1y_cagr_pct",
        "3y_total_pct", "3y_arith_ann_pct", "3y_cagr_pct",
        "5y_total_pct", "5y_arith_ann_pct", "5y_cagr_pct",
    ]
    for col, h in enumerate(h3, 1):
        cell = ws3.cell(1, col, h)
        cell.fill = header_fill
        cell.font = header_font
    for i, f in enumerate(funds, 2):
        ws3.cell(i, 1, f["fund_code"])
        ws3.cell(i, 2, f["abbr"])
        ws3.cell(i, 3, f["name"])
        ws3.cell(i, 4, f["fund_type"])
        ws3.cell(i, 5, f["category"])
        as_of = None
        for yrs, cols in [(1, (6, 7, 8)), (3, (9, 10, 11)), (5, (12, 13, 14))]:
            # columns offset: as_of is col 6, then triples starting col 7
            pass
        # rewrite cleanly
    # clearer rewrite of annualised rows
    ws3.delete_rows(2, ws3.max_row)
    for i, f in enumerate(funds, 2):
        ws3.cell(i, 1, f["fund_code"])
        ws3.cell(i, 2, f["abbr"])
        ws3.cell(i, 3, f["name"])
        ws3.cell(i, 4, f["fund_type"])
        ws3.cell(i, 5, f["category"])
        a1 = ann.get((f["fund_code"], 1))
        a3 = ann.get((f["fund_code"], 3))
        a5 = ann.get((f["fund_code"], 5))
        as_of = (a1 or a3 or a5)["as_of"] if (a1 or a3 or a5) else None
        ws3.cell(i, 6, as_of)
        for yrs, base_col in [(1, 7), (3, 10), (5, 13)]:
            a = ann.get((f["fund_code"], yrs))
            if not a:
                continue
            ws3.cell(i, base_col, a["total_return_pct"]).number_format = "0.00"
            ws3.cell(i, base_col + 1, a["arithmetic_avg_annual_pct"]).number_format = "0.00"
            ws3.cell(i, base_col + 2, a["cagr_pct"]).number_format = "0.00"
    ws3.freeze_panes = "G2"
    ws3.auto_filter.ref = ws3.dimensions

    # ---- Distributions ----
    ws4 = wb.create_sheet("Distributions")
    h4 = [
        "fund_code", "abbr", "ex_date", "declare_date", "amount_sen", "amount_rm",
        "dist_type", "income_pct", "capital_pct", "for_year_end", "dist_no", "unit_split", "source",
    ]
    for col, h in enumerate(h4, 1):
        cell = ws4.cell(1, col, h)
        cell.fill = header_fill
        cell.font = header_font
    abbr = {f["fund_code"]: f["abbr"] for f in funds}
    for i, r in enumerate(
        conn.execute(
            "SELECT * FROM distributions ORDER BY fund_code, ex_date DESC"
        ),
        2,
    ):
        ws4.cell(i, 1, r["fund_code"])
        ws4.cell(i, 2, abbr.get(r["fund_code"]))
        ws4.cell(i, 3, r["ex_date"])
        ws4.cell(i, 4, r["declare_date"])
        ws4.cell(i, 5, r["amount_per_unit_sen"])
        ws4.cell(i, 6, r["amount_per_unit_rm"])
        ws4.cell(i, 7, r["dist_type"])
        ws4.cell(i, 8, r["income_pct"])
        ws4.cell(i, 9, r["capital_pct"])
        ws4.cell(i, 10, r["for_year_end"])
        ws4.cell(i, 11, r["dist_no"])
        ws4.cell(i, 12, r["unit_split"])
        ws4.cell(i, 13, r["source"])
    ws4.freeze_panes = "A2"
    ws4.auto_filter.ref = ws4.dimensions

    # ---- Gaps ----
    ws5 = wb.create_sheet("Data_Gaps")
    for col, h in enumerate(["fund_code", "gap_type", "detail", "recorded_at"], 1):
        cell = ws5.cell(1, col, h)
        cell.fill = header_fill
        cell.font = header_font
    for i, r in enumerate(conn.execute("SELECT fund_code, gap_type, detail, recorded_at FROM data_gaps"), 2):
        ws5.cell(i, 1, r["fund_code"])
        ws5.cell(i, 2, r["gap_type"])
        ws5.cell(i, 3, r["detail"])
        ws5.cell(i, 4, r["recorded_at"])

    for sheet in wb.worksheets:
        for col in range(1, min(sheet.max_column, 20) + 1):
            sheet.column_dimensions[get_column_letter(col)].width = 14
        if sheet.title.startswith("TR"):
            sheet.column_dimensions["C"].width = 40
        if sheet.title.startswith("Annualised"):
            sheet.column_dimensions["C"].width = 40

    wb.save(xlsx_path)
    conn.close()
