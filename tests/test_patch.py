"""Behaviour of patch(): request logs, correlation ids, trace ids, docs, metrics."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_loki_tempo
from tests.test_logs import REFERENCE_REQUEST_KEYS

HEX32 = re.compile(r'^[0-9a-f]{32}$')
HEX16 = re.compile(r'^[0-9a-f]{16}$')


def test_request_line_starts_with_the_documented_keys_in_order(client):
    test_client, capture = client
    test_client.get('/ping')
    request_logs = capture.of_type('request')
    assert len(request_logs) == 1
    assert list(request_logs[0])[:len(REFERENCE_REQUEST_KEYS)] == REFERENCE_REQUEST_KEYS


def test_request_line_reports_the_real_request(client):
    test_client, capture = client
    test_client.get('/ping')
    entry = capture.of_type('request')[0]
    assert entry['request'] == '/ping'
    assert entry['method'] == 'GET'
    assert entry['response_status'] == 200
    assert entry['protocol'] == 'HTTP/1.1'
    assert entry['response_content_type'].startswith('application/json')
    assert entry['request_route'] == '/ping'
    assert entry['level'] == 'INFO'
    assert entry['response_time_ms'] >= 0


def test_trace_ids_are_zero_padded_hex(client):
    """hex(trace_id)[2:] drops leading zeros; Tempo then cannot find the trace."""
    test_client, capture = client
    test_client.get('/ping')
    for entry in capture.objects():
        if entry.get('traceID') is None:
            continue
        assert HEX32.match(entry['traceID']), entry['traceID']
        assert HEX16.match(entry['spanID']), entry['spanID']
        assert entry['trace_message'] == f"traceID={entry['traceID']}"
        assert entry['dd.trace_id'].isdigit()


def test_handler_logs_share_the_trace_id_of_their_request(client):
    test_client, capture = client
    test_client.get('/ping')
    handler_log = [o for o in capture.of_type('log') if o['msg'] == 'pinged'][0]
    request_log = capture.of_type('request')[0]
    assert handler_log['traceID'] is not None
    assert handler_log['traceID'] == request_log['traceID']
    assert handler_log['correlation_id'] == request_log['correlation_id']


def test_correlation_id_is_generated_and_returned(client):
    test_client, capture = client
    response = test_client.get('/ping')
    correlation_id = response.headers['x-correlation-id']
    assert correlation_id
    assert capture.of_type('request')[0]['correlation_id'] == correlation_id


def test_inbound_correlation_id_is_reused(client):
    test_client, capture = client
    response = test_client.get('/ping', headers={'X-Correlation-ID': 'from-upstream'})
    assert response.headers['x-correlation-id'] == 'from-upstream'
    assert capture.of_type('request')[0]['correlation_id'] == 'from-upstream'


def test_trace_id_response_header_is_set(client):
    test_client, _ = client
    response = test_client.get('/ping')
    assert HEX32.match(response.headers['x-trace-id'])


def test_unhandled_exception_is_logged_as_500_with_traceback(client):
    test_client, capture = client
    test_client.get('/kaboom')
    entry = capture.of_type('request')[0]
    assert entry['response_status'] == 500
    assert entry['level'] == 'ERROR'
    tracebacks = [o for o in capture.objects() if 'RuntimeError: nope' in str(o.get('exc_info'))]
    assert tracebacks, 'traceback should be logged'
    assert tracebacks[0]['traceID'] is not None


def test_4xx_is_logged_at_warning(client):
    test_client, capture = client
    test_client.get('/nope-does-not-exist')
    entry = capture.of_type('request')[0]
    assert entry['response_status'] == 404
    assert entry['level'] == 'WARNING'


def test_health_endpoints_are_not_request_logged(client):
    test_client, capture = client
    for path in ('/healthz', '/livez', '/readyz'):
        assert test_client.get(path).status_code == 200
    assert capture.of_type('request') == []


def test_metrics_endpoint_exposes_prometheus(client):
    test_client, _ = client
    response = test_client.get('/metrics')
    assert response.status_code == 200
    assert 'http_requests_total' in response.text


def test_metrics_can_be_disabled(make_app):
    app, _ = make_app(enable_prometheus_metrics=False)
    with TestClient(app) as test_client:
        # The reference accepted this flag and ignored it.
        assert test_client.get('/metrics').status_code == 404


def test_scalar_page_is_served(client):
    test_client, _ = client
    response = test_client.get('/scalar')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert '/openapi.json' in response.text
    assert 'createApiReference' in response.text
    # Pinned, not @latest: an upstream release must not break deployed docs.
    assert f'@scalar/api-reference@{fastapi_loki_tempo.scalar.SCALAR_VERSION}' in response.text


def test_scalar_can_be_disabled(make_app):
    app, _ = make_app(enable_scalar_doc=False)
    with TestClient(app) as test_client:
        assert test_client.get('/scalar').status_code == 404


def test_scalar_endpoint_is_configurable(make_app):
    app, _ = make_app(scalar_doc_endpoint='/docs-v2', scalar_theme='moon')
    with TestClient(app) as test_client:
        response = test_client.get('/docs-v2')
        assert response.status_code == 200
        assert '"theme": "moon"' in response.text


def test_request_log_can_be_disabled(make_app):
    app, capture = make_app(enable_request_log=False)
    with TestClient(app) as test_client:
        test_client.get('/ping')
    assert capture.of_type('request') == []
    assert capture.of_type('log')  # handler logs still flow


def test_invalid_tracing_sample_is_rejected():
    with pytest.raises(ValueError, match='tracing_sample'):
        fastapi_loki_tempo.patch(app=FastAPI(), tracing_sample=0)
    with pytest.raises(ValueError, match='tracing_sample'):
        fastapi_loki_tempo.patch(app=FastAPI(), tracing_sample=1.5)


def test_otlp_and_jaeger_together_is_rejected():
    with pytest.raises(ValueError, match='same time'):
        fastapi_loki_tempo.patch(
            app=FastAPI(), otlp_endpoint='http://localhost:4317', jaeger_host='localhost',
        )


def test_patching_twice_is_a_no_op(make_app):
    app, _ = make_app()
    before = len(app.user_middleware)
    fastapi_loki_tempo.patch(app=app)
    assert len(app.user_middleware) == before


def test_correlation_headers_helper_is_empty_outside_a_request():
    assert fastapi_loki_tempo.correlation_headers() == {}
