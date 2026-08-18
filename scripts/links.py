#!/usr/bin/env python3
"""Print deep links into Grafana for a trace or a correlation id.

    python3 scripts/links.py                      # newest trace in Tempo
    python3 scripts/links.py --trace <traceID>
    python3 scripts/links.py --correlation <id>
    python3 scripts/links.py --service service-a

Hand-building these URLs is fiddly -- Grafana's Explore state is a JSON blob in the
query string -- so this generates them instead of documenting a format that drifts.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit('/', 1)[0])

from e2e import GRAFANA, TEMPO, get_json  # noqa: E402

DEFAULT_RANGE = {'from': 'now-1h', 'to': 'now'}

LOKI_DS = {'type': 'loki', 'uid': 'loki'}
TEMPO_DS = {'type': 'tempo', 'uid': 'tempo'}


def explore_url(panes, grafana=GRAFANA):
    """Grafana 10.2+ Explore URL: one entry in `panes` per side-by-side pane."""
    query = urllib.parse.urlencode({
        'schemaVersion': '1',
        'orgId': '1',
        'panes': json.dumps(panes, separators=(',', ':')),
    })
    return f'{grafana}/explore?{query}'


def tempo_pane(query, query_type='traceql', time_range=None, **extra):
    return {
        'datasource': 'tempo',
        'queries': [dict({'refId': 'A', 'datasource': TEMPO_DS,
                          'queryType': query_type, 'query': query}, **extra)],
        'range': time_range or DEFAULT_RANGE,
    }


def loki_pane(expr, time_range=None):
    return {
        'datasource': 'loki',
        'queries': [{'refId': 'A', 'datasource': LOKI_DS, 'editorMode': 'code',
                     'queryType': 'range', 'expr': expr}],
        'range': time_range or DEFAULT_RANGE,
    }


def newest_trace(service=None):
    query = f'{{resource.service.name="{service}"}}' if service else '{}'
    params = urllib.parse.urlencode({'q': query, 'limit': 1})
    traces = get_json(f'{TEMPO}/api/search?{params}').get('traces') or []
    return traces[0]['traceID'] if traces else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', help='trace id; default is the newest in Tempo')
    parser.add_argument('--correlation', help='correlation id to filter logs by')
    parser.add_argument('--service', help='restrict trace discovery/search to a service')
    parser.add_argument('--grafana', default=GRAFANA,
                        help='Grafana base URL to build links against, e.g. '
                             'http://192.168.1.10:3010 for links you can send someone')
    args = parser.parse_args()
    grafana = args.grafana.rstrip('/')

    trace_id = args.trace or newest_trace(args.service)
    if not trace_id:
        print('no traces in Tempo yet -- send some traffic first', file=sys.stderr)
        return 1

    log_filter = (
        f'{{job="fastapi"}} | correlation_id="{args.correlation}"'
        if args.correlation else
        f'{{job="fastapi"}} |= "{trace_id}"'
    )

    links = [
        ('Tempo, this trace', explore_url({'tr': tempo_pane(trace_id)}, grafana)),
        ('Loki, its logs', explore_url({'lg': loki_pane(log_filter)}, grafana)),
        ('Loki + Tempo side by side',
         explore_url({'lg': loki_pane(log_filter), 'tr': tempo_pane(trace_id)}, grafana)),
        ('Tempo, TraceQL search',
         explore_url({'sr': tempo_pane(
             f'{{resource.service.name="{args.service}"}}' if args.service else '{}',
             limit=20, tableType='traces')}, grafana)),
        ('Tempo, service graph',
         explore_url({'sg': tempo_pane('', query_type='serviceMap')}, grafana)),
        ('Dashboard', f'{grafana}/d/wan?from=now-1h&to=now'),
    ]

    print(f'trace id       : {trace_id}')
    if args.correlation:
        print(f'correlation id : {args.correlation}')
    print()
    for name, url in links:
        print(f'{name}\n  {url}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
