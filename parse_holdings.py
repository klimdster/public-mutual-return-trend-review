"""Parse top holdings and as-at dates from Public Mutual annual/semi-annual report PDFs."""
from __future__ import annotations

import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

MONTHS = {
    "JANUARY": 1, "JAN": 1, "FEBRUARY": 2, "FEB": 2, "MARCH": 3, "MAR": 3,
    "APRIL": 4, "APR": 4, "MAY": 5, "JUNE": 6, "JUN": 6, "JULY": 7, "JUL": 7,
    "AUGUST": 8, "AUG": 8, "SEPTEMBER": 9, "SEP": 9, "SEPT": 9,
    "OCTOBER": 10, "OCT": 10, "NOVEMBER": 11, "NOV": 11, "DECEMBER": 12, "DEC": 12,
}

SKIP_NAME = re.compile(
    r"(?i)^(total|sub-?total|equity securities|collective investment|"
    r"fixed income|unquoted|quoted|income securities|redeemable|"
    r"convertible|ringgit-?denominated|us dollar|outside malaysia|"
    r"malaysian government|deposits with|financial institutions|"
    r"basic materials|communications|consumer|diversified|financial|"
    r"industrial|utilities|technology|energy|health|real estate|"
    r"notes to the|statement of|percent of nav|nominal|aggregate|"
    r"fair value|myr.?000|page |\d+\s*[•·]|cont.?d|fund$|"
    r"the equity securities|categorised|bloomberg|assets and liabilities|"
    r"cash and cash|as at |for the financial|government securities|"
    r"sovereign|private debt|sukuk|bonds?$|commercial paper|"
    r"placement|licen[sc]ed|effective yield|jumlah|keseluruhan|jumlah keseluruhan|limited$|berhad$|holdings berhad$|inc\.?$|corp\.?$|plc$|by the prs provider|by the manager|managed by)"
)

PAGE_OR_HEADER = re.compile(
    r"(?i)(•\s*\w|\w+\s*•|notes to the financial|statement of assets|"
    r"manager.?s report|extracts of financial|4\.\s*investments|"
    r"nominal\s+aggregate|fair\s+percent|myr.?000)"
)

EQUITY_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .,&'’()/%+\-]+?)\s+(-?[\d,]{2,})\s+(\d+\.\d+)\s*$"
)
BOND_LINE = re.compile(
    r"^\s*(.*?)\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(\d+\.\d+)\s*$"
)
DATE_INVEST = re.compile(
    r"INVESTMENTS\s*[-–]\s*(\d{1,2}\s+[A-Z]+\s+\d{4})", re.I
)
DATE_ASAT = re.compile(
    r"as at\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.I
)


