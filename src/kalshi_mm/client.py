from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PRODUCTION_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiAPIError(RuntimeError):
    def __init__(self, status_code: int, method: str, path: str, detail: str) -> None:
        super().__init__(f"Kalshi API {method} {path} returned {status_code}: {detail}")
        self.status_code = status_code
        self.method = method
        self.path = path
        self.detail = detail


def signature_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """Build Kalshi's timestamp + method + path payload, excluding query params."""
    path_without_query = path.split("?", 1)[0]
    return f"{timestamp_ms}{method.upper()}{path_without_query}".encode()


def sign_request(
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: str,
    method: str,
    path: str,
) -> str:
    signature = private_key.sign(
        signature_message(timestamp_ms, method, path),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class KalshiClient:
    def __init__(
        self,
        *,
        base_url: str = PRODUCTION_BASE_URL,
        api_key_id: str | None = None,
        private_key_path: str | Path | None = None,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key_path = Path(private_key_path).expanduser() if private_key_path else None
        self.timeout = timeout
        self.session = session or requests.Session()
        self._private_key: rsa.RSAPrivateKey | None = None

    @classmethod
    def from_env(cls, *, demo: bool) -> KalshiClient:
        return cls(
            base_url=DEMO_BASE_URL if demo else PRODUCTION_BASE_URL,
            api_key_id=os.getenv("KALSHI_API_KEY_ID"),
            private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH"),
        )

    @classmethod
    def from_production_env(cls) -> KalshiClient:
        """Use production-only variable names for any real-money workflow."""
        return cls(
            base_url=PRODUCTION_BASE_URL,
            api_key_id=os.getenv("KALSHI_PROD_API_KEY_ID"),
            private_key_path=os.getenv("KALSHI_PROD_PRIVATE_KEY_PATH"),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is not None:
            return self._private_key
        if not self.private_key_path:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH is required for authenticated requests")
        loaded = serialization.load_pem_private_key(
            self.private_key_path.read_bytes(),
            password=None,
        )
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA private key")
        self._private_key = loaded
        return loaded

    def _signed_headers(self, method: str, full_path: str) -> dict[str, str]:
        if not self.api_key_id:
            raise ValueError("KALSHI_API_KEY_ID is required for authenticated requests")
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sign_request(
                self._load_private_key(), timestamp_ms, method, full_path
            ),
        }

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        full_path = urlparse(self.base_url + path).path
        return self._signed_headers(method, full_path)

    def websocket_headers(self) -> dict[str, str]:
        """Create authentication headers for the Trade API WebSocket handshake."""
        return self._signed_headers("GET", "/trade-api/ws/v2")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        headers = self._auth_headers(method, path) if authenticated else {}
        response = self.session.request(
            method,
            self.base_url + path,
            params=params,
            json=json,
            headers=headers,
            timeout=self.timeout,
        )
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise KalshiAPIError(response.status_code, method, path, str(detail))
        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise KalshiAPIError(response.status_code, method, path, "expected a JSON object")
        return data

    def get_series(
        self,
        *,
        category: str = "Sports",
        tags: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category, "include_volume": "true"}
        if tags:
            params["tags"] = tags
        return self._request("GET", "/series", params=params).get("series", [])

    def get_series_details(self, series_ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/series/{series_ticker}")["series"]

    def get_events(
        self,
        *,
        series_ticker: str,
        status: str = "open",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/events",
            params={
                "series_ticker": series_ticker,
                "status": status,
                "with_nested_markets": "true",
                "limit": min(limit, 200),
            },
        )
        return data.get("events", [])

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}")["market"]

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/events/{event_ticker}",
            params={"with_nested_markets": "true"},
        )["event"]

    def get_orderbook(self, ticker: str, *, depth: int = 20) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_trades(self, ticker: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/markets/trades", params={"ticker": ticker, "limit": limit}
        ).get("trades", [])

    def get_balance(self, *, subaccount: int = 0) -> dict[str, Any]:
        return self._request(
            "GET",
            "/portfolio/balance",
            params={"subaccount": subaccount},
            authenticated=True,
        )

    def get_subaccount_balances(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/portfolio/subaccounts/balances",
            authenticated=True,
        ).get("subaccount_balances", [])

    def get_api_keys(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api_keys", authenticated=True).get("api_keys", [])

    def get_positions(self, *, subaccount: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/portfolio/positions",
            params={
                "count_filter": "position",
                "subaccount": subaccount,
                "limit": min(max(limit, 1), 1000),
            },
            authenticated=True,
        ).get("market_positions", [])

    def get_position(self, ticker: str, *, subaccount: int = 0) -> str:
        data = self._request(
            "GET",
            "/portfolio/positions",
            params={
                "ticker": ticker,
                "count_filter": "position",
                "subaccount": subaccount,
            },
            authenticated=True,
        )
        positions = data.get("market_positions", [])
        return str(positions[0].get("position_fp", "0")) if positions else "0"

    def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        subaccount: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "subaccount": subaccount,
            "limit": min(max(limit, 1), 1000),
        }
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request(
            "GET",
            "/portfolio/orders",
            params=params,
            authenticated=True,
        ).get("orders", [])

    def get_resting_orders(self, ticker: str, *, subaccount: int = 0) -> list[dict[str, Any]]:
        return self.get_orders(ticker=ticker, status="resting", subaccount=subaccount)

    def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        subaccount: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "subaccount": subaccount,
            "limit": min(max(limit, 1), 1000),
        }
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        return self._request(
            "GET",
            "/portfolio/fills",
            params=params,
            authenticated=True,
        ).get("fills", [])

    def create_rfq_quote(
        self,
        *,
        rfq_id: str,
        yes_bid: str,
        no_bid: str,
        rest_remainder: bool = False,
        post_only: bool = True,
        subaccount: int = 0,
    ) -> str:
        data = self._request(
            "POST",
            "/communications/quotes",
            json={
                "rfq_id": rfq_id,
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "rest_remainder": rest_remainder,
                "post_only": post_only,
                "subaccount": subaccount,
            },
            authenticated=True,
        )
        quote_id = str(data.get("id", ""))
        if not quote_id:
            raise KalshiAPIError(201, "POST", "/communications/quotes", "missing quote id")
        return quote_id

    def get_rfq_quotes(
        self,
        *,
        status: str | None = None,
        rfq_id: str | None = None,
        user_filter: str = "self",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user_filter": user_filter,
            "limit": min(max(limit, 1), 500),
        }
        if status:
            params["status"] = status
        if rfq_id:
            params["rfq_id"] = rfq_id
        return self._request(
            "GET",
            "/communications/quotes",
            params=params,
            authenticated=True,
        ).get("quotes", [])

    def get_rfq_quote(self, quote_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/communications/quotes/{quote_id}",
            authenticated=True,
        )["quote"]

    def delete_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self._request(
            "DELETE",
            f"/communications/rfqs/{rfq_id}/quotes/{quote_id}",
            authenticated=True,
        )

    def confirm_rfq_quote(self, rfq_id: str, quote_id: str) -> None:
        self._request(
            "PUT",
            f"/communications/rfqs/{rfq_id}/quotes/{quote_id}/confirm",
            json={},
            authenticated=True,
        )

    def create_order(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,
        count: str,
        price: str,
        expiration_time: int | None = None,
        reduce_only: bool = False,
        subaccount: int = 0,
        post_only: bool = True,
        cancel_order_on_pause: bool = True,
        time_in_force: str = "good_till_canceled",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "count": count,
            "price": price,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "cancel_order_on_pause": cancel_order_on_pause,
            "reduce_only": reduce_only,
            "subaccount": subaccount,
            "exchange_index": 0,
        }
        if time_in_force == "immediate_or_cancel" and expiration_time is not None:
            raise ValueError("immediate-or-cancel orders cannot have an expiration")
        if expiration_time is not None:
            payload["expiration_time"] = expiration_time
        return self._request(
            "POST",
            "/portfolio/events/orders",
            json=payload,
            authenticated=True,
        )

    def cancel_order(self, order_id: str, *, subaccount: int = 0) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"subaccount": subaccount, "exchange_index": 0},
            authenticated=True,
        )
