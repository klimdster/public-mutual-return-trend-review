"""Public Mutual ETL configuration."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
DB_PATH = BASE_DIR / "output" / "pm_funds.db"
XLSX_PATH = BASE_DIR / "output" / "pm_5y_total_returns.xlsx"

BASE_URL = "https://www.publicmutual.com.my"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Throttle between HTTP calls (seconds)
THROTTLE_SEC = 1.2

# Analysis window (month-end TR series)
WINDOW_START = "2021-11-01"   # ~5y lookback from Oct 2026
WINDOW_END = "2026-10-31"

# Chart API date format
def chart_date(iso_yyyy_mm_dd: str) -> str:
    from datetime import datetime
    dt = datetime.strptime(iso_yyyy_mm_dd, "%Y-%m-%d")
    return dt.strftime("%d-%b-%Y")  # 01-Nov-2021

# Price API date format
def price_date(iso_yyyy_mm_dd: str) -> str:
    from datetime import datetime
    dt = datetime.strptime(iso_yyyy_mm_dd, "%Y-%m-%d")
    return dt.strftime("%d-%m-%Y")  # 01-11-2021

(BASE_DIR / "output").mkdir(parents=True, exist_ok=True)
