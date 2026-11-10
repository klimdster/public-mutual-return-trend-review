"""HTTP client for Public Mutual public APIs with cookie/token, throttle, cache."""
from __future__ import annotations

import json
import re
import time
import hashlib
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from config import BASE_URL, USER_AGENT, THROTTLE_SEC, CACHE_DIR


class PMClient:
    def __init__(self, throttle: float = THROTTLE_SEC):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.throttle = throttle
        self._last_call = 0.0
        self._tokens: dict[str, str] = {}  # page path -> token

    def _wait(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)
        self._last_call = time.time()

    def _ensure_token(self, page_path: str) -> str:
        if page_path in self._tokens:
            return self._tokens[page_path]
        self._wait()
        url = BASE_URL + page_path
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        m = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text
        )
        if not m:
            m = re.search(
                r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', r.text
            )
        if not m:
            raise RuntimeError(f"No antiforgery token on {page_path}")
        self._tokens[page_path] = m.group(1)
        return self._tokens[page_path]

    def _cache_path(self, prefix: str, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80]
        d = CACHE_DIR / prefix
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{safe}_{h}.json"

    def get_json(
        self,
        path: str,
        *,
        page_for_token: str,
        params: Optional[dict] = None,
        cache_prefix: Optional[str] = None,
        cache_key: Optional[str] = None,
        force: bool = False,
    ) -> Any:
        if cache_prefix and cache_key and not force:
            cp = self._cache_path(cache_prefix, cache_key)
            if cp.exists() and cp.stat().st_size > 0:
                return json.loads(cp.read_text())

        token = self._ensure_token(page_for_token)
        self._wait()
        url = BASE_URL + path
        if params:
            url = url + ("&" if "?" in path else "?") + urlencode(params)
        r = self.session.get(
            url,
            headers={
                "RequestVerificationToken": token,
                "Content-Type": "application/json",
                "Referer": BASE_URL + page_for_token,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        if cache_prefix and cache_key:
            cp = self._cache_path(cache_prefix, cache_key)
            cp.write_text(json.dumps(data))
        return data

    def post_json(
        self,
        path: str,
        body: dict,
        *,
        page_for_token: str,
        cache_prefix: Optional[str] = None,
        cache_key: Optional[str] = None,
        force: bool = False,
    ) -> Any:
        if cache_prefix and cache_key and not force:
            cp = self._cache_path(cache_prefix, cache_key)
            if cp.exists() and cp.stat().st_size > 0:
                return json.loads(cp.read_text())

        token = self._ensure_token(page_for_token)
        self._wait()
        r = self.session.post(
            BASE_URL + path,
            headers={
                "RequestVerificationToken": token,
                "Content-Type": "application/json",
                "Referer": BASE_URL + page_for_token,
                "X-Requested-With": "XMLHttpRequest",
            },
            data=json.dumps(body),
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        if cache_prefix and cache_key:
            cp = self._cache_path(cache_prefix, cache_key)
            cp.write_text(json.dumps(data))
        return data

    # ---- typed helpers ----

    def latest_ut_prices(self, date: str = "") -> dict:
        return self.get_json(
            f"/FundPriceUT/GetAllUTFundPriceByDate?date={date}",
            page_for_token="/Fund-Price-UT",
            cache_prefix="nav",
            cache_key=f"ut_{date or 'latest'}",
        )

    def latest_prs_prices(self, date: str = "") -> dict:
        return self.get_json(
            f"/FundPricePRS/GetAllPrsFundPriceByDate?date={date}",
            page_for_token="/fund-price-prs",
            cache_prefix="nav",
            cache_key=f"prs_{date or 'latest'}",
        )

    def ut_categories(self) -> dict:
        return self.get_json(
            "/FundPriceUT/GetFundCategory",
            page_for_token="/Fund-Price-UT",
            cache_prefix="fundinfo",
            cache_key="ut_categories",
        )

    def prs_categories(self) -> dict:
        return self.get_json(
            "/FundPricePRS/GetFundCategory",
            page_for_token="/fund-price-prs",
            cache_prefix="fundinfo",
            cache_key="prs_categories",
        )

    def overview_fund_list(self) -> dict:
        return self.get_json(
            "/FundOverview/GetFundsList",
            page_for_token="/Fund-Overview",
            cache_prefix="fundinfo",
            cache_key="overview_funds",
        )

    def fund_info(self, scheme_code: str) -> dict:
        return self.get_json(
            "/FundOverview/GetFundInfo",
            page_for_token="/Fund-Overview",
            params={"schemeCode": scheme_code},
            cache_prefix="fundinfo",
            cache_key=f"info_{scheme_code}",
        )

    def performance_chart(
        self, scheme_code: str, start_dd_mmm_yyyy: str, end_dd_mmm_yyyy: str
    ) -> dict:
        return self.post_json(
            "/FundOverview/GetFundPerformanceChartData",
            {
                "SchemeCode": scheme_code,
                "StartDate": start_dd_mmm_yyyy,
                "EndDate": end_dd_mmm_yyyy,
                "IndexCode": "",
            },
            page_for_token="/Fund-Overview",
            cache_prefix="perf",
            cache_key=f"perf_{scheme_code}_{start_dd_mmm_yyyy}_{end_dd_mmm_yyyy}",
        )

    def distribution_history(self, scheme_code: str) -> dict:
        return self.get_json(
            "/FundDistribution/GetFundDistributionHistory",
            page_for_token="/home",
            params={"schemeCode": scheme_code},
            cache_prefix="dist",
            cache_key=f"dist_{scheme_code}",
        )

    def distribution_fund_list(self, dist_feature: str, scheme_type: str) -> dict:
        return self.post_json(
            "/FundDistribution/GetFilteredFundListByDistFeature",
            {"DistFeature": dist_feature, "SchemType": scheme_type},
            page_for_token="/home",
            cache_prefix="dist",
            cache_key=f"distlist_{scheme_type}_{dist_feature}",
        )
