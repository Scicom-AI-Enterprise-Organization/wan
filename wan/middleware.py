"""Pure ASGI request logging + correlation id middleware.

Deliberately *not* a ``BaseHTTPMiddleware`` subclass. ``BaseHTTPMiddleware`` runs
``dispatch`` in a separate anyio task from the endpoint, so a ``ContextVar`` set
there is invisible downstream, and it buffers streaming responses. A raw ASGI
callable shares the task with the endpoint, which is what makes the correlation
id readable from ordinary ``logging`` calls inside handlers.
"""

import logging
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from wan.context import (
    EMPTY_VALUE,
    get_trace_ids,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from wan.logs import (
    REQUEST_LOG_ATTR,
    REQUEST_LOGGER_NAME,
    TYPE_REQUEST,
    iso_time,
)

DEFAULT_CORRELATION_ID_HEADERS = (
    'x-correlation-id',
    'correlation-id',
    'x-request-id',
    'request-id',
)

RawHeaders = Sequence[Tuple[bytes, bytes]]


def _header(headers: RawHeaders, name: bytes, default: Optional[str] = None) -> Optional[str]:
    for key, value in headers:
        if key.lower() == name:
            # Header bytes are latin-1 per RFC 7230; decoding as utf-8 can raise.
            return value.decode('latin-1')
    return default


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _remote_user(scope: Dict[str, Any]) -> str:
    """Best effort username, without requiring AuthenticationMiddleware.

    ``json_logging`` read ``request.user`` unguarded, which raises AssertionError
    on every request when no auth middleware is installed.
    """
    user = scope.get('user')
    if user is None:
        return EMPTY_VALUE
    for attr in ('display_name', 'username', 'identity'):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    return str(user) or EMPTY_VALUE


class RequestLoggingMiddleware:
    """Emit one ``type=request`` JSON line per HTTP request and stamp response headers."""

    def __init__(
        self,
        app,
        exclude_urls: Iterable[str] = (),
        correlation_id_headers: Iterable[str] = DEFAULT_CORRELATION_ID_HEADERS,
        correlation_id_response_header: str = 'X-Correlation-ID',
        correlation_id_max_length: int = 128,
        trace_id_response_header: str = 'X-Trace-Id',
        logger_name: str = REQUEST_LOGGER_NAME,
        log_requests: bool = True,
        on_request_start: Optional[Callable[..., None]] = None,
    ):
        self.app = app
        self.exclude_urls = tuple(exclude_urls or ())
        self.correlation_id_headers = tuple(
            name.lower().encode('latin-1') for name in correlation_id_headers
        )
        self.correlation_id_response_header = (
            correlation_id_response_header.lower().encode('latin-1')
            if correlation_id_response_header else None
        )
        self.correlation_id_max_length = correlation_id_max_length
        self.trace_id_response_header = (
            trace_id_response_header.lower().encode('latin-1')
            if trace_id_response_header else None
        )
        self.log_requests = log_requests
        # Hook for anything that must see the ids while the request is live.
        self.on_request_start = on_request_start
        self.logger = logging.getLogger(logger_name)

    def excluded(self, path: str) -> bool:
        return any(path == pattern or path.startswith(pattern) for pattern in self.exclude_urls)

    def correlation_id(self, headers: RawHeaders) -> str:
        """Reuse an inbound id so one id spans every hop of a request.

        Truncated to `correlation_id_max_length` (0 = no cap): the inbound value is
        attacker-controlled, and uncapped it lands verbatim on every log line and
        response header for the request. Truncation rather than rejection, so a chain
        whose first hop minted an oversized id still correlates from this hop on.
        """
        for name in self.correlation_id_headers:
            value = _header(headers, name)
            if value:
                if self.correlation_id_max_length > 0:
                    value = value[:self.correlation_id_max_length]
                return value
        return new_correlation_id()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        headers: RawHeaders = scope.get('headers') or ()
        correlation_id = self.correlation_id(headers)
        token = set_correlation_id(correlation_id)
        # Also expose it as request.state.correlation_id for handler code.
        scope.setdefault('state', {})['correlation_id'] = correlation_id

        path = scope.get('path', '')
        should_log = self.log_requests and not self.excluded(path)
        started_monotonic = time.monotonic()
        received_at = iso_time(time.time())

        if self.on_request_start is not None:
            # The OpenTelemetry middleware is outside this one, so its span is already
            # active and the trace id is available here.
            trace_id, span_id, _ = get_trace_ids()
            self.on_request_start(
                correlation_id=correlation_id, trace_id=trace_id, span_id=span_id)

        status: Optional[int] = None
        response_headers: RawHeaders = ()
        body_bytes = 0

        async def send_wrapper(message):
            nonlocal status, response_headers, body_bytes
            if message['type'] == 'http.response.start':
                status = message['status']
                raw: List[Tuple[bytes, bytes]] = list(message.get('headers') or [])
                if self.correlation_id_response_header:
                    raw.append((self.correlation_id_response_header, correlation_id.encode('latin-1')))
                trace_id, _, _ = get_trace_ids()
                if trace_id and self.trace_id_response_header:
                    raw.append((self.trace_id_response_header, trace_id.encode('latin-1')))
                message['headers'] = raw
                response_headers = raw
            elif message['type'] == 'http.response.body':
                body_bytes += len(message.get('body') or b'')
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            if should_log:
                # uvicorn logs unhandled exceptions too, but from outside the request
                # context -- so its traceback has no traceID and no correlation_id.
                # Attaching exc_info here keeps the traceback, the request detail and
                # the trace id on one searchable line.
                self.emit(
                    scope, headers, received_at, started_monotonic,
                    status if status is not None else 500, (), body_bytes, correlation_id,
                    exc_info=sys.exc_info(),
                )
            raise
        else:
            if should_log:
                self.emit(
                    scope, headers, received_at, started_monotonic,
                    status if status is not None else 500, response_headers, body_bytes,
                    correlation_id,
                )
        finally:
            reset_correlation_id(token)

    def emit(
        self,
        scope: Dict[str, Any],
        headers: RawHeaders,
        received_at: str,
        started_monotonic: float,
        status: int,
        response_headers: RawHeaders,
        body_bytes: int,
        correlation_id: str,
        exc_info=None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        client = scope.get('client') or (None, None)
        remote_ip, remote_port = client[0], client[1]
        protocol = scope.get('type', '').upper()
        http_version = scope.get('http_version')
        if http_version:
            protocol = f'{protocol}/{http_version}'

        content_length = _header(response_headers, b'content-length')
        # Fall back to bytes actually written, so streaming/chunked responses report
        # a real size instead of '-'.
        response_size = content_length if content_length is not None else str(body_bytes)

        request_log = {
            'type': TYPE_REQUEST,
            'correlation_id': correlation_id,
            'remote_user': _remote_user(scope),
            'request': scope.get('path', ''),
            'referer': _header(headers, b'referer', EMPTY_VALUE),
            'x_forwarded_for': _header(headers, b'x-forwarded-for', EMPTY_VALUE),
            'protocol': protocol or EMPTY_VALUE,
            'method': scope.get('method', EMPTY_VALUE),
            'remote_ip': remote_ip or EMPTY_VALUE,
            'request_size_b': _parse_int(_header(headers, b'content-length'), -1),
            'remote_host': remote_ip or EMPTY_VALUE,
            'remote_port': remote_port if remote_port is not None else -1,
            'request_received_at': received_at,
            'response_time_ms': elapsed_ms,
            'response_status': status,
            'response_size_b': response_size,
            'response_content_type': _header(response_headers, b'content-type', EMPTY_VALUE),
            'response_sent_at': iso_time(time.time()),
        }

        route = scope.get('route')
        extra: Dict[str, Any] = {REQUEST_LOG_ATTR: request_log}
        route_path = getattr(route, 'path', None)
        if route_path:
            # Low cardinality template ('/items/{id}'), safe to use as a Loki label.
            extra['request_route'] = route_path

        self.logger.log(self.level_for(status), '', extra=extra, exc_info=exc_info)

    @staticmethod
    def level_for(status: int) -> int:
        """Map status onto a log level so `level` is usable across both log types."""
        if status >= 500:
            return logging.ERROR
        if status >= 400:
            return logging.WARNING
        return logging.INFO
