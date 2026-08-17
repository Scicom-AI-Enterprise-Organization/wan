"""Environment variable defaults for every :func:`fastapi_loki_tempo.patch` argument.

Every knob is readable from the environment so the same image can be deployed to
local docker-compose, staging and production without a code change.
"""

import os
from typing import List, Optional

_TRUTHY = {'1', 'true', 't', 'yes', 'y', 'on'}


def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read `name`, treating an empty/whitespace value the same as unset.

    Compose and Kubernetes both happily inject empty strings for unset variables,
    and an empty OTLP endpoint must not look like a configured one.
    """
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in _TRUTHY


def env_int(name: str, default: Optional[int]) -> Optional[int]:
    value = env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as e:
        raise ValueError(f'{name}={value!r} is not a valid integer') from e


def env_float(name: str, default: Optional[float]) -> Optional[float]:
    value = env_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as e:
        raise ValueError(f'{name}={value!r} is not a valid float') from e


def env_list(name: str, default: str) -> List[str]:
    value = env_str(name, default) or ''
    return [part.strip() for part in value.split(',') if part.strip()]


# --- identity -------------------------------------------------------------------------
SERVICE_NAME = env_str('SERVICE_NAME', 'fastapi')
SERVICE_VERSION = env_str('SERVICE_VERSION')
DEPLOYMENT_ENVIRONMENT = env_str('DEPLOYMENT_ENVIRONMENT')

# --- tracing --------------------------------------------------------------------------
OTLP_ENDPOINT = env_str('OTLP_ENDPOINT')
# 'grpc' -> :4317, 'http' -> :4318. Also accepts OpenTelemetry's own spelling
# 'http/protobuf' so OTEL_EXPORTER_OTLP_PROTOCOL can be reused verbatim.
OTLP_PROTOCOL = (env_str('OTLP_PROTOCOL', 'grpc') or 'grpc').lower()
OTLP_INSECURE = env_bool('OTLP_INSECURE', True)
# 'key1=value1,key2=value2', e.g. Grafana Cloud's Authorization header.
OTLP_HEADERS = env_str('OTLP_HEADERS')
OTLP_TIMEOUT = env_int('OTLP_TIMEOUT', 10)

JAEGER_HOST = env_str('JAEGER_HOST')
JAEGER_PORT = env_int('JAEGER_PORT', 6831)

TRACING_SAMPLE = env_float('TRACING_SAMPLE', 1.0)
# Lower than the OpenTelemetry default of 5s so a container that is SIGTERM'd
# straight after a request still ships the trace.
SPAN_EXPORT_DELAY_MS = env_int('SPAN_EXPORT_DELAY_MS', 2000)
ENABLE_CONSOLE_SPAN_EXPORTER = env_bool('ENABLE_CONSOLE_SPAN_EXPORTER', False)

# Paths that should never create a trace: they are polled constantly and would
# otherwise dominate both the trace store and the service graph.
TRACE_EXCLUDE_URLS = env_str(
    'TRACE_EXCLUDE_URLS',
    'healthz,livez,readyz,metrics,favicon.ico',
)

ENABLE_HTTPX_INSTRUMENTATION = env_bool('ENABLE_HTTPX_INSTRUMENTATION', False)
ENABLE_REQUESTS_INSTRUMENTATION = env_bool('ENABLE_REQUESTS_INSTRUMENTATION', False)

# --- logging --------------------------------------------------------------------------
LOGLEVEL = (env_str('LOGLEVEL', 'INFO') or 'INFO').upper()
LOG_STDOUT = env_bool('LOG_STDOUT', True)
LOG_FILE = env_str('LOG_FILE')
LOG_FILE_MAX_BYTES = env_int('LOG_FILE_MAX_BYTES', 64 * 1024 * 1024)
LOG_FILE_BACKUP_COUNT = env_int('LOG_FILE_BACKUP_COUNT', 3)
LOG_MAX_MSG_LENGTH = env_int('LOG_MAX_MSG_LENGTH', 0)
ENABLE_REQUEST_LOG = env_bool('ENABLE_REQUEST_LOG', True)
LOG_EXCLUDE_URLS = env_list('LOG_EXCLUDE_URLS', '/healthz,/livez,/readyz,/metrics')
# Inbound: any of these is reused as the correlation id, so an id created by the
# first service in a chain survives every hop instead of each hop minting its own.
CORRELATION_ID_HEADERS = env_list(
    'CORRELATION_ID_HEADERS',
    'x-correlation-id,correlation-id,x-request-id,request-id',
)
# Outbound and on responses: the single canonical header this service writes.
CORRELATION_ID_HEADER = env_str('CORRELATION_ID_HEADER', 'X-Correlation-ID')
# Attach it to outbound calls made by any OpenTelemetry-instrumented HTTP client.
ENABLE_CORRELATION_ID_PROPAGATION = env_bool('ENABLE_CORRELATION_ID_PROPAGATION', True)

# --- extras ---------------------------------------------------------------------------
ENABLE_PROMETHEUS_METRICS = env_bool('ENABLE_PROMETHEUS_METRICS', True)
METRICS_ENDPOINT = env_str('METRICS_ENDPOINT', '/metrics')

ENABLE_HEALTH_ENDPOINTS = env_bool('ENABLE_HEALTH_ENDPOINTS', True)

ENABLE_SCALAR_DOC = env_bool('ENABLE_SCALAR_DOC', True)
SCALAR_DOC_ENDPOINT = env_str('SCALAR_DOC_ENDPOINT', '/scalar')
SCALAR_TITLE = env_str('SCALAR_TITLE')
SCALAR_THEME = env_str('SCALAR_THEME', 'purple')
SCALAR_DARK_MODE = env_bool('SCALAR_DARK_MODE', True)
# Override to self host the bundle (air-gapped deploys) e.g. '/static/scalar.js'.
SCALAR_JS_URL = env_str('SCALAR_JS_URL')
