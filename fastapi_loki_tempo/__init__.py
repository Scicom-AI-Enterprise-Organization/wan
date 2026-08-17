"""FastAPI boilerplate for Loki and Tempo.

One call wires up JSON logs carrying the active trace id, OTLP traces for Tempo,
Prometheus metrics, health probes and a Scalar API reference::

    import fastapi_loki_tempo
    from fastapi import FastAPI

    app = FastAPI()
    fastapi_loki_tempo.patch(app=app)
"""

__version__ = '0.1.0'

import logging
from typing import Any, Dict, Iterable, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from fastapi_loki_tempo import scalar as scalar_module
from fastapi_loki_tempo.context import (
    EMPTY_VALUE,
    correlation_headers,
    get_correlation_id,
    get_trace_ids,
    new_correlation_id,
    set_correlation_id,
    trace_context,
)
from fastapi_loki_tempo.logs import (
    REQUEST_LOGGER_NAME,
    TYPE_LOG,
    TYPE_REQUEST,
    JsonLogFormatter,
    setup_logging,
)
from fastapi_loki_tempo.middleware import RequestLoggingMiddleware
from fastapi_loki_tempo.os_env import *  # noqa: F401,F403  (env-derived defaults)
from fastapi_loki_tempo.tracing import flush, setup_tracing

__all__ = [
    '__version__',
    'patch',
    'flush',
    'get_correlation_id',
    'get_trace_ids',
    'correlation_headers',
    'trace_context',
    'new_correlation_id',
    'set_correlation_id',
    'setup_logging',
    'setup_tracing',
    'JsonLogFormatter',
    'RequestLoggingMiddleware',
    'EMPTY_VALUE',
    'REQUEST_LOGGER_NAME',
    'TYPE_LOG',
    'TYPE_REQUEST',
]

logger = logging.getLogger(__name__)


