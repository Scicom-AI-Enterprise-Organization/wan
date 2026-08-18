"""Sentry error reporting that shares its ids with Loki and Tempo.

Sentry is good at what logs and traces are bad at: grouping the same error across
deploys, deduplicating, alerting, and keeping stack traces with local variables. It is
added here as a fourth signal, not as a replacement for any of the other three.

The whole point of this module is that one id pivots between all of them. Sentry keeps
its own trace id by default, which would mean an issue in Sentry could not be matched to
a trace in Tempo or a log line in Loki; every event is rewritten to carry the
OpenTelemetry trace id instead.
"""

import logging
from typing import Any, Callable, Dict, Optional

from wan import grafana
from wan.context import get_correlation_id, get_trace_ids

logger = logging.getLogger(__name__)

#: Set once initialised, so calling patch() twice does not re-init the SDK.
_initialised = False


def build_before_send(
    override_trace_context: bool = True,
    static_tags: Optional[Dict[str, str]] = None,
    grafana_url: Optional[str] = None,
    loki_selector: str = '{job="fastapi"}',
    dashboard_uid: str = 'wan',
) -> Callable:
    """An event processor that stamps the correlation and trace ids onto every event.

    `override_trace_context` replaces Sentry's own randomly generated trace id with the
    OpenTelemetry one. That is correct when Tempo owns tracing (the default), and wrong
    when Sentry's own performance monitoring is enabled -- there its trace id is the key
    to its own span data, so it is left alone.
    """

    def before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Dict[str, Any]:
        try:
            tags = event.setdefault('tags', {})
            if static_tags:
                for key, value in static_tags.items():
                    tags.setdefault(key, value)

            correlation_id = get_correlation_id(default='')
            if not correlation_id:
                # Same reason as the trace id below: before_send runs after the request
                # has finished, so read back what bind_request_scope stamped on the
                # scope while it was live.
                correlation_id = tags.get('correlation_id', '')
            if correlation_id:
                # Searchable in Sentry as `correlation_id:<value>`, which is the pivot
                # from a Sentry issue to `| correlation_id="..."` in Loki.
                tags['correlation_id'] = correlation_id

            trace_id, span_id, _ = get_trace_ids()
            if not trace_id:
                # Sentry's ASGI middleware wraps the whole app, so it captures the
                # exception after our middleware has reset its ContextVar and after the
                # OpenTelemetry span has ended. bind_request_scope() put the ids on the
                # request's isolation scope while it was still live; read them back.
                trace_id = tags.get('traceID')
                span_id = tags.get('spanID')
            if trace_id:
                tags['traceID'] = trace_id
                if span_id:
                    tags['spanID'] = span_id
                trace_context = event.setdefault('contexts', {}).setdefault('trace', {})
                if override_trace_context or 'trace_id' not in trace_context:
                    trace_context['trace_id'] = trace_id
                    if span_id:
                        trace_context['span_id'] = span_id
            if grafana_url:
                links = grafana.links_for(
                    grafana_url,
                    selector=loki_selector,
                    dashboard_uid=dashboard_uid,
                    correlation_id=correlation_id or None,
                    trace_id=trace_id,
                )
                if links:
                    # Two places, because Sentry renders them differently: a named
                    # context card, and Additional Data. Whichever your Sentry version
                    # linkifies, the URLs are there.
                    event.setdefault('contexts', {})['grafana'] = links
                    event.setdefault('extra', {}).update(
                        {f'grafana_{name}': url for name, url in links.items()}
                    )
        except Exception:  # pragma: no cover - never lose an event over enrichment
            logger.warning('failed to attach correlation ids to a Sentry event')
        return event

    return before_send


