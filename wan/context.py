"""Request scoped correlation id and OpenTelemetry trace context helpers.

The correlation id lives in a :class:`~contextvars.ContextVar` so reading it is
O(1) from anywhere in the request, including background tasks spawned from it.
"""

import uuid
from contextvars import ContextVar, Token
from typing import Any, Dict, Optional, Tuple

from opentelemetry import trace

#: What `json_logging` used for "no value", kept so log consumers see the same token.
EMPTY_VALUE = '-'

_correlation_id: ContextVar[Optional[str]] = ContextVar(
    'wan_correlation_id', default=None,
)


def new_correlation_id() -> str:
    """A fresh correlation id.

    uuid4, not uuid1: uuid1 embeds the host MAC address and a timestamp, which
    leaks infrastructure detail into every log line and response header.
    """
    return str(uuid.uuid4())


def get_correlation_id(default: str = EMPTY_VALUE) -> str:
    return _correlation_id.get() or default


def set_correlation_id(correlation_id: str) -> Token:
    return _correlation_id.set(correlation_id)


def reset_correlation_id(token: Token) -> None:
    _correlation_id.reset(token)


def get_trace_ids() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(trace_id, span_id, dd_trace_id)`` of the active span, or all ``None``.

    Tempo, Jaeger and the W3C spec all identify a trace by a zero padded 32
    character hex string. ``hex(trace_id)[2:]`` drops leading zeros, so roughly
    one in 16 trace ids came out too short and could not be looked up -- hence
    the explicit ``032x`` formatting.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.trace_id:
        return None, None, None
    return (
        format(span_context.trace_id, '032x'),
        format(span_context.span_id, '016x'),
        # Datadog reads the low 64 bits as an unsigned decimal.
        str(span_context.trace_id & 0xFFFFFFFFFFFFFFFF),
    )


def correlation_headers(header: str = 'X-Correlation-ID') -> Dict[str, str]:
    """Headers to forward on an outbound call so one correlation id spans every hop.

    OpenTelemetry's instrumentation already propagates `traceparent`, so the *trace*
    stays joined without this. The correlation id is not part of that standard, so
    pass it explicitly::

        async with httpx.AsyncClient(headers=correlation_headers()) as client:
            ...
    """
    correlation_id = _correlation_id.get()
    return {header: correlation_id} if correlation_id else {}


def trace_context() -> Dict[str, Any]:
    """Trace correlation fields to merge into a log line.

    ``trace_message`` exists so a Loki derived field can pull the trace id out of
    the raw line with ``traceID=(\\w+)`` regardless of how the line was parsed.
    It stays ``None`` when there is no span, otherwise every unsampled log line
    would render a dead "traceID=None" link in Grafana.
    """
    trace_id, span_id, dd_trace_id = get_trace_ids()
    return {
        'traceID': trace_id,
        'trace_message': f'traceID={trace_id}' if trace_id else None,
        'dd.trace_id': dd_trace_id,
        'spanID': span_id,
    }