def patch(
    app,
    service_name: str = SERVICE_NAME,  # noqa: F405
    otlp_endpoint: Optional[str] = OTLP_ENDPOINT,  # noqa: F405
    jaeger_host: Optional[str] = JAEGER_HOST,  # noqa: F405
    jaeger_port: Optional[int] = JAEGER_PORT,  # noqa: F405
    tracing_sample: float = TRACING_SAMPLE,  # noqa: F405
    enable_prometheus_metrics: bool = ENABLE_PROMETHEUS_METRICS,  # noqa: F405
    enable_scalar_doc: bool = ENABLE_SCALAR_DOC,  # noqa: F405
    scalar_doc_endpoint: str = SCALAR_DOC_ENDPOINT,  # noqa: F405
    service_version: Optional[str] = SERVICE_VERSION,  # noqa: F405
    environment: Optional[str] = DEPLOYMENT_ENVIRONMENT,  # noqa: F405
    otlp_protocol: str = OTLP_PROTOCOL,  # noqa: F405
    otlp_insecure: bool = OTLP_INSECURE,  # noqa: F405
    otlp_headers: Optional[str] = OTLP_HEADERS,  # noqa: F405
    otlp_timeout: Optional[int] = OTLP_TIMEOUT,  # noqa: F405
    span_export_delay_ms: int = SPAN_EXPORT_DELAY_MS,  # noqa: F405
    console_span_exporter: bool = ENABLE_CONSOLE_SPAN_EXPORTER,  # noqa: F405
    trace_exclude_urls: Optional[str] = TRACE_EXCLUDE_URLS,  # noqa: F405
    loglevel: str = LOGLEVEL,  # noqa: F405
    log_stdout: bool = LOG_STDOUT,  # noqa: F405
    log_file: Optional[str] = LOG_FILE,  # noqa: F405
    log_file_max_bytes: int = LOG_FILE_MAX_BYTES,  # noqa: F405
    log_file_backup_count: int = LOG_FILE_BACKUP_COUNT,  # noqa: F405
    log_max_msg_length: int = LOG_MAX_MSG_LENGTH,  # noqa: F405
    log_static_fields: Optional[Dict[str, Any]] = None,
    enable_request_log: bool = ENABLE_REQUEST_LOG,  # noqa: F405
    log_exclude_urls: Iterable[str] = LOG_EXCLUDE_URLS,  # noqa: F405
    correlation_id_headers: Iterable[str] = CORRELATION_ID_HEADERS,  # noqa: F405
    metrics_endpoint: str = METRICS_ENDPOINT,  # noqa: F405
    enable_health_endpoints: bool = ENABLE_HEALTH_ENDPOINTS,  # noqa: F405
    scalar_title: Optional[str] = SCALAR_TITLE,  # noqa: F405
    scalar_theme: str = SCALAR_THEME,  # noqa: F405
    scalar_dark_mode: bool = SCALAR_DARK_MODE,  # noqa: F405
    scalar_js_url: Optional[str] = SCALAR_JS_URL,  # noqa: F405
    enable_httpx_instrumentation: bool = ENABLE_HTTPX_INSTRUMENTATION,  # noqa: F405
    enable_requests_instrumentation: bool = ENABLE_REQUESTS_INSTRUMENTATION,  # noqa: F405
) -> Dict[str, Any]:
    """Add JSON logging, OpenTelemetry tracing, metrics and docs to a FastAPI app.

    Every argument defaults to an environment variable of the same name in upper
    case, so a deployment can be reconfigured without touching code.

    Parameters
    ----------
    app: fastapi.FastAPI
    service_name: str (env SERVICE_NAME, default 'fastapi')
        Reported as `service.name` on every span, and as `service` on every log line.
    otlp_endpoint: Optional[str] (env OTLP_ENDPOINT)
        Tempo's OTLP endpoint, e.g. 'http://localhost:4317'. Without it, trace ids
        are still generated and logged but no spans leave the process.
    jaeger_host / jaeger_port: Optional[str] / Optional[int] (env JAEGER_HOST, JAEGER_PORT)
        Deprecated. The Jaeger thrift exporter was deprecated upstream in
        OpenTelemetry 1.16; Tempo ingests OTLP directly. Requires the `jaeger` extra.
    tracing_sample: float (env TRACING_SAMPLE, default 1.0)
        Head sampling ratio in (0, 1]. Applied through a ParentBased sampler so a
        trace is never sampled half way. https://opentelemetry.io/docs/concepts/sampling/
    enable_prometheus_metrics: bool (env ENABLE_PROMETHEUS_METRICS, default True)
        Expose Prometheus metrics at `metrics_endpoint` using
        https://github.com/trallnag/prometheus-fastapi-instrumentator
    enable_scalar_doc: bool (env ENABLE_SCALAR_DOC, default True)
        Serve a Scalar API reference at `scalar_doc_endpoint`.
    otlp_protocol: str (env OTLP_PROTOCOL, default 'grpc')
        'grpc' for port 4317, 'http' for port 4318.
    trace_exclude_urls: Optional[str] (env TRACE_EXCLUDE_URLS)
        Comma separated regexes that must not produce spans; health and metrics
        endpoints are excluded by default so they do not swamp Tempo.
    log_exclude_urls: Iterable[str] (env LOG_EXCLUDE_URLS)
        Path prefixes that must not produce a `type=request` log line.
    log_file: Optional[str] (env LOG_FILE)
        Additionally write JSON logs to this rotating file, for agents that tail
        files rather than container stdout.

    Returns
    -------
    dict with the configured `tracer_provider` and `formatter`, for tests and for
    callers that want to add their own exporters.
    """
    if getattr(app.state, 'fastapi_loki_tempo', None) is not None:
        logger.warning('fastapi_loki_tempo.patch() already applied to this app, skipping')
        return app.state.fastapi_loki_tempo

    if not 0 < tracing_sample <= 1:
        raise ValueError('`tracing_sample` must, 0 < `tracing_sample` <= 1')

    if otlp_endpoint and jaeger_host:
        raise ValueError('cannot set `otlp_endpoint` and `jaeger_host` at the same time.')

    formatter = setup_logging(
        level=loglevel,
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        stdout=log_stdout,
        log_file=log_file,
        log_file_max_bytes=log_file_max_bytes,
        log_file_backup_count=log_file_backup_count,
        max_msg_length=log_max_msg_length,
        static_fields=log_static_fields,
        silence_access_logs=enable_request_log,
    )

    # Added before the OpenTelemetry middleware on purpose. Starlette treats the
    # most recently added middleware as the outermost one, so instrumenting after
    # this leaves the span context active while the request line is written --
    # that is what puts `traceID` on `type=request` logs.
    app.add_middleware(
        RequestLoggingMiddleware,
        exclude_urls=log_exclude_urls,
        correlation_id_headers=correlation_id_headers,
        log_requests=enable_request_log,
    )

    tracer_provider = setup_tracing(
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        otlp_endpoint=otlp_endpoint,
        otlp_protocol=otlp_protocol,
        otlp_insecure=otlp_insecure,
        otlp_headers=otlp_headers,
        otlp_timeout=otlp_timeout,
        jaeger_host=jaeger_host,
        jaeger_port=jaeger_port,
        tracing_sample=tracing_sample,
        span_export_delay_ms=span_export_delay_ms,
        console_exporter=console_span_exporter,
    )

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    if not getattr(app, '_is_instrumented_by_opentelemetry', False):
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=tracer_provider,
            excluded_urls=trace_exclude_urls or None,
        )

    if enable_httpx_instrumentation:
        _instrument_optional(
            'opentelemetry.instrumentation.httpx', 'HTTPXClientInstrumentor',
            tracer_provider, extra='httpx',
        )
    if enable_requests_instrumentation:
        _instrument_optional(
            'opentelemetry.instrumentation.requests', 'RequestsInstrumentor',
            tracer_provider, extra='requests',
        )

    if enable_prometheus_metrics:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            excluded_handlers=[metrics_endpoint, '/healthz', '/livez', '/readyz'],
        ).instrument(app).expose(
            app, endpoint=metrics_endpoint, include_in_schema=False, tags=['observability'],
        )

    if enable_health_endpoints:
        _add_health_endpoints(app, service_name, service_version)

    if enable_scalar_doc:
        _add_scalar_endpoint(
            app,
            endpoint=scalar_doc_endpoint,
            title=scalar_title or f'{service_name} API Reference',
            theme=scalar_theme,
            dark_mode=scalar_dark_mode,
            js_url=scalar_js_url,
        )

    state = {
        'version': __version__,
        'service_name': service_name,
        'service_version': service_version,
        'environment': environment,
        'tracer_provider': tracer_provider,
        'formatter': formatter,
        'otlp_endpoint': otlp_endpoint,
        'scalar_doc_endpoint': scalar_doc_endpoint if enable_scalar_doc else None,
        'metrics_endpoint': metrics_endpoint if enable_prometheus_metrics else None,
    }
    app.state.fastapi_loki_tempo = state

    logger.info({
        'message': 'fastapi_loki_tempo patched',
        'service_name': service_name,
        'otlp_endpoint': otlp_endpoint,
        'tracing_sample': tracing_sample,
        'prometheus_metrics': enable_prometheus_metrics,
        'scalar_doc': scalar_doc_endpoint if enable_scalar_doc else None,
    })
    return state


