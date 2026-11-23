"""Yahoo Finance から5分足を取り込む（予備検証用）。

非公式のデータで、取得できるのは直近約60日分。品質の保証はないため、
ここから得た成績は参考値として扱うこと。
"""
from __future__ import annotations

from datetime import datetime

import requests

from .bars import JST, Bar, bucket_start

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"


def parse_chart(symbol: str, payload: dict, minutes: int = 5) -> list[Bar]:
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        return []
    q = result["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(result["timestamp"]):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or not v:
            continue
        start = datetime.fromtimestamp(ts, JST)
        if bucket_start(start, minutes) != start:
            continue  # 立会時間外・引けの端数は使わない
        bars.append(Bar(symbol, start, minutes, o, h, l, c, float(v), 0, True))
    return bars


def fetch(symbol: str, days: int = 60, minutes: int = 5) -> list[Bar]:
    r = requests.get(URL.format(code=symbol),
                     params={"interval": f"{minutes}m", "range": f"{days}d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return parse_chart(symbol, r.json(), minutes)
