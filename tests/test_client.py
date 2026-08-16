from kalshi_mm.client import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    KalshiClient,
    signature_message,
)


def test_signature_message_strips_query_parameters() -> None:
    assert signature_message(
        "1700000000000",
        "get",
        "/trade-api/v2/portfolio/orders?limit=5",
    ) == b"1700000000000GET/trade-api/v2/portfolio/orders"


def test_demo_client_uses_current_external_host() -> None:
    client = KalshiClient(base_url=DEMO_BASE_URL)
    assert client.base_url == "https://external-api.demo.kalshi.co/trade-api/v2"


def test_websocket_headers_sign_exact_websocket_path(monkeypatch) -> None:
    signed_paths: list[str] = []
    client = KalshiClient(api_key_id="key-id")

    monkeypatch.setattr("kalshi_mm.client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr(client, "_load_private_key", lambda: object())
    monkeypatch.setattr(
        "kalshi_mm.client.sign_request",
        lambda _key, _timestamp, _method, path: signed_paths.append(path) or "signature",
    )

    headers = client.websocket_headers()

    assert headers["KALSHI-ACCESS-SIGNATURE"] == "signature"
    assert signed_paths == ["/trade-api/ws/v2"]


def test_production_client_uses_separate_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("KALSHI_API_KEY_ID", "demo-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/demo.key")
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "prod-key")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY_PATH", "/tmp/prod.key")

    client = KalshiClient.from_production_env()

    assert client.base_url == PRODUCTION_BASE_URL
    assert client.api_key_id == "prod-key"
    assert str(client.private_key_path) == "/tmp/prod.key"


class FakeResponse:
    ok = True
    content = b"{}"

    def json(self) -> dict:
        return {"order_id": "order-1"}


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()


def test_create_order_uses_guarded_v2_payload(monkeypatch) -> None:
    session = RecordingSession()
    client = KalshiClient(session=session)  # type: ignore[arg-type]
    monkeypatch.setattr(client, "_auth_headers", lambda method, path: {"auth": "test"})

    client.create_order(
        ticker="MARKET",
        client_order_id="manual-live-test-intent-001",
        side="bid",
        count="1",
        price="0.16",
        expiration_time=123456,
    )

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/portfolio/events/orders")
    assert call["json"] == {
        "ticker": "MARKET",
        "client_order_id": "manual-live-test-intent-001",
        "side": "bid",
        "count": "1",
        "price": "0.16",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "cancel_order_on_pause": True,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0,
        "expiration_time": 123456,
    }


def test_create_order_supports_reduce_only_ioc_exit(monkeypatch) -> None:
    session = RecordingSession()
    client = KalshiClient(session=session)  # type: ignore[arg-type]
    monkeypatch.setattr(client, "_auth_headers", lambda method, path: {"auth": "test"})

    client.create_order(
        ticker="MARKET",
        client_order_id="manual-exit-fallback-test-intent-001",
        side="ask",
        count="1",
        price="0.55",
        reduce_only=True,
        post_only=False,
        time_in_force="immediate_or_cancel",
    )

    payload = session.calls[0]["json"]
    assert payload["side"] == "ask"
    assert payload["reduce_only"] is True
    assert payload["post_only"] is False
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert "expiration_time" not in payload


class QuoteResponse(FakeResponse):
    def json(self) -> dict:
        return {"id": "quote-1"}


class QuoteSession(RecordingSession):
    def request(self, method: str, url: str, **kwargs) -> QuoteResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return QuoteResponse()


def test_create_delete_and_confirm_rfq_quote_use_communications_endpoints(monkeypatch) -> None:
    session = QuoteSession()
    client = KalshiClient(session=session)  # type: ignore[arg-type]
    monkeypatch.setattr(client, "_auth_headers", lambda method, path: {"auth": "test"})

    quote_id = client.create_rfq_quote(
        rfq_id="rfq-1",
        yes_bid="0.53",
        no_bid="0.43",
        subaccount=2,
    )
    client.delete_rfq_quote("rfq-1", quote_id)
    client.confirm_rfq_quote("rfq-1", quote_id)

    assert quote_id == "quote-1"
    assert session.calls[0]["url"].endswith("/communications/quotes")
    assert session.calls[0]["json"] == {
        "rfq_id": "rfq-1",
        "yes_bid": "0.53",
        "no_bid": "0.43",
        "rest_remainder": False,
        "post_only": True,
        "subaccount": 2,
    }
    assert session.calls[1]["method"] == "DELETE"
    assert session.calls[1]["url"].endswith("/communications/quotes/quote-1")
    assert session.calls[2]["method"] == "PUT"
    assert session.calls[2]["url"].endswith(
        "/communications/rfqs/rfq-1/quotes/quote-1/confirm"
    )
    assert session.calls[2]["json"] == {}


def test_rfq_canary_preflight_reads_use_exact_authenticated_endpoints(monkeypatch) -> None:
    client = KalshiClient()
    calls: list[tuple[str, str, dict]] = []

    def request(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api_keys":
            return {"api_keys": [{"api_key_id": "key"}]}
        if path == "/portfolio/subaccounts/balances":
            return {"subaccount_balances": [{"subaccount_number": 1}]}
        if path == "/portfolio/positions":
            return {"market_positions": []}
        return {"quote": {"id": "quote-1"}}

    monkeypatch.setattr(client, "_request", request)

    assert client.get_api_keys() == [{"api_key_id": "key"}]
    assert client.get_subaccount_balances() == [{"subaccount_number": 1}]
    assert client.get_positions(subaccount=1) == []
    assert client.get_rfq_quote("quote-1") == {"id": "quote-1"}
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api_keys"),
        ("GET", "/portfolio/subaccounts/balances"),
        ("GET", "/portfolio/positions"),
        ("GET", "/communications/quotes/quote-1"),
    ]
    assert all(kwargs["authenticated"] is True for _, _, kwargs in calls)
