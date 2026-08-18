"""Sentry integration, verified against a mock Sentry endpoint.

The thing worth testing is not that events arrive, it is that they arrive carrying the
*same* ids as Loki and Tempo. Sentry mints its own trace id by default, and its ASGI
middleware wraps the entire app -- outside every user middleware -- so a naive
integration captures events after the correlation ContextVar has been reset and the
OpenTelemetry span has ended, producing an issue that cannot be matched to anything.
"""

import gzip
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import sentry_sdk
from fastapi.testclient import TestClient

import wan
from wan import sentry as sentry_module

CID = 'SENTRY-CID-42'


class _Captured:
    def __init__(self):
        self.envelopes = []

    def events(self):
        return [
            payload
            for envelope in self.envelopes
            for header, payload in envelope
            if header.get('type') == 'event'
        ]


def _handler_for(captured):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('content-length', 0)))
            if self.headers.get('content-encoding') == 'gzip':
                body = gzip.decompress(body)
            # An envelope is newline-delimited JSON: envelope header, then pairs of
            # (item header, item payload).
            lines = body.decode('utf-8', 'replace').splitlines()
            items, index = [], 1
            while index < len(lines) - 1:
                try:
                    items.append((json.loads(lines[index]), json.loads(lines[index + 1])))
                except json.JSONDecodeError:
                    pass
                index += 2
            captured.envelopes.append(items)
            self.send_response(200)
            self.send_header('content-length', '2')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def sentry_app(make_app):
    """A patched app wired to a throwaway local Sentry endpoint."""
    captured = _Captured()
    server = HTTPServer(('127.0.0.1', 0), _handler_for(captured))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dsn = f'http://publickey@127.0.0.1:{server.server_address[1]}/1'

    sentry_module.reset()
    app, log_capture = make_app(
        sentry_dsn=dsn, sentry_environment='test', sentry_release='1.2.3',
    )

    @app.get('/card-fail')
    async def card_fail():
        logging.getLogger('billing').info('about to fail')
        raise ValueError('card processor exploded')

    yield app, captured, log_capture

    # Global SDK state: disable it again so later tests do not ship events anywhere.
    sentry_sdk.flush(timeout=5)
    sentry_sdk.init(dsn=None)
    sentry_module.reset()
    server.shutdown()
    server.server_close()


def test_sentry_is_off_without_a_dsn(make_app):
    app, _ = make_app(sentry_dsn=None)
    assert app.state.wan['sentry_enabled'] is False


def test_error_reaches_sentry_once(sentry_app):
    """Exactly one event: our request logger logs 5xx at ERROR with a traceback, and
    Sentry's FastAPI integration captures the same exception. ignore_logger() stops
    that arriving twice."""
    app, captured, _ = sentry_app
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get('/card-fail', headers={'X-Correlation-ID': CID})
    sentry_sdk.flush(timeout=10)

    events = captured.events()
    assert len(events) == 1, f'expected 1 event, got {len(events)}'
    exception = events[0]['exception']['values'][-1]
    assert exception['type'] == 'ValueError'
    assert exception['value'] == 'card processor exploded'


def test_event_carries_the_correlation_id_as_a_tag(sentry_app):
    app, captured, _ = sentry_app
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get('/card-fail', headers={'X-Correlation-ID': CID})
    sentry_sdk.flush(timeout=10)

    tags = captured.events()[0]['tags']
    # Searchable in Sentry as `correlation_id:SENTRY-CID-42`, which is the pivot to Loki.
    assert tags['correlation_id'] == CID
    assert tags['service'] == 'test-service'


def test_sentry_trace_id_is_the_opentelemetry_trace_id(sentry_app):
    """Without this, a Sentry issue cannot be matched to a Tempo trace at all."""
    app, captured, log_capture = sentry_app
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get('/card-fail', headers={'X-Correlation-ID': CID})
    sentry_sdk.flush(timeout=10)

    event = captured.events()[0]
    # The id this request actually logged, taken from the request log line.
    logged_trace = log_capture.of_type('request')[0]['traceID']
    assert logged_trace

    assert event['tags']['traceID'] == logged_trace
    assert event['contexts']['trace']['trace_id'] == logged_trace, (
        'Sentry kept its own trace id; the issue cannot be correlated to Tempo'
    )


def test_successful_request_produces_no_event(sentry_app):
    app, captured, _ = sentry_app

    @app.get('/fine')
    async def fine():
        logging.getLogger('billing').info('all good')
        return {'ok': True}

    with TestClient(app) as client:
        client.get('/fine')
    sentry_sdk.flush(timeout=10)
    assert captured.events() == []


def test_log_breadcrumbs_are_attached(sentry_app):
    """INFO logs become breadcrumbs, so the event shows what happened before the error."""
    app, captured, _ = sentry_app
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get('/card-fail', headers={'X-Correlation-ID': CID})
    sentry_sdk.flush(timeout=10)

    event = captured.events()[0]
    crumbs = event.get('breadcrumbs', {})
    messages = [c.get('message') for c in crumbs.get('values', crumbs if isinstance(crumbs, list) else [])]
    assert 'about to fail' in messages


def test_event_carries_clickable_grafana_links(make_app):
    """A Sentry issue is one click from its logs and its trace."""
    captured = _Captured()
    server = HTTPServer(('127.0.0.1', 0), _handler_for(captured))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sentry_module.reset()
    app, _ = make_app(
        sentry_dsn=f'http://k@127.0.0.1:{server.server_address[1]}/1',
        grafana_url='http://grafana.example.com:3010',
        grafana_loki_selector='{job="fastapi"}',
        grafana_dashboard_uid='wan',
    )

    @app.get('/link-fail')
    async def link_fail():
        raise ValueError('boom')

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get('/link-fail', headers={'X-Correlation-ID': CID})
        sentry_sdk.flush(timeout=10)

        event = captured.events()[0]
        links = event['contexts']['grafana']
        assert set(links) == {'logs_and_trace', 'logs', 'trace', 'dashboard'}
        # Also in Additional Data, since Sentry versions differ on what they linkify.
        assert event['extra']['grafana_logs'] == links['logs']

        import json as _json
        import urllib.parse as _url
        panes = _json.loads(
            _url.parse_qs(_url.urlparse(links['logs_and_trace']).query)['panes'][0])
        # Must filter by correlation id, not fall back to a raw trace-id substring:
        # before_send runs after the request, so the id has to come off the scope.
        assert panes['lg']['queries'][0]['expr'] == (
            f'{{job="fastapi"}} | correlation_id="{CID}"')
        assert panes['tr']['queries'][0]['query'] == event['tags']['traceID']
        assert links['dashboard'].startswith('http://grafana.example.com:3010/d/wan')
    finally:
        sentry_sdk.flush(timeout=5)
        sentry_sdk.init(dsn=None)
        sentry_module.reset()
        server.shutdown()
        server.server_close()


def test_no_grafana_context_without_a_url(sentry_app):
    """GRAFANA_URL unset: no half-built links pointing at nothing."""
    app, captured, _ = sentry_app
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get('/card-fail', headers={'X-Correlation-ID': CID})
    sentry_sdk.flush(timeout=10)
    assert 'grafana' not in captured.events()[0].get('contexts', {})
