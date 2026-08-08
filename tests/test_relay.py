from types import SimpleNamespace

import pytest

from tv_alpaca_gateway.relay import RelaySettings, admit_message


@pytest.fixture
def settings():
    return RelaySettings(token="token", channel_id=123, source_webhook_id=456)


def message(channel_id=123, webhook_id=456, content=None):
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        webhook_id=webhook_id,
        content=content if content is not None else (
            "EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | "
            "ORDER_TYPE=MARKET | TIME_IN_FORCE=DAY"
        ),
    )


def test_relay_admits_only_configured_channel_and_webhook(settings):
    assert admit_message(message(), settings).startswith("EXECUTE_ALPACA_ORDER")
    with pytest.raises(ValueError, match="channel"):
        admit_message(message(channel_id=999), settings)
    with pytest.raises(ValueError, match="source webhook"):
        admit_message(message(webhook_id=999), settings)


def test_relay_rejects_message_without_execution_prefix(settings):
    with pytest.raises(ValueError, match="EXECUTE_ALPACA_ORDER"):
        admit_message(message(content="BUY QQQ"), settings)


def test_relay_rejects_non_order_json(settings):
    with pytest.raises(ValueError, match="EXECUTE_ALPACA_ORDER"):
        admit_message(message(content="[1, 2, 3]"), settings)