def setup_sentry(
    dsn: Optional[str],
    service_name: str = 'fastapi',
    environment: Optional[str] = None,
    release: Optional[str] = None,
    traces_sample_rate: float = 0.0,
    profiles_sample_rate: float = 0.0,
    send_default_pii: bool = False,
    event_level: str = 'ERROR',
    breadcrumb_level: str = 'INFO',
    instrumenter: str = 'sentry',
    grafana_url: Optional[str] = None,
    loki_selector: str = '{job="fastapi"}',
    dashboard_uid: str = 'wan',
    ignore_loggers: tuple = (),
    tracer_provider=None,
    **init_kwargs,
) -> bool:
    """Initialise the Sentry SDK. Returns True if it was enabled.

    Without a `dsn` this is a no-op, so the same image runs locally with Sentry off.

    Parameters
    ----------
    traces_sample_rate: float (default 0.0)
        0 keeps Sentry to errors only and leaves tracing to Tempo, which avoids running
        two tracers over one request. Above 0, Sentry also records transactions.
    instrumenter: str (default 'sentry')
        'otel' makes Sentry read spans from the OpenTelemetry tracer provider instead of
        creating its own, so its trace ids match Tempo's. Only meaningful when
        `traces_sample_rate` > 0.
    """
    global _initialised

    if not dsn:
        logger.info('SENTRY_DSN not set, Sentry disabled')
        return False

    if _initialised:
        return True

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
    except ImportError as e:
        raise ImportError(
            'SENTRY_DSN is set but sentry-sdk is not installed. Install with: '
            "pip3 install 'wan[sentry] @ git+https://github.com/"
            "Scicom-AI-Enterprise-Organization/wan'"
        ) from e

    sentry_tracing = traces_sample_rate > 0
    use_otel = sentry_tracing and instrumenter.lower() == 'otel'

    options: Dict[str, Any] = {
        'dsn': dsn,
        'environment': environment,
        'release': release,
        'traces_sample_rate': traces_sample_rate,
        'profiles_sample_rate': profiles_sample_rate,
        'send_default_pii': send_default_pii,
        'before_send': build_before_send(
            # Only take over the trace context when Sentry is not tracing itself.
            override_trace_context=not sentry_tracing,
            static_tags={'service': service_name},
            grafana_url=grafana_url,
            loki_selector=loki_selector,
            dashboard_uid=dashboard_uid,
        ),
        'integrations': [
            LoggingIntegration(
                level=logging.getLevelName(breadcrumb_level.upper()),
                event_level=logging.getLevelName(event_level.upper()),
            ),
        ],
    }
    if use_otel:
        options['instrumenter'] = 'otel'
    options.update(init_kwargs)

    sentry_sdk.init(**options)

    for name in ignore_loggers:
        # Our request logger already logs 5xx at ERROR with the traceback attached, and
        # Sentry's FastAPI integration captures the same exception -- without this every
        # failure arrives in Sentry twice.
        ignore_logger(name)

    if use_otel:
        from sentry_sdk.integrations.opentelemetry import SentrySpanProcessor

        provider = tracer_provider
        if provider is None:
            from opentelemetry import trace
            provider = trace.get_tracer_provider()
        provider.add_span_processor(SentrySpanProcessor())
        logger.info('Sentry reading spans from the OpenTelemetry tracer provider')

    _initialised = True
    logger.info({
        'message': 'Sentry enabled',
        'environment': environment,
        'release': release,
        'traces_sample_rate': traces_sample_rate,
        'instrumenter': 'otel' if use_otel else 'sentry',
    })
    return True


def bind_request_scope(
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> None:
    """Stamp the ids onto Sentry's per-request isolation scope.

    Called from inside the request, which is the only place both ids exist: Sentry's
    ASGI middleware is installed outside every user middleware, so when it captures an
    exception our correlation ContextVar has already been reset and the OpenTelemetry
    span has already ended. Writing to the isolation scope while the request is live is
    what carries the ids onto the eventual event.

    A no-op when Sentry is not enabled, so the middleware can call it unconditionally.
    """
    if not _initialised:
        return
    try:
        import sentry_sdk

        scope = sentry_sdk.get_isolation_scope()
        if correlation_id:
            scope.set_tag('correlation_id', correlation_id)
        if trace_id:
            scope.set_tag('traceID', trace_id)
        if span_id:
            scope.set_tag('spanID', span_id)
    except Exception:  # pragma: no cover - never fail a request over telemetry
        logger.warning('failed to bind ids to the Sentry scope')


def reset() -> None:
    """Forget that we initialised. For tests only."""
    global _initialised
    _initialised = False
