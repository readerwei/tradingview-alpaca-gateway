from __future__ import annotations

import logging

import uvicorn

from .logging_setup import configure


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    configure()
    uvicorn.run("tv_alpaca_gateway.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
