"""Contract for relaying a raw Pine alert from Discord to the dry-run endpoint.

The relay is the only component that accepts input from outside the machine, so
its admission rules are the perimeter. Everything downstream — the parser, the
risk layer, the executor — assumes the message came from the one approved
webhook in the one approved channel.

WHAT CHANGES FOR PINE
---------------------
`admit_message` today requires JSON and returns a dict, and `forward`
re-serialises it. A Pine alert is raw pipe-delimited text, and the dry-run
endpoint hashes the exact body it receives to form the audit id. Re-encoding
would change that id, so the text must pass through byte-for-byte.

WHAT MUST NOT CHANGE
--------------------
Channel id AND source webhook id, both checked before anything else. That is
the security boundary; the rest of this file is about not making it noisy,
not making it lossy, and not letting it reach anything that trades.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tv_alpaca_gateway.relay import RelaySettings, admit_message

CHANNEL = 1530636075947659424
WEBHOOK = 999888777

PINE_ALERT = (
    "EXECUTE_ALPACA_ORDER | SYMBOL=QQQ | SIDE=BUY | QTY=1 | ORDER_TYPE=MARKET | "
    "TIME_IN_FORCE=DAY | REQUIRED_ACTIONS=SUBMIT_ORDER"
)


def _settings(**kw):
    base = dict(token="t", channel_id=CHANNEL, source_webhook_id=WEBHOOK,
                internal_url="http://127.0.0.1:8000/webhooks/tradingview/pine/dry-run",
                internal_secret="s")
    base.update(kw)
    return RelaySettings(**base)


def _message(content=PINE_ALERT, channel=CHANNEL, webhook=WEBHOOK):
    return SimpleNamespace(channel=SimpleNamespace(id=channel),
                           webhook_id=webhook, content=content)


# ══════════════════════════════════════════════ the perimeter must not move

def test_only_the_approved_channel_is_admitted():
    with pytest.raises(ValueError):
        admit_message(_message(channel=CHANNEL + 1), _settings())


def test_only_the_approved_source_webhook_is_admitted():
    """A human typing the alert text into the right channel must not trade."""
    with pytest.raises(ValueError):
        admit_message(_message(webhook=None), _settings())
    with pytest.raises(ValueError):
        admit_message(_message(webhook=WEBHOOK + 1), _settings())


# ═════════════════════════════════════════════ raw text, byte for byte

def test_a_pine_alert_survives_the_relay_unaltered():
    """The dry-run endpoint hashes the body to form its audit id.

    Re-serialising through JSON — even losslessly — changes the bytes and
    therefore the id, so the same alert would audit under two different ids
    depending on the path it took.
    """
    admitted = admit_message(_message(), _settings())
    text = admitted if isinstance(admitted, str) else admitted.get("raw", admitted)
    assert text == PINE_ALERT


def test_the_alert_is_not_required_to_be_json():
    """The current relay rejects anything that is not a JSON object. A Pine
    alert is not JSON, so as it stands every real alert is dropped."""
    admit_message(_message(), _settings())          # must not raise


# ════════════════════════════════════════════════ noise, loss, and safety

def test_messages_without_the_execution_prefix_are_ignored():
    """Chat in the same channel must not become 422s in the gateway log.

    A relay that forwards everything turns the endpoint's error log into a
    transcript, and the real failures are then indistinguishable from noise.

    Matched on the reason: on the current relay this text is refused for not
    being JSON, which would pass a bare `raises(ValueError)` while proving
    nothing about a prefix rule that does not exist yet.
    """
    with pytest.raises(ValueError, match=r"(?i)prefix|EXECUTE_ALPACA_ORDER|not an order"):
        admit_message(_message(content="how did the open go?"), _settings())


def test_a_message_at_discords_length_limit_is_refused():
    """Discord splits messages beyond 2000 characters.

    Half an order is worse than no order: the fragments each fail to parse,
    and if the split ever landed between two fields the surviving half could
    parse into something valid but wrong.

    Matched on the reason, for the same cause as above.
    """
    with pytest.raises(ValueError, match=r"(?i)length|too long|2000|truncat"):
        admit_message(_message(content=PINE_ALERT + "X" * 2000), _settings())


def test_the_relay_refuses_to_target_an_executing_endpoint():
    """The dry-run path cannot trade. Nothing else about the relay makes it
    safe, so the target URL is load-bearing and must be asserted rather than
    trusted to configuration."""
    for path in ("/webhooks/tradingview/pine/paper-submit",
                 "/webhooks/tradingview"):
        with pytest.raises(ValueError):
            _settings(internal_url=f"http://127.0.0.1:8000{path}").validate_target()


def test_the_dry_run_target_is_accepted():
    _settings().validate_target()


def test_a_rejected_message_says_why():
    """`except ValueError: return` drops rejections silently, so a wrong
    channel id is indistinguishable from no alert arriving — which is exactly
    the first failure to expect when wiring a new webhook."""
    with pytest.raises(ValueError) as caught:
        admit_message(_message(channel=CHANNEL + 1), _settings())
    assert str(caught.value), "the rejection carried no reason"
