"""kabuステーションAPI のRESTクライアント（情報系のみ。発注は第5段階で追加）。"""
from __future__ import annotations

import threading
import time

import requests


class KabuApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"kabuステーションAPI エラー HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class KabuClient:
    def __init__(self, base_url: str, api_password: str, min_interval: float = 0.15,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._password = api_password
        self._token: str | None = None
        self._min_interval = min_interval  # 情報系は秒間約10件の制限 → 余裕を持たせる
        self._last = 0.0
        self._lock = threading.Lock()
        self._timeout = timeout
        self._session = requests.Session()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def token(self) -> str:
        """トークンを発行する。kabuステーション再起動や再発行で旧トークンは無効になる。"""
        self._throttle()
        r = self._session.post(f"{self.base_url}/token",
                               json={"APIPassword": self._password}, timeout=self._timeout)
        if r.status_code != 200:
            raise KabuApiError(r.status_code, r.text)
        self._token = r.json()["Token"]
        return self._token

    def _request(self, method: str, path: str, *, params=None, json=None, _retry=True):
        if self._token is None:
            self.token()
        self._throttle()
        r = self._session.request(method, f"{self.base_url}{path}", params=params, json=json,
                                  headers={"X-API-KEY": self._token}, timeout=self._timeout)
        if r.status_code == 401 and _retry:
            self.token()
            return self._request(method, path, params=params, json=json, _retry=False)
        if r.status_code != 200:
            raise KabuApiError(r.status_code, r.text)
        return r.json()

    def register(self, symbols: list[str], exchange: int = 1) -> list[dict]:
        """PUSH配信の対象銘柄を登録する（REST/PUSH合わせて最大50銘柄）。"""
        body = {"Symbols": [{"Symbol": s, "Exchange": exchange} for s in symbols]}
        return self._request("PUT", "/register", json=body).get("RegistList", [])

    def unregister_all(self) -> None:
        self._request("PUT", "/unregister/all")

    def ranking(self, type_: int = 4, exchange_division: str = "T") -> list[dict]:
        """詳細ランキング。type_=4 が売買代金。平日7:53頃〜9:00過ぎは空で返る。"""
        res = self._request("GET", "/ranking",
                            params={"Type": str(type_), "ExchangeDivision": exchange_division})
        return res.get("Ranking") or []

    def soft_limit(self) -> dict:
        return self._request("GET", "/apisoftlimit")
