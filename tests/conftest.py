from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """CLI commands configure structlog to write to the stderr of the moment, which under CliRunner is
    a capture stream that is closed afterwards; never let that leak into later tests."""
    yield
    structlog.reset_defaults()
