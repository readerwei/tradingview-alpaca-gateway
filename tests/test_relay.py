import json
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
        content=content if content is not None else json.dumps({"event_id": "x", "symbol": "QQQ"}),
    )


def test_relay_admits_only_configured_channel_and_webhook(settings):
    assert admit_message(message(), settings)["symbol"] == "QQQ"
    with pytest.raises(ValueError, match="channel"):
        admit_message(message(channel_id=999), settings)
    with pytest.raises(ValueError, match="source webhook"):
        admit_message(message(webhook_id=999), settings)


def test_relay_rejects_non_json_content(settings):
    with pytest.raises(ValueError, match="valid JSON"):
        admit_message(message(content="BUY QQQ"), settings)


def test_relay_requires_json_object(settings):
    with pytest.raises(ValueError, match="JSON object"):
        admit_message(message(content="[1, 2, 3]"), settings)
