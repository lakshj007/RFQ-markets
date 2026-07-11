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
