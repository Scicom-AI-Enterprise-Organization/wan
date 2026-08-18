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


def explore_url(base_url: str, panes: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode({
        'schemaVersion': '1',
        'orgId': '1',
        'panes': json.dumps(panes, separators=(',', ':')),
    })
    return f'{base_url.rstrip("/")}/explore?{query}'


def loki_pane(expr: str, time_range: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        'datasource': 'loki',
        'queries': [{'refId': 'A', 'datasource': LOKI_DATASOURCE,
                     'editorMode': 'code', 'queryType': 'range', 'expr': expr}],
        'range': time_range or DEFAULT_RANGE,
    }


def tempo_pane(query: str, query_type: str = 'traceql',
               time_range: Optional[Dict[str, str]] = None, **extra) -> Dict[str, Any]:
    return {
        'datasource': 'tempo',
        'queries': [dict({'refId': 'A', 'datasource': TEMPO_DATASOURCE,
                          'queryType': query_type, 'query': query}, **extra)],
        'range': time_range or DEFAULT_RANGE,
    }


def logs_url(
    base_url: str,
    selector: str = '{job="fastapi"}',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """Explore link to the log lines for one request.

    Prefers the correlation id: it exists even when tracing is sampled out, and it spans
    every service the request touched.
    """
    if correlation_id:
        expr = f'{selector} | correlation_id="{correlation_id}"'
    elif trace_id:
        expr = f'{selector} |= "{trace_id}"'
    else:
        return None
    return explore_url(base_url, {'lg': loki_pane(expr)})


def trace_url(base_url: str, trace_id: Optional[str]) -> Optional[str]:
    if not trace_id:
        return None
    return explore_url(base_url, {'tr': tempo_pane(trace_id)})


def logs_and_trace_url(
    base_url: str,
    selector: str = '{job="fastapi"}',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """Both in one Explore view: logs on the left, flame graph on the right."""
    if not trace_id:
        return logs_url(base_url, selector, correlation_id, trace_id)
    expr = (
        f'{selector} | correlation_id="{correlation_id}"' if correlation_id
        else f'{selector} |= "{trace_id}"'
    )
    return explore_url(base_url, {'lg': loki_pane(expr), 'tr': tempo_pane(trace_id)})


def dashboard_url(base_url: str, uid: str = 'wan') -> str:
    return f'{base_url.rstrip("/")}/d/{uid}?from=now-1h&to=now'


def links_for(
    base_url: str,
    selector: str = '{job="fastapi"}',
    dashboard_uid: str = 'wan',
    correlation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, str]:
    """Every relevant link for one request, ready to attach to an error report."""
    candidates = {
        'logs_and_trace': logs_and_trace_url(base_url, selector, correlation_id, trace_id),
        'logs': logs_url(base_url, selector, correlation_id, trace_id),
        'trace': trace_url(base_url, trace_id),
        'dashboard': dashboard_url(base_url, dashboard_uid),
    }
    return {name: url for name, url in candidates.items() if url}
