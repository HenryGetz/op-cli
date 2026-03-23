from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--engine",
        action="store",
        default=None,
        help="Validate only one engine name (default: all registered engines).",
    )

