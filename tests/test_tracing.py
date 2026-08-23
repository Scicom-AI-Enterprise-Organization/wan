"""OTLP exporter wiring: protocol choice, endpoint shape and push authentication."""

import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from wan.os_env import _default_otlp_protocol
from wan.tracing import (
    basic_auth_headers,
    build_headers,
    build_otlp_exporter,
    build_resource,
    http_traces_endpoint,
    parse_headers,
)


def expected_basic(username, password):
    return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()


def test_parse_headers_ignores_junk():
    assert parse_headers('a=1, b = 2 ,,broken') == {'a': '1', 'b': '2'}
    assert parse_headers('') is None


def test_basic_auth_headers_are_only_built_when_credentials_exist():
    assert basic_auth_headers(None, None) == {}
    assert basic_auth_headers('user', 'pass') == {
        'Authorization': expected_basic('user', 'pass'),
    }


def test_credentials_with_a_colon_or_padding_survive_the_encoding():
    """A password containing ':' is legal; only the first colon separates the pair."""
    header = basic_auth_headers('user', 'p:ss word')['Authorization']
    decoded = base64.b64decode(header.split(' ', 1)[1]).decode()
    assert decoded.split(':', 1) == ['user', 'p:ss word']


def test_explicit_headers_win_over_basic_auth():
    """OTLP_HEADERS is the more specific knob: a bearer token must not be clobbered."""
    headers = build_headers('Authorization=Bearer abc', 'user', 'pass')
    assert headers == {'Authorization': 'Bearer abc'}


def test_basic_auth_and_unrelated_headers_are_merged():
    headers = build_headers('X-Scope-OrgID=tenant', 'user', 'pass')
    assert headers == {
        'Authorization': expected_basic('user', 'pass'),
        'X-Scope-OrgID': 'tenant',
    }


def test_no_headers_at_all_stays_none():
    assert build_headers(None, None, None) is None


@pytest.mark.parametrize(
    'given,expected',
    [
        ('https://host/insert/opentelemetry', 'https://host/insert/opentelemetry/v1/traces'),
        ('https://host/insert/opentelemetry/', 'https://host/insert/opentelemetry/v1/traces'),
        (
            'https://host/insert/opentelemetry/v1/traces',
            'https://host/insert/opentelemetry/v1/traces',
        ),
        ('http://tempo:4318', 'http://tempo:4318/v1/traces'),
    ],
)
def test_http_endpoint_gets_the_signal_path_exactly_once(given, expected):
    assert http_traces_endpoint(given) == expected


@pytest.mark.parametrize(
    'endpoint,expected',
    [
        ('http://tempo:4317', 'grpc'),
        ('tempo:4317', 'grpc'),
        ('https://host/insert/opentelemetry/v1/traces', 'http'),
        ('https://host/insert/opentelemetry/v1/traces/', 'http'),
        (None, 'grpc'),
    ],
)
def test_protocol_is_guessed_from_the_endpoint(endpoint, expected):
    """A push URL carrying the signal path can only be HTTP; a bare host:port is gRPC."""
    assert _default_otlp_protocol(endpoint) == expected


def test_http_exporter_carries_the_credentials_and_the_full_path():
    exporter = build_otlp_exporter(
        'https://host/insert/opentelemetry',
        protocol='http',
        username='user',
        password='pass',
    )
    assert exporter._endpoint == 'https://host/insert/opentelemetry/v1/traces'
    assert exporter._session.headers['Authorization'] == expected_basic('user', 'pass')


def test_otel_spelling_of_the_http_protocol_is_accepted():
    """So OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf can be reused verbatim."""
    exporter = build_otlp_exporter('http://tempo:4318', protocol='http/protobuf')
    assert exporter._endpoint == 'http://tempo:4318/v1/traces'


def test_grpc_metadata_keys_are_lowercased():
    """gRPC rejects a capitalised metadata key in the channel, before the request is
    sent: every batch fails with 'Invalid metadata' and nothing reaches the collector."""
    exporter = build_otlp_exporter(
        'localhost:4317', protocol='grpc', username='user', password='pass',
        headers='X-Scope-OrgID=tenant',
    )
    keys = [key for key, _ in exporter._headers]
    assert keys == [key.lower() for key in keys], keys
    assert ('authorization', expected_basic('user', 'pass')) in exporter._headers


def test_unknown_protocol_fails_loudly():
    with pytest.raises(ValueError, match='unknown otlp_protocol'):
        build_otlp_exporter('http://tempo:4318', protocol='thrift')


def test_standard_otel_env_names_are_fallbacks(monkeypatch):
    """A deploy configured for OpenTelemetry auto-instrumentation sets the OTEL_*
    names; ignoring them reads as "tracing is configured" while nothing exports."""
    from wan.os_env import _otlp_endpoint_from_env

    for name in ('OTLP_ENDPOINT', 'OTLP_URL',
                 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'OTEL_EXPORTER_OTLP_ENDPOINT'):
        monkeypatch.delenv(name, raising=False)
    assert _otlp_endpoint_from_env() is None

    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://generic:4318')
    assert _otlp_endpoint_from_env() == 'http://generic:4318'

    # The signal-specific name outranks the generic one, same as in the SDK.
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'http://traces:4318')
    assert _otlp_endpoint_from_env() == 'http://traces:4318'

    # And wan's own names outrank both.
    monkeypatch.setenv('OTLP_URL', 'http://url:4318')
    assert _otlp_endpoint_from_env() == 'http://url:4318'
    monkeypatch.setenv('OTLP_ENDPOINT', 'http://endpoint:4318')
    assert _otlp_endpoint_from_env() == 'http://endpoint:4318'


class Ingest(BaseHTTPRequestHandler):
    """A stand-in for the remote collector, recording what the exporter sent."""

    received = []

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        Ingest.received.append({
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'content_type': self.headers.get('Content-Type'),
            'body': self.rfile.read(length),
        })
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def ingest():
    Ingest.received = []
    server = HTTPServer(('127.0.0.1', 0), Ingest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_spans_reach_an_authenticated_http_ingest(ingest):
    """End to end over the wire: one span, pushed as protobuf with basic auth."""
    host, port = ingest.server_address
    exporter = build_otlp_exporter(
        f'http://{host}:{port}/insert/opentelemetry',
        protocol='http',
        username='user',
        password='pass',
    )
    # A local provider, not the global one: setup_tracing() installs process-wide
    # state that would leak into every other test.
    provider = TracerProvider(resource=build_resource('test-service'))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.get_tracer(__name__).start_span('unit').end()
    assert provider.force_flush(timeout_millis=5000)
    provider.shutdown()

    assert len(Ingest.received) == 1
    sent = Ingest.received[0]
    assert sent['path'] == '/insert/opentelemetry/v1/traces'
    assert sent['authorization'] == expected_basic('user', 'pass')
    assert sent['content_type'] == 'application/x-protobuf'
    # The service name travels as a resource attribute, so the payload is real.
    assert b'test-service' in sent['body']
