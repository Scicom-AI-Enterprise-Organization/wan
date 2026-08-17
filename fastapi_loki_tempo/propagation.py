"""Automatic correlation id propagation on outbound calls.

Inbound is handled by :class:`~fastapi_loki_tempo.middleware.RequestLoggingMiddleware`,
which reuses an incoming correlation id header instead of minting a new one. This
module covers the other half: putting that same id back on every call this service
makes, so one id spans A -> B -> C without any per-call code.

It is implemented as an OpenTelemetry ``TextMapPropagator`` because every OpenTelemetry
HTTP instrumentation (httpx, requests, aiohttp, urllib, grpc, boto) injects the global
propagator into its outbound headers. Hooking in there means the id rides along with
`traceparent` on whatever client the service happens to use, rather than needing a
separate integration per library.
"""

import logging
from typing import Iterable, Optional, Set

from opentelemetry.context import Context
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)

from fastapi_loki_tempo.context import get_correlation_id

logger = logging.getLogger(__name__)

DEFAULT_CORRELATION_ID_HEADER = 'X-Correlation-ID'

_installed = False


class CorrelationIdPropagator(TextMapPropagator):
    """Writes the active correlation id into outbound request headers."""

    def __init__(self, header: str = DEFAULT_CORRELATION_ID_HEADER):
        self._header = header

    def inject(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        setter: Setter = default_setter,
    ) -> None:
        # Empty default: get_correlation_id() normally returns '-' for "no value",
        # and propagating a literal '-' downstream would be worse than propagating
        # nothing, since the receiver would treat it as a real id.
        correlation_id = get_correlation_id(default='')
        if correlation_id:
            setter.set(carrier, self._header, correlation_id)

    def extract(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        getter: Getter = default_getter,
    ) -> Context:
        """No-op.

        Inbound extraction is the request middleware's job -- it owns the ContextVar
        and, crucially, resets it when the request ends. Doing it here as well would
        give two places that set the same id and no clear owner of its lifetime.
        """
        return context if context is not None else Context()

    @property
    def fields(self) -> Set[str]:
        return {self._header}


def install(header: str = DEFAULT_CORRELATION_ID_HEADER) -> bool:
    """Add correlation id propagation to the global OpenTelemetry propagator.

    Composed onto whatever is already registered rather than replacing it: the
    default global propagator carries `traceparent` and `baggage`, and overwriting
    it would break distributed tracing itself.

    Returns True if it was installed by this call.
    """
    global _installed
    if _installed:
        return False

    current = get_global_textmap()
    set_global_textmap(CompositePropagator([current, CorrelationIdPropagator(header)]))
    _installed = True
    logger.info(f'enabled outbound correlation id propagation via {header}')
    return True


def reset() -> None:
    """Forget that we installed. For tests only."""
    global _installed
    _installed = False
