from kalshi_mm.client import DEMO_BASE_URL, KalshiClient, signature_message


def test_signature_message_strips_query_parameters() -> None:
    assert signature_message(
        "1700000000000",
        "get",
        "/trade-api/v2/portfolio/orders?limit=5",
    ) == b"1700000000000GET/trade-api/v2/portfolio/orders"


def test_demo_client_uses_current_external_host() -> None:
    client = KalshiClient(base_url=DEMO_BASE_URL)
    assert client.base_url == "https://external-api.demo.kalshi.co/trade-api/v2"

