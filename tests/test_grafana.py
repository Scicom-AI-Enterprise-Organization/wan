"""Explore deep links: which datasource they open and how they query it."""

import json
import urllib.parse

import pytest

from wan.grafana import (
    links_for,
    logs_url,
    parse_explore_url,
    trace_pane,
    trace_url,
)

VICTORIA = {'type': 'jaeger', 'uid': 'victoria-traces'}

# A link copied straight out of a Grafana Explore address bar.
EXPLORE_URL = (
    'https://grafana.example.com/explore?schemaVersion=1&panes=%7B%22j6s%22%3A%7B%22'
    'datasource%22%3A%22victoria-traces%22%2C%22queries%22%3A%5B%7B%22refId%22%3A%22A'
    '%22%2C%22datasource%22%3A%7B%22type%22%3A%22jaeger%22%2C%22uid%22%3A%22victoria-'
    'traces%22%7D%7D%5D%2C%22range%22%3A%7B%22from%22%3A%22now-1h%22%2C%22to%22%3A%22'
    'now%22%7D%2C%22compact%22%3Afalse%7D%7D&orgId=1'
)


def panes_of(url):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return json.loads(query['panes'][0])


def test_an_explore_link_yields_the_base_url_and_the_datasource():
    assert parse_explore_url(EXPLORE_URL) == (
        'https://grafana.example.com', VICTORIA,
    )


def test_a_plain_grafana_url_yields_only_the_base():
    assert parse_explore_url('https://grafana.example.com/') == (
        'https://grafana.example.com', None,
    )


@pytest.mark.parametrize('url', [None, '', 'not a url', 'https://g/explore?panes=%7Bbroken'])
def test_unparseable_input_never_raises(url):
    """This runs at import time off an environment variable; it must not crash a boot."""
    base, datasource = parse_explore_url(url)
    assert datasource is None


def test_tempo_panes_still_ask_for_traceql():
    pane = trace_pane('abc123')
    assert pane['datasource'] == 'tempo'
    assert pane['queries'][0]['queryType'] == 'traceql'
    assert pane['queries'][0]['query'] == 'abc123'


def test_a_jaeger_backend_is_queried_by_trace_id_with_no_query_type():
    """VictoriaTraces reads through Grafana's jaeger datasource, which has no
    queryType: sending Tempo's `traceql` there renders an empty pane."""
    pane = trace_pane('abc123', datasource=VICTORIA)
    assert pane['datasource'] == 'victoria-traces'
    assert pane['queries'][0] == {
        'refId': 'A', 'datasource': VICTORIA, 'query': 'abc123',
    }


def test_trace_url_opens_the_configured_datasource():
    url = trace_url('https://grafana.example.com', 'abc123', VICTORIA)
    assert url.startswith('https://grafana.example.com/explore?')
    assert panes_of(url)['tr']['queries'][0]['datasource'] == VICTORIA


def test_logs_url_opens_the_configured_datasource():
    logs = {'type': 'loki', 'uid': 'victoria-logs-loki'}
    url = logs_url('https://grafana.example.com', correlation_id='cid-1', logs_datasource=logs)
    pane = panes_of(url)['lg']
    assert pane['datasource'] == 'victoria-logs-loki'
    assert pane['queries'][0]['expr'] == '{job="fastapi"} | correlation_id="cid-1"'


def test_a_victorialogs_backend_is_queried_in_logsql():
    """The VictoriaLogs plugin speaks LogsQL: LogQL's `| field="v"` pipe is a parse
    error there (`unexpected pipe`), so the filter must be `field:"v"` instead."""
    logs = {'type': 'victoriametrics-logs-datasource', 'uid': 'victoria-logs'}
    url = logs_url('https://grafana.example.com', correlation_id='cid-1', logs_datasource=logs)
    pane = panes_of(url)['lg']
    assert pane['datasource'] == 'victoria-logs'
    assert pane['queries'][0]['expr'] == (
        '{job="fastapi"} (correlation_id:"cid-1" OR "cid-1")'
    )
    assert '|' not in pane['queries'][0]['expr']


def test_victorialogs_trace_id_matches_both_field_spellings():
    """wan's own lines carry traceID; an OTel-renaming pipeline carries trace_id.
    The link cannot know which shipped, so it must match either."""
    logs = {'type': 'victoriametrics-logs-datasource', 'uid': 'victoria-logs'}
    url = logs_url('https://grafana.example.com', trace_id='abc123', logs_datasource=logs)
    expr = panes_of(url)['lg']['queries'][0]['expr']
    assert 'trace_id:"abc123"' in expr
    assert 'traceID:"abc123"' in expr


def test_a_loki_backend_still_gets_logql():
    url = logs_url('https://grafana.example.com', correlation_id='cid-1')
    expr = panes_of(url)['lg']['queries'][0]['expr']
    assert expr == '{job="fastapi"} | correlation_id="cid-1"'


def test_links_for_carries_both_datasources_into_the_combined_pane():
    logs = {'type': 'loki', 'uid': 'victoria-logs-loki'}
    links = links_for(
        'https://grafana.example.com',
        correlation_id='cid-1',
        trace_id='abc123',
        logs_datasource=logs,
        trace_datasource=VICTORIA,
    )
    panes = panes_of(links['logs_and_trace'])
    assert panes['lg']['queries'][0]['datasource'] == logs
    assert panes['tr']['queries'][0]['datasource'] == VICTORIA
    assert 'queryType' not in panes['tr']['queries'][0]


def test_an_empty_dashboard_uid_drops_the_dashboard_link():
    """A remote Grafana need not host this dashboard; a dead link is worse than none."""
    links = links_for('https://grafana.example.com', trace_id='abc', dashboard_uid='')
    assert 'dashboard' not in links
    assert 'trace' in links