def _instrument_optional(module_path: str, class_name: str, tracer_provider, extra: str) -> None:
    try:
        module = __import__(module_path, fromlist=[class_name])
    except ImportError:
        logger.warning(
            f'{module_path} not installed, skipping. '
            f"install with: pip install 'fastapi-loki-tempo[{extra}]'"
        )
        return
    getattr(module, class_name)().instrument(tracer_provider=tracer_provider)
    logger.info(f'enabled {class_name}')


def _add_health_endpoints(app, service_name: str, service_version: Optional[str]) -> None:
    payload = {'status': 'ok', 'service': service_name, 'version': service_version}

    # Excluded from tracing and from request logs by default: these are polled
    # every few seconds and are pure noise in both Tempo and Loki.
    @app.get('/healthz', include_in_schema=False, response_class=JSONResponse, tags=['observability'])
    async def healthz():
        return payload

    @app.get('/livez', include_in_schema=False, response_class=JSONResponse, tags=['observability'])
    async def livez():
        return payload

    @app.get('/readyz', include_in_schema=False, response_class=JSONResponse, tags=['observability'])
    async def readyz():
        return payload


def _add_scalar_endpoint(
    app,
    endpoint: str,
    title: str,
    theme: str,
    dark_mode: bool,
    js_url: Optional[str],
) -> None:
    if not app.openapi_url:
        logger.warning(
            'scalar doc requested but app.openapi_url is None '
            '(FastAPI(openapi_url=None)); skipping'
        )
        return

    if not endpoint.startswith('/'):
        endpoint = '/' + endpoint

    @app.get(endpoint, include_in_schema=False, response_class=HTMLResponse)
    async def scalar_doc(request: Request):
        # Prefix with root_path the same way FastAPI's own /docs does, so the spec
        # still resolves when the app is mounted behind a path-stripping proxy.
        root_path = request.scope.get('root_path', '').rstrip('/')
        return HTMLResponse(
            scalar_module.render(
                openapi_url=f'{root_path}{app.openapi_url}',
                title=title,
                theme=theme,
                dark_mode=dark_mode,
                js_url=js_url,
            )
        )