def parse_date(text: str) -> str | None:
    text = text.strip()
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).upper().rstrip("."), int(m.group(3))
    month = MONTHS.get(mon)
    if not month:
        return None
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def extract_facing_pages(pdf_path: Path) -> list[str]:
    """Public Mutual reports draw two booklet pages; MediaBox is only the left half."""
    chunks = []
    for x in (0, 283):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            out = tmp.name
        try:
            subprocess.check_call(
                [
                    "pdftotext", "-layout",
                    "-x", str(x), "-y", "0", "-W", "283", "-H", "620",
                    str(pdf_path), out,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            chunks.append(Path(out).read_text(encoding="utf-8", errors="replace"))
        finally:
            Path(out).unlink(missing_ok=True)
    return chunks


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" -–,")
    name = re.sub(r"\(related party\)", "", name, flags=re.I).strip()
    return name


def _strip_lead_in(name: str) -> str:
    name = re.sub(r"(?i)^(by the (?:PRS )?Provider|managed by the (?:PRS )?Provider|managed by)\s+", "", name).strip()
    return name


_SUFFIX_ONLY = re.compile(
    r"(?i)^(co\.,?\s*ltd\.?|ltd\.?|limited|berhad|bhd\.?|inc\.?|corp\.?|"
    r"plc|nv\.?|s[./]?a\.?|sendirian berhad|holdings berhad|"
    r"holdings limited|limited - foreign|- foreign)$"
)


def _is_junk_name(name: str) -> bool:
    if not name or len(name) < 3:
        return True
    if SKIP_NAME.search(name):
        return True
    if _SUFFIX_ONLY.match(name.strip()):
        return True
    if re.fullmatch(r"[\d,.\-/% *]+", name):
        return True
    if len(re.findall(r"[A-Za-z]", name)) < 3:
        return True
    if name.upper().startswith("JUMLAH") or name.upper().startswith("TOTAL"):
        return True
    # two holdings glued together from a two-column line
    if re.search(r"\d+\.\d+\s+[A-Z]", name):
        return True
    return False


def _section_after(text: str, start_pat: str, end_pats: list[str]) -> str:
    m = re.search(start_pat, text, re.I)
    if not m:
        return ""
    rest = text[m.start():]
    end = len(rest)
    for p in end_pats:
        em = re.search(p, rest[80:], re.I)  # skip the start heading itself
        if em:
            end = min(end, 80 + em.start())
    return rest[:end]


def parse_equity_investments(text: str) -> list[tuple[str, float]]:
    section = _section_after(
        text,
        r"INVESTMENTS\s*[-–]\s*\d",
        [
            r"\n\s*TOTAL\s+[\d,]+\s+\d+\.\d+",
            r"\n基金",  # Chinese block after EN
            r"\nStatement By Manager",
        ],
    )
    if not section:
        return []
    # include the TOTAL line area: cut after last holding before page footer noise
    rows = []
    pending: list[str] = []
    for line in section.splitlines():
        m = EQUITY_LINE.match(line)
        if m:
            prefix = m.group(1).strip()
            pct = float(m.group(3))
            parts = [p.strip() for p in pending if p.strip()]
            if prefix:
                parts.append(prefix)
            pending = []
            name = _clean_name(" ".join(parts))
            name = _strip_lead_in(name)
            if _is_junk_name(name) or name.upper().startswith("TOTAL") or name.upper().startswith("JUMLAH"):
                continue
            rows.append((name, pct))
            continue
        s = line.strip()
        if not s or PAGE_OR_HEADER.search(s) or SKIP_NAME.search(s):
            pending = []
            continue
        # wrapped issuer / security name
        pending.append(s)
        if len(pending) > 3:
            pending = pending[-3:]
    return rows


def parse_bond_note_investments(text: str) -> list[tuple[str, float]]:
    """Notes '4. Investments' tables: issuer + coupon line with 4 numeric columns."""
    section = _section_after(
        text,
        r"4\.\s*Investments",
        [r"\n5\.\s+", r"\nEffective yields", r"\nNet \(loss\)/gain from investments"],
    )
    if not section:
        # fall back to scanning whole text for the 4-number pattern
        section = text
    rows = []
    pending: list[str] = []
    for raw in section.splitlines():
        line = raw.rstrip()
        if PAGE_OR_HEADER.search(line) or re.search(r"(?i)cont.?d\)?\s*$", line.strip()):
            pending = []
            continue
        m = BOND_LINE.match(line)
        if m:
            prefix = m.group(1).strip()
            # require the last number to look like a % of NAV (not a year)
            pct = float(m.group(5))
            if pct > 100:  # not a NAV %
                pending = []
                continue
            parts = [p.strip() for p in pending if p.strip()]
            if prefix and not SKIP_NAME.search(prefix):
                parts.append(prefix)
            name = _strip_lead_in(_clean_name(" ".join(parts)))
            pending = []
            if _is_junk_name(name) or name.upper().startswith("JUMLAH"):
                continue
            # must look like a security (has a letter and usually % coupon or Berhad/Bank/Sukuk)
            rows.append((name, pct))
            continue
        s = line.strip()
        if not s:
            # don't wipe pending on blank; wrap continues
            continue
        if SKIP_NAME.search(s) and not re.search(r"\d+\.\d+\s*%\s*/", s):
            pending = []
            continue
        pending.append(s)
        # cap wrap
        if len(pending) > 4:
            pending = pending[-4:]
    return rows


def parse_top_exposures_table(text: str) -> list[tuple[str, float]]:
    """'Top 10 Equity Exposures of FUND vs BENCH as at ...' — use the fund % of NAV column."""
    m = re.search(
        r"Top\s+\d+\s+(?:Equity\s+)?Exposures?\s+of\s+\S+\s+vs\s+.+?as at\s+([^\n]+)",
        text,
        re.I | re.S,
    )
    if not m:
        return []
    # take a window after the heading
    start = m.start()
    window = text[start:start + 2500]
    # Lines often: "1 Malayan Banking Berhad  9.6 Tenaga Nasional Berhad  10.0"
    # After left/right split, may be just the fund column: "1 Tenaga Nasional Berhad  10.0"
    rows = []
    for line in window.splitlines():
        # ranked line starting with 1-10
        rm = re.match(
            r"^\s*(\d{1,2})\s+(.+?)\s+(\d+\.\d+)\s*$",
            line,
        )
        if rm and 1 <= int(rm.group(1)) <= 10:
            name = _clean_name(rm.group(2))
            # strip a leading benchmark leftover like "Malayan Banking Berhad  9.6 "
            # already single-column after split
            if _is_junk_name(name):
                continue
            # drop index weight prefix if still present: "Name  9.6 Name2"
            inner = re.match(r"^.+\s+\d+\.\d+\s+(.+)$", name)
            if inner:
                name = _clean_name(inner.group(1))
            rows.append((name, float(rm.group(3))))
    return rows


def parse_asset_allocation_aggregates(text: str) -> list[tuple[str, float]]:
    """For money-market funds that only publish a single placement/deposit line."""
    m = re.search(
        r"Asset Allocation for the Past Three Financial (?:Years|Periods).{0,400}?"
        r"\(Percent of NAV\)",
        text,
        re.I | re.S,
    )
    if not m:
        return []
    window = text[m.end():m.end() + 1500]
    rows = []
    pending = []
    for line in window.splitlines():
        # Name ... 101.4  101.6  102.8  (latest year first)
        mm = re.match(
            r"^\s*(.*?)\s+(-?\d+\.\d+)\s+-?\d+\.\d+\s+-?\d+\.\d+\s*$",
            line,
        )
        if mm:
            prefix = mm.group(1).strip()
            name = _clean_name(" ".join(pending + ([prefix] if prefix else [])))
            pending = []
            if re.search(r"(?i)other assets|liabilit|percent of nav|^as at |^total\b", name):
                continue
            name = re.sub(r"^[%\s]+", "", name).strip()
            if not name:
                continue
            rows.append((name, float(mm.group(2))))
            continue
        s = line.strip()
        if (not s or re.search(r"(?i)as at |percent of nav|^\d{4}\s+\d{4}|^[%\s]+$", s)
                or re.fullmatch(r"[\d.\s%]+", s)):
            pending = []
            continue
        pending.append(s)
    return rows


def infer_as_at(text: str, fallback_report_date: str | None = None) -> str | None:
    m = DATE_INVEST.search(text)
    if m:
        d = parse_date(m.group(1))
        if d:
            return d
    # "As at 31 July 2026, the top 10"
    for m in DATE_ASAT.finditer(text):
        d = parse_date(m.group(1))
        if d:
            return d
    if fallback_report_date:
        # 31/07/2026 or 2026-07-31
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(fallback_report_date[:10], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def top5_from_rows(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    # keep first occurrence; sort by pct desc
    seen = set()
    uniq = []
    for name, pct in rows:
        key = name.lower()
        if key in seen:
            # same security listed twice: keep larger
            continue
        seen.add(key)
        uniq.append((name, pct))
    uniq.sort(key=lambda x: (-x[1], x[0]))
    return uniq[:5]


def parse_report(pdf_path: Path, fallback_report_date: str | None = None) -> dict:
    # Parse left and right booklet pages separately so a "5." heading on
    # the left page cannot truncate holdings that continue on the right.
    chunks = extract_facing_pages(pdf_path)
    text = "\n\n".join(chunks)
    as_at = infer_as_at(text, fallback_report_date)

    equity, bond, top_tbl, alloc = [], [], [], []
    for chunk in chunks:
        equity.extend(parse_equity_investments(chunk))
        bond.extend(parse_bond_note_investments(chunk))
        top_tbl.extend(parse_top_exposures_table(chunk))
        alloc.extend(parse_asset_allocation_aggregates(chunk))

    # Prefer detailed INVESTMENTS / notes tables over the manager's top-10 graphic
    if len(equity) >= 3:
        chosen, method = equity, "investments_schedule"
    elif len(bond) >= 3:
        chosen, method = bond, "notes_investments"
    elif len(top_tbl) >= 3:
        chosen, method = top_tbl, "top_exposures_table"
    elif equity:
        chosen, method = equity, "investments_schedule"
    elif bond:
        chosen, method = bond, "notes_investments"
    elif top_tbl:
        chosen, method = top_tbl, "top_exposures_table"
    elif alloc:
        chosen, method = alloc, "asset_allocation_aggregate"
    else:
        chosen, method = [], "none"

    top5 = top5_from_rows(chosen)
    return {
        "as_at": as_at,
        "method": method,
        "top5": top5,
        "n_parsed": len(chosen),
    }
