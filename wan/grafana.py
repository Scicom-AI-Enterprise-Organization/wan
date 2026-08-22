"""Grafana Explore deep links.

Used to put clickable Loki and Tempo links onto Sentry events, so an issue is one click
from the logs and the trace behind it. Also importable for building links yourself.

Grafana's Explore state is a JSON blob in the query string; one entry per `panes` key
gives one pane, so two keys renders logs and the flame graph side by side.
"""

import json
import urllib.parse
from typing import Any, Dict, Optional

LOKI_DATASOURCE = {'type': 'loki', 'uid': 'loki'}
TEMPO_DATASOURCE = {'type': 'tempo', 'uid': 'tempo'}

DEFAULT_RANGE = {'from': 'now-1h', 'to': 'now'}


def parse_explore_url(url: Optional[str]):
    """Read a Grafana base URL and datasource out of a link copied from Explore.

    Grafana's own address bar is the only place a datasource uid is on show without
    an admin API call, so pasting one Explore link is the least error-prone way to
    aim these links at a backend that is not the local stack.

    Returns ``(base_url, datasource)``, either half None when the URL does not carry
    it. The datasource is the ``{'type': ..., 'uid': ...}`` dict Grafana itself uses.
    """
    if not url:
        return None, None

    parsed = urllib.parse.urlsplit(url)
    base_url = (
        urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
        if parsed.scheme and parsed.netloc else None
    )

    panes = urllib.parse.parse_qs(parsed.query).get('panes')
    if not panes:
        return base_url, None
    try:
        decoded = json.loads(panes[0])
    except ValueError:
        return base_url, None

    for pane in (decoded or {}).values():
        for query in (pane or {}).get('queries') or []:
            # The query's datasource carries the type too; the pane's is a bare uid.
            datasource = (query or {}).get('datasource')
            if isinstance(datasource, dict) and datasource.get('uid'):
                return base_url, {
                    'type': datasource.get('type'), 'uid': datasource['uid'],
                }
        if isinstance((pane or {}).get('datasource'), str):
            return base_url, {'type': None, 'uid': pane['datasource']}
    return base_url, None


def explore_url(base_url: str, panes: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode({
        'schemaVersion': '1',
        'orgId': '1',
        'panes': json.dumps(panes, separators=(',', ':')),
    })
    return f'{base_url.rstrip("/")}/explore?{query}'


def loki_pane(
    expr: str,
    time_range: Optional[Dict[str, str]] = None,
    datasource: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    datasource = datasource or LOKI_DATASOURCE
    return {
        'datasource': datasource['uid'],
        'queries': [{'refId': 'A', 'datasource': datasource,
                     'editorMode': 'code', 'queryType': 'range', 'expr': expr}],
        'range': time_range or DEFAULT_RANGE,
    }


def trace_pane(query: str, query_type: str = 'traceql',
               time_range: Optional[Dict[str, str]] = None,
               datasource: Optional[Dict[str, Any]] = None, **extra) -> Dict[str, Any]:
    """One trace-backend pane, for Tempo or for a Jaeger-API backend.

    VictoriaTraces and Jaeger itself are read through Grafana's `jaeger` datasource,
    whose query model has no queryType: the `query` field alone is the trace id.
    Sending Tempo's `queryType: traceql` to one of those yields an empty pane, so
    the query is shaped from the datasource type rather than assumed.
    """
    datasource = datasource or TEMPO_DATASOURCE
    entry = {'refId': 'A', 'datasource': datasource, 'query': query}
    if datasource.get('type') in (None, 'tempo'):
        entry['queryType'] = query_type
    entry.update(extra)
    return {
        'datasource': datasource['uid'],
        'queries': [entry],
        'range': time_range or DEFAULT_RANGE,
    }


#: Kept under its old name: it is the Tempo-shaped pane, which is still the default.
tempo_pane = trace_pane


def logs_expr(
    selector: str = '{job="fastapi"}',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """LogQL for one request.

    Prefers the correlation id: it exists even when tracing is sampled out, and it spans
    every service the request touched.
    """
    if correlation_id:
        return f'{selector} | correlation_id="{correlation_id}"'
    if trace_id:
        return f'{selector} |= "{trace_id}"'
    return None


def logs_url(
    base_url: str,
    selector: str = '{job="fastapi"}',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    logs_datasource: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Explore link to the log lines for one request."""
    expr = logs_expr(selector, correlation_id, trace_id)
    if expr is None:
        return None
    return explore_url(base_url, {'lg': loki_pane(expr, datasource=logs_datasource)})


def trace_url(
    base_url: str,
    trace_id: Optional[str],
    trace_datasource: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    if not trace_id:
        return None
    return explore_url(
        base_url, {'tr': trace_pane(trace_id, datasource=trace_datasource)},
    )


def logs_and_trace_url(
    base_url: str,
    selector: str = '{job="fastapi"}',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    logs_datasource: Optional[Dict[str, Any]] = None,
    trace_datasource: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Both in one Explore view: logs on the left, flame graph on the right."""
    if not trace_id:
        return logs_url(base_url, selector, correlation_id, trace_id, logs_datasource)
    expr = logs_expr(selector, correlation_id, trace_id)
    return explore_url(base_url, {
        'lg': loki_pane(expr, datasource=logs_datasource),
        'tr': trace_pane(trace_id, datasource=trace_datasource),
    })


def dashboard_url(base_url: str, uid: str = 'wan') -> str:
    return f'{base_url.rstrip("/")}/d/{uid}?from=now-1h&to=now'


def links_for(
    base_url: str,
    selector: str = '{job="fastapi"}',
    dashboard_uid: str = 'wan',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    logs_datasource: Optional[Dict[str, Any]] = None,
    trace_datasource: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Every relevant link for one request, ready to attach to an error report."""
    candidates = {
        'logs_and_trace': logs_and_trace_url(
            base_url, selector, correlation_id, trace_id,
            logs_datasource, trace_datasource,
        ),
        'logs': logs_url(base_url, selector, correlation_id, trace_id, logs_datasource),
        'trace': trace_url(base_url, trace_id, trace_datasource),
        'dashboard': dashboard_url(base_url, dashboard_uid) if dashboard_uid else None,
    }
    return {name: url for name, url in candidates.items() if url}
