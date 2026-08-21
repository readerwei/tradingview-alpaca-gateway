from types import SimpleNamespace

import pytest

from tv_alpaca_relay.relay import RelaySettings, admit_message, handle_message


@pytest.fixture
def settings():
    return RelaySettings(token="token", channel_id=123, source_webhook_id=456)


def message(channel_id=123, webhook_id=456, message_id=123456789012345678, content=None):
    return SimpleNamespace(
        id=message_id,
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


def test_relay_forwards_the_authenticated_discord_message_id(settings):
    """A Discord message ID distinguishes a genuine repeat signal from redelivery."""
    calls = []

    class Relay:
        def forward(self, pine_alert, *, discord_message_id):
            calls.append((pine_alert, discord_message_id))

    source = message(message_id=123456789012345678)
    assert handle_message(source, settings, Relay()) is True
    assert calls == [(source.content, "123456789012345678")]


def test_relay_refuses_missing_or_noncanonical_discord_message_id(settings):
    calls = []

    class Relay:
        def forward(self, *_args, **_kwargs):
            calls.append(True)

    for invalid_id in (None, 0, -1, "123", "abc"):
        assert handle_message(message(message_id=invalid_id), settings, Relay()) is False
    assert calls == []
