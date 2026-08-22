"""OpenTelemetry tracer provider setup for Tempo (OTLP) and, optionally, Jaeger."""

import base64
import logging
from typing import Dict, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

logger = logging.getLogger(__name__)

GRPC_PROTOCOLS = ('grpc',)
HTTP_PROTOCOLS = ('http', 'http/protobuf', 'httpprotobuf', 'http-protobuf')

#: Set once configured, so calling patch() twice (uvicorn --reload, tests) does not
#: stack duplicate exporters onto the same provider.
_provider: Optional[TracerProvider] = None


def parse_headers(raw: Optional[str]) -> Optional[Dict[str, str]]:
    """``'a=1,b=2'`` -> ``{'a': '1', 'b': '2'}``."""
    if not raw:
        return None
    headers = {}
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        key, _, value = pair.partition('=')
        headers[key.strip()] = value.strip()
    return headers or None


def basic_auth_headers(
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, str]:
    """``Authorization: Basic ...`` for a collector sitting behind basic auth.

    Grafana Cloud, VictoriaTraces and most reverse-proxied collectors authenticate
    a push this way. OpenTelemetry has no username/password knob of its own -- it
    only takes headers -- so the credentials are encoded here.
    """
    if not username and not password:
        return {}
    token = base64.b64encode(f'{username or ""}:{password or ""}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def build_headers(
    raw: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Merge basic-auth credentials with the free-form ``otlp_headers`` string."""
    headers = basic_auth_headers(username, password)
    # Explicit headers win: `otlp_headers` is the more specific knob, so an
    # Authorization set there (a bearer token, say) is not silently overwritten.
    headers.update(parse_headers(raw) or {})
    return headers or None


def http_traces_endpoint(endpoint: str) -> str:
    """The HTTP exporter wants the full signal path, unlike gRPC.

    Accepts either the collector root ('https://host/insert/opentelemetry') or the
    complete path, so a URL copied from a vendor's docs works unchanged.
    """
    endpoint = endpoint.rstrip('/')
    if endpoint.endswith('/v1/traces'):
        return endpoint
    return endpoint + '/v1/traces'


def build_otlp_exporter(
    endpoint: str,
    protocol: str = 'grpc',
    insecure: bool = True,
    headers: Optional[str] = None,
    timeout: Optional[int] = 10,
    username: Optional[str] = None,
    password: Optional[str] = None,
):
    """One OTLP span exporter, over HTTP/protobuf or gRPC."""
    protocol = (protocol or 'grpc').lower()
    resolved = build_headers(headers, username, password)

    if protocol in HTTP_PROTOCOLS:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HTTPSpanExporter,
        )
        endpoint = http_traces_endpoint(endpoint)
        exporter = HTTPSpanExporter(
            endpoint=endpoint,
            headers=resolved,
            timeout=timeout,
        )
        # Never log `resolved`: it carries the credentials.
        logger.info(f'enabled OTLP/HTTP span exporter at {endpoint}')
        return exporter

    if protocol in GRPC_PROTOCOLS:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GRPCSpanExporter,
        )
        exporter = GRPCSpanExporter(
            endpoint=endpoint,
            insecure=insecure,
            headers=resolved,
            timeout=timeout,
        )
        logger.info(f'enabled OTLP/gRPC span exporter at {endpoint}')
        return exporter

    raise ValueError(
        f'unknown otlp_protocol {protocol!r}, expected one of '
        f'{GRPC_PROTOCOLS + HTTP_PROTOCOLS}'
    )


def build_resource(
    service_name: str,
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
) -> Resource:
    attributes = {'service.name': service_name}
    if service_version:
        attributes['service.version'] = service_version
    if environment:
        attributes['deployment.environment'] = environment
        # Newer semantic convention name; Grafana reads either.
        attributes['deployment.environment.name'] = environment
    # Resource.create merges OTEL_RESOURCE_ATTRIBUTES and process/sdk detectors in.
    return Resource.create(attributes)


def build_sampler(tracing_sample: float):
    """Head sampler honouring the caller's decision.

    ``ParentBased`` keeps a distributed trace all-or-nothing: if an upstream
    service sampled the trace we keep our spans too, so traces never come out
    with holes in the middle. The reference implementation accepted
    ``tracing_sample`` but never built a sampler at all, so it was always 100%.
    """
    if tracing_sample >= 1:
        return ALWAYS_ON
    return ParentBased(root=TraceIdRatioBased(tracing_sample))


def setup_tracing(
    service_name: str,
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
    otlp_endpoint: Optional[str] = None,
    otlp_protocol: str = 'grpc',
    otlp_insecure: bool = True,
    otlp_headers: Optional[str] = None,
    otlp_username: Optional[str] = None,
    otlp_password: Optional[str] = None,
    otlp_timeout: Optional[int] = 10,
    jaeger_host: Optional[str] = None,
    jaeger_port: Optional[int] = 6831,
    tracing_sample: float = 1.0,
    span_export_delay_ms: int = 2000,
    console_exporter: bool = False,
) -> TracerProvider:
    global _provider
    if _provider is not None:
        return _provider

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Something else already installed an SDK provider (another library, or an
        # auto-instrumentation agent). Reuse it instead of losing its exporters.
        provider = current
        logger.info('reusing existing OpenTelemetry TracerProvider')
    else:
        provider = TracerProvider(
            resource=build_resource(service_name, service_version, environment),
            sampler=build_sampler(tracing_sample),
        )
        # TracerProvider registers its own atexit hook (shutdown_on_exit=True),
        # which flushes the batch processors on a clean interpreter exit.
        trace.set_tracer_provider(provider)

    exporters = []

    if otlp_endpoint:
        exporters.append(build_otlp_exporter(
            endpoint=otlp_endpoint,
            protocol=otlp_protocol,
            insecure=otlp_insecure,
            headers=otlp_headers,
            timeout=otlp_timeout,
            username=otlp_username,
            password=otlp_password,
        ))

    if jaeger_host:
        try:
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter
        except ImportError as e:
            raise ImportError(
                'jaeger_host is set but the Jaeger exporter is not installed, and it '
                'has been deprecated upstream since OpenTelemetry 1.16. Prefer '
                "otlp_endpoint (Tempo accepts OTLP natively); or install "
                "`pip install 'wan[jaeger]'` to keep using it."
            ) from e
        exporters.append(JaegerExporter(
            agent_host_name=jaeger_host,
            agent_port=int(jaeger_port or 6831),
        ))
        logger.warning(
            f'enabled deprecated Jaeger thrift exporter at {jaeger_host}:{jaeger_port}, '
            'use OTLP instead'
        )

    if console_exporter:
        exporters.append(ConsoleSpanExporter())
        logger.info('enabled console span exporter')

    for exporter in exporters:
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=span_export_delay_ms)
        )

    if not exporters:
        logger.warning(
            'no span exporter configured: trace ids are still generated and logged, '
            'but nothing is sent to Tempo. Set OTLP_ENDPOINT to export.'
        )

    _provider = provider
    return provider


def flush(timeout_millis: int = 5000) -> bool:
    """Force pending spans out. Call before a hard shutdown (e.g. serverless)."""
    if _provider is None:
        return True
    return _provider.force_flush(timeout_millis=timeout_millis)


def reset() -> None:
    """Drop the cached provider. For tests only."""
    global _provider
    _provider = None
