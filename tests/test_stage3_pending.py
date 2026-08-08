"""A visible marker for work that is specified but not built.

`tests/test_execution_contract.py` uses `importorskip`, so until Stage 3 exists
its cases skip. Skips are easy to scroll past, and a suite that looks green
while a specified contract is unimplemented is the exact illusion this
repository keeps producing.

So this test carries the state instead:

    Stage 3 absent      -> xfailed   (expected; the contract is waiting)
    Stage 3 implemented -> FAILS     (strict xpass — delete this file and
                                      confirm the contract suite now runs)

The failure on success is deliberate. It is the prompt to check that the 15
contract tests actually ran, rather than assuming they did because the suite
was green.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.xfail(strict=True, reason=(
    "Stage 3 (tv_alpaca_gateway.execution) is specified in "
    "tests/test_execution_contract.py but not implemented. When it lands, this "
    "flips to a failure: delete this file and confirm the contract suite runs "
    "instead of skipping."))
def test_stage_three_execution_module_exists():
    importlib.import_module("tv_alpaca_gateway.execution")


def test_the_contract_suite_is_present_and_non_trivial():
    """Guards the guard: the contract file must exist and still say something.

    If it were deleted or emptied, every contract test would vanish and the
    suite would go green — indistinguishable from the work being done.
    """
    import pathlib

    contract = pathlib.Path(__file__).parent / "test_execution_contract.py"
    assert contract.exists(), "the execution contract has gone missing"
    body = contract.read_text()
    cases = body.count("def test_")
    assert cases >= 19, f"the contract has shrunk to {cases} cases"
