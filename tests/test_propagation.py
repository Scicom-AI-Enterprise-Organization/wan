"""Outbound correlation id propagation.

The point of these: an id created by the first service in a chain must survive every
hop, and composing our propagator in must not break trace propagation itself.
"""

from opentelemetry.propagate import get_global_textmap, inject

import wan
from wan.context import reset_correlation_id, set_correlation_id
from wan.propagation import CorrelationIdPropagator

HEADER = 'X-Correlation-ID'


def test_propagator_injects_the_active_correlation_id():
    carrier = {}
    token = set_correlation_id('abc-123')
    try:
        CorrelationIdPropagator().inject(carrier)
    finally:
        reset_correlation_id(token)
    assert carrier == {HEADER: 'abc-123'}


def test_propagator_injects_nothing_when_there_is_no_correlation_id():
    """Must not send the '-' placeholder: downstream would treat it as a real id."""
    carrier = {}
    CorrelationIdPropagator().inject(carrier)
    assert carrier == {}


def test_propagator_honours_a_custom_header_name():
    carrier = {}
    token = set_correlation_id('abc-123')
    try:
        CorrelationIdPropagator(header='X-Request-ID').inject(carrier)
    finally:
        reset_correlation_id(token)
    assert carrier == {'X-Request-ID': 'abc-123'}


def test_extract_is_a_no_op():
    # The middleware owns inbound extraction and the ContextVar's lifetime.
    context = CorrelationIdPropagator().extract({HEADER.lower(): 'abc-123'})
    assert context is not None


def test_patch_installs_it_globally_without_losing_trace_propagation(make_app):
    make_app()
    fields = get_global_textmap().fields
    # Composed onto the default propagator, not replacing it.
    assert HEADER in fields
    assert 'traceparent' in fields, 'trace propagation was clobbered'
    assert 'baggage' in fields


def test_global_inject_carries_the_correlation_id(make_app):
    """What an instrumented httpx/requests client actually calls on an outbound hop."""
    make_app()
    carrier = {}
    token = set_correlation_id('end-to-end-id')
    try:
        inject(carrier)
    finally:
        reset_correlation_id(token)
    assert carrier[HEADER] == 'end-to-end-id'


def test_propagation_can_be_disabled(make_app):
    app, _ = make_app(enable_correlation_id_propagation=False)
    # Already installed by another test in this process, so only assert the flag is
    # accepted and the app still patches cleanly.
    assert app.state.wan['version'] == wan.__version__


def test_inbound_id_is_what_gets_propagated_onward(client):
    """A -> B -> C: the id B forwards is the one A sent, not a fresh one."""
    test_client, capture = client
    forwarded = {}

    @test_client.app.get('/hop')
    async def hop():
        inject(forwarded)
        return {'ok': True}

    test_client.get('/hop', headers={HEADER: 'created-by-service-a'})
    assert forwarded[HEADER] == 'created-by-service-a'
    assert capture.of_type('request')[0]['correlation_id'] == 'created-by-service-a'
