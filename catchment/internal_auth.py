"""The shared-secret check for every ``/internal/*`` route.

Its own module so that routers split across files can depend on the *same*
callable. The test that asserts every internal route carries this dependency
compares by identity, and two copies of an equivalent function would pass that
check while being separately configurable — which is exactly the kind of drift
an auth boundary must not have.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from catchment.config import MissingConfiguration, Settings, get_settings
from catchment.logging_config import get_logger

logger = get_logger(__name__)


def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject anything without the shared secret.

    Fails closed: if no token is configured the routes are unavailable rather
    than open, so a half-configured deployment cannot expose the review gate.
    """
    try:
        expected = settings.require_internal_token()
    except MissingConfiguration:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal routes are not configured",
        ) from None

    if x_internal_token is None or not hmac.compare_digest(
        x_internal_token, expected.get_secret_value()
    ):
        logger.warning("internal route rejected: bad or missing token")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden")
