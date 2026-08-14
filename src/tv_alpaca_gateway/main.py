from __future__ import annotations

import uvicorn

from .config import Settings, configure_logging


def run() -> None:
    # direct_runner already calls this; the gateway did not, so LOG_LEVEL was a
    # setting with no effect on the very process it matters most for. A knob
    # that silently does nothing is worse than no knob — you turn it and
    # conclude the thing you are debugging is not the problem.
    configure_logging(Settings.from_env().log_level)
    uvicorn.run("tv_alpaca_gateway.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
