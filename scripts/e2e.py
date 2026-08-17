#!/usr/bin/env python3
"""End-to-end verification of the whole stack. Standard library only.

Brings nothing up: start the stack first, then run this.

    docker compose -f grafana/docker-compose.yaml up -d --build
    python3 scripts/e2e.py

It asserts the things that actually matter and that are easy to get quietly wrong:

  * every log line is valid JSON and carries traceID / trace_message / dd.trace_id
  * a handler log and its request log share one trace id and one correlation id
  * that exact trace id resolves in Tempo          (the Loki -> Tempo direction)
  * that exact trace id finds the logs in Loki     (the Tempo -> Loki direction)
  * trace ids are 32 hex chars, so Tempo can find them
  * the Scalar page is served, pinned, and its bundle is reachable
  * Prometheus scrapes the app, and Tempo's generated span metrics arrive
  * Grafana can run each dashboard query through its own datasource proxy

Exits non-zero if anything fails.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(REPO, 'grafana', 'docker-compose.yaml')
ENV_FILE = os.path.join(REPO, 'grafana', '.env')

HEX32 = re.compile(r'^[0-9a-f]{32}$')
HEX16 = re.compile(r'^[0-9a-f]{16}$')

DEFAULT_PORTS = {
    'APP_PORT': '7072',
    'GRAFANA_PORT': '3000',
    'LOKI_PORT': '3100',
    'TEMPO_PORT': '3200',
    'PROMETHEUS_PORT': '9090',
    'ALLOY_PORT': '12345',
}
SERVICE_NAME = 'fastapi-loki-tempo'
GRAFANA_AUTH = ('admin', 'admin')

GREEN, RED, YELLOW, DIM, RESET = '\033[32m', '\033[31m', '\033[33m', '\033[2m', '\033[0m'


# --- config ------------------------------------------------------------------------
def load_ports():
    ports = dict(DEFAULT_PORTS)
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                if key.strip() in ports:
                    ports[key.strip()] = value.strip()
    # Real environment wins, same as docker compose.
    for key in ports:
        ports[key] = os.environ.get(key, ports[key])
    return ports


PORTS = load_ports()
APP = f"http://localhost:{PORTS['APP_PORT']}"
GRAFANA = f"http://localhost:{PORTS['GRAFANA_PORT']}"
LOKI = f"http://localhost:{PORTS['LOKI_PORT']}"
TEMPO = f"http://localhost:{PORTS['TEMPO_PORT']}"
PROM = f"http://localhost:{PORTS['PROMETHEUS_PORT']}"


# --- http --------------------------------------------------------------------------
def request(url, data=None, auth=None, timeout=20, method=None):
    headers = {'Accept': 'application/json'}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    if auth:
        token = base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        headers['Authorization'] = f'Basic {token}'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode('utf-8', 'replace')


def get_json(url, **kwargs):
    _, text = request(url, **kwargs)
    return json.loads(text)


def status_of(url, **kwargs):
    try:
        status, _ = request(url, **kwargs)
        return status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def loki_query(query, minutes=30, limit=200):
    now = int(time.time())
    params = urllib.parse.urlencode({
        'query': query,
        'start': f'{(now - minutes * 60)}000000000',
        'end': f'{now}000000000',
        'limit': limit,
    })
    data = get_json(f'{LOKI}/loki/api/v1/query_range?{params}')
    if data.get('status') != 'success':
        raise RuntimeError(f'loki query failed: {str(data)[:200]}')
    return [v for stream in data['data']['result'] for v in stream['values']]


def prom_query(query):
    params = urllib.parse.urlencode({'query': query})
    data = get_json(f'{PROM}/api/v1/query?{params}')
    if data.get('status') != 'success':
        raise RuntimeError(f'prometheus query failed: {str(data)[:200]}')
    return data['data']['result']


def grafana_query(datasource_type, uid, query_fields):
    """Run a query the way Grafana runs a dashboard panel, through its own proxy."""
    payload = {
        'from': 'now-30m',
        'to': 'now',
        'queries': [dict(
            {'refId': 'A', 'datasource': {'type': datasource_type, 'uid': uid}},
            **query_fields,
        )],
    }
    return get_json(f'{GRAFANA}/api/ds/query', data=payload, auth=GRAFANA_AUTH)


def wait_until(fn, timeout=90, interval=2):
    """Poll until fn() returns something truthy. Log pipelines are not instant."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as e:
            last_error = e
        time.sleep(interval)
    if last_error:
        raise last_error
    return None


# --- reporting ---------------------------------------------------------------------
RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or ''))
        print(f'{GREEN}PASS{RESET}  {name}' + (f'{DIM}  {detail}{RESET}' if detail else ''))
        return True
    except Exception as e:
        message = f'{type(e).__name__}: {e}'
        RESULTS.append((name, False, message))
        print(f'{RED}FAIL{RESET}  {name}\n        {RED}{message}{RESET}')
        return False


def section(title):
    print(f'\n{YELLOW}== {title} =={RESET}')


def assert_that(condition, message):
    if not condition:
        raise AssertionError(message)


# --- app log helpers ---------------------------------------------------------------
def app_log_objects():
    """JSON objects the app container has written to stdout."""
    out = subprocess.run(
        ['docker', 'compose', '-f', COMPOSE_FILE, 'logs', 'app', '--no-log-prefix', '--tail', '600'],
        capture_output=True, text=True, timeout=60,
    ).stdout
    objects, malformed = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            malformed.append(line)
    return objects, malformed


def generate_traffic():
    for _ in range(3):
        status_of(f'{APP}/random?minimum=0.05&maximum=0.2')
    status_of(f'{APP}/nested')
    status_of(f'{APP}/chain?depth=2')
    status_of(f'{APP}/not-found')
    status_of(f'{APP}/boom')
    status_of(f'{APP}/')


# --- checks ------------------------------------------------------------------------
def main():
    print(f'{DIM}app={APP} grafana={GRAFANA} loki={LOKI} tempo={TEMPO} prometheus={PROM}{RESET}')

    section('readiness')
    check('app is up', lambda: (
        assert_that(wait_until(lambda: status_of(f'{APP}/healthz') == 200), 'no /healthz'),
        'GET /healthz 200')[1])
    check('loki is ready', lambda: (
        assert_that(wait_until(lambda: status_of(f'{LOKI}/ready') == 200), 'not ready'),
        'GET /ready 200')[1])
    check('tempo is ready', lambda: (
        assert_that(wait_until(lambda: status_of(f'{TEMPO}/ready') == 200), 'not ready'),
        'GET /ready 200')[1])
    check('prometheus is ready', lambda: (
        assert_that(wait_until(lambda: status_of(f'{PROM}/-/ready') == 200), 'not ready'),
        'GET /-/ready 200')[1])
    check('grafana is ready', lambda: (
        assert_that(wait_until(lambda: status_of(f'{GRAFANA}/api/health') == 200), 'not ready'),
        'GET /api/health 200')[1])

    section('traffic')
    generate_traffic()
    print(f'{DIM}      hit /random x3, /nested, /chain?depth=2, /not-found, /boom, /{RESET}')

    section('log format')
    objects, malformed = app_log_objects()
    assert objects, 'no app logs found at all'

    def every_line_is_json():
        assert_that(not malformed, f'{len(malformed)} non-JSON lines, e.g. {malformed[:1]}')
        return f'{len(objects)} lines, 0 malformed'
    check('every app log line is valid JSON', every_line_is_json)

    def request_lines_exist():
        requests = [o for o in objects if o.get('type') == 'request']
        assert_that(requests, 'no type=request lines')
        return f'{len(requests)} type=request lines'
    check('request logs are emitted', request_lines_exist)

    def trace_fields_present():
        missing = [
            o for o in objects
            if not {'traceID', 'trace_message', 'dd.trace_id', 'spanID'} <= set(o)
        ]
        assert_that(not missing, f'{len(missing)} lines missing trace fields')
        return 'traceID, trace_message, dd.trace_id, spanID on every line'
    check('trace correlation fields on every line', trace_fields_present)

    def ids_are_well_formed():
        traced = [o for o in objects if o.get('traceID')]
        assert_that(traced, 'no line carried a trace id')
        for o in traced:
            assert_that(HEX32.match(o['traceID']), f"traceID not 32 hex: {o['traceID']}")
            assert_that(HEX16.match(o['spanID']), f"spanID not 16 hex: {o['spanID']}")
            assert_that(o['trace_message'] == f"traceID={o['traceID']}", 'trace_message mismatch')
            assert_that(str(o['dd.trace_id']).isdigit(), 'dd.trace_id not numeric')
        return f'{len(traced)} traced lines, all 32/16 hex'
    check('trace ids are zero-padded hex', ids_are_well_formed)

    def handler_and_request_logs_agree():
        by_trace = {}
        for o in objects:
            if o.get('traceID'):
                by_trace.setdefault(o['traceID'], []).append(o)
        shared = [
            group for group in by_trace.values()
            if {g.get('type') for g in group} >= {'log', 'request'}
        ]
        assert_that(shared, 'no trace id shared between a handler log and a request log')
        group = shared[0]
        correlation_ids = {g['correlation_id'] for g in group}
        assert_that(len(correlation_ids) == 1, f'correlation ids differ: {correlation_ids}')
        return f'{len(shared)} traces link handler logs to their request log'
    check('handler log and request log share trace + correlation id', handler_and_request_logs_agree)

    def chain_propagates():
        chains = {}
        for o in objects:
            if o.get('request') == '/chain' and o.get('traceID'):
                chains.setdefault(o['traceID'], []).append(o)
        multi = [t for t, v in chains.items() if len(v) >= 3]
        assert_that(multi, 'no trace covered all 3 hops of /chain?depth=2')
        return f'trace {multi[0][:12]}.. spans {len(chains[multi[0]])} hops'
    check('trace context propagates across service hops', chain_propagates)

    def error_is_captured():
        errors = [o for o in objects if o.get('request') == '/boom']
        assert_that(errors, 'no /boom request log')
        assert_that(any(o.get('response_status') == 500 for o in errors), 'no 500 logged')
        with_tb = [o for o in errors if 'ZeroDivisionError' in str(o.get('exc_info'))]
        assert_that(with_tb, 'traceback not attached to the request log')
        assert_that(with_tb[0].get('traceID'), 'traceback line has no trace id')
        return 'ZeroDivisionError traceback logged with trace id'
    check('unhandled error logged as 500 with traceback + trace id', error_is_captured)

    # The trace id used for the correlation checks below.
    traced = [o for o in objects if o.get('traceID') and o.get('type') == 'request']
    trace_id = traced[-1]['traceID']
    print(f'{DIM}      correlating on traceID={trace_id}{RESET}')

    section('tempo (traces)')

    def tempo_has_trace():
        data = wait_until(lambda: get_json(f'{TEMPO}/api/traces/{trace_id}'), timeout=60)
        batches = data.get('batches') or []
        spans = [s for b in batches for ss in b.get('scopeSpans', []) for s in ss.get('spans', [])]
        assert_that(spans, f'tempo returned no spans for {trace_id}')
        services = {
            a['value']['stringValue']
            for b in batches for a in b.get('resource', {}).get('attributes', [])
            if a['key'] == 'service.name'
        }
        assert_that(SERVICE_NAME in services, f'service.name missing, got {services}')
        return f'{len(spans)} spans, service.name={SERVICE_NAME}'
    check('trace id from the log resolves in Tempo', tempo_has_trace)

    def tempo_search_works():
        params = urllib.parse.urlencode({
            'q': f'{{resource.service.name="{SERVICE_NAME}"}}', 'limit': 20,
        })
        traces = wait_until(
            lambda: (get_json(f'{TEMPO}/api/search?{params}').get('traces') or None),
            timeout=60,
        )
        return f'TraceQL search returned {len(traces)} traces'
    check('TraceQL search finds the service', tempo_search_works)

    def tempo_records_the_error():
        params = urllib.parse.urlencode({
            'q': f'{{resource.service.name="{SERVICE_NAME}" && status=error}}', 'limit': 20,
        })
        traces = wait_until(
            lambda: (get_json(f'{TEMPO}/api/search?{params}').get('traces') or None),
            timeout=60,
        )
        return f'{len(traces)} error traces (the /boom span is marked failed)'
    check('failed request produces an error span', tempo_records_the_error)

    section('loki (logs)')

    def loki_has_app_logs():
        values = wait_until(lambda: loki_query('{job="fastapi"}') or None, timeout=120)
        return f'{len(values)} entries under {{job="fastapi"}}'
    check('app logs reach Loki', loki_has_app_logs)

    def loki_finds_the_trace():
        # This is literally the query the Tempo datasource's tracesToLogsV2 runs.
        values = wait_until(
            lambda: loki_query(f'{{job="fastapi"}} |= "{trace_id}"') or None, timeout=120,
        )
        return f'{len(values)} entries for that trace (Tempo -> Loki link)'
    check('Tempo -> Loki: trace id finds its log lines', loki_finds_the_trace)

    def derived_field_regex_matches():
        # And this is what the Loki datasource's derived field regex matches on.
        values = wait_until(
            lambda: loki_query(r'{job="fastapi"} |~ "traceID=[0-9a-f]{32}"') or None, timeout=120,
        )
        return f'{len(values)} lines match traceID=(\\w+) (Loki -> Tempo link)'
    check('Loki -> Tempo: derived field regex matches the raw line', derived_field_regex_matches)

    def structured_metadata_works():
        values = wait_until(
            lambda: loki_query(f'{{job="fastapi"}} | traceID="{trace_id}"') or None, timeout=120,
        )
        labels = get_json(f'{LOKI}/loki/api/v1/labels')['data']
        assert_that('traceID' not in labels, 'traceID became a label; that is a cardinality bomb')
        return f'{len(values)} entries via structured metadata, traceID is not a label'
    check('traceID is structured metadata, not a label', structured_metadata_works)

    def loki_parses_json():
        values = wait_until(
            lambda: loki_query('{job="fastapi", log_type="request"} | json') or None, timeout=120,
        )
        return f'{len(values)} request lines parse with | json'
    check('log_type label and | json parsing work', loki_parses_json)

    def loki_levels():
        values = wait_until(
            lambda: loki_query('{job="fastapi", level=~"WARNING|ERROR"}') or None, timeout=120,
        )
        return f'{len(values)} warning/error entries'
    check('level label is set on request logs too', loki_levels)

    def only_this_project_is_scraped():
        data = get_json(f'{LOKI}/loki/api/v1/label/compose_service/values')
        services = set(data.get('data') or [])
        expected = {'app', 'tempo', 'loki', 'alloy', 'prometheus', 'grafana'}
        stray = services - expected
        assert_that(not stray, f'Alloy is scraping unrelated containers: {sorted(stray)}')
        return f'compose_service values: {sorted(services)}'
    check('Alloy scrapes only this compose project', only_this_project_is_scraped)

    section('scalar (api docs)')

    def scalar_is_served():
        status, html = request(f'{APP}/scalar')
        assert_that(status == 200, f'status {status}')
        assert_that('createApiReference' in html, 'no createApiReference call')
        assert_that('scalar-app' in html, 'no mount point')
        return f'{len(html)} bytes of HTML'
    check('/scalar returns the reference page', scalar_is_served)

    def scalar_bundle_is_pinned_and_reachable():
        _, html = request(f'{APP}/scalar')
        match = re.search(r'<script src="([^"]+)"', html)
        assert_that(match, 'no script tag')
        url = match.group(1)
        assert_that('@latest' not in url, f'bundle is not pinned: {url}')
        assert_that(re.search(r'@scalar/api-reference@\d+\.\d+\.\d+', url), f'no version pin: {url}')
        assert_that(status_of(url, timeout=60) == 200, f'bundle unreachable: {url}')
        return url.split('/npm/')[-1]
    check('Scalar bundle is version pinned and fetchable', scalar_bundle_is_pinned_and_reachable)

    def openapi_is_valid():
        spec = get_json(f'{APP}/openapi.json')
        paths = sorted(spec['paths'])
        assert_that('/random' in paths, f'/random missing from {paths}')
        # Observability endpoints must not clutter the public schema.
        assert_that('/metrics' not in paths, '/metrics leaked into the schema')
        assert_that('/healthz' not in paths, '/healthz leaked into the schema')
        assert_that('/scalar' not in paths, '/scalar leaked into the schema')
        return f'{len(paths)} documented paths, observability endpoints hidden'
    check('openapi.json is valid and clean', openapi_is_valid)

    section('prometheus (metrics)')

    def app_exposes_metrics():
        _, text = request(f'{APP}/metrics')
        for family in ('http_requests_total', 'http_request_duration_seconds_bucket'):
            assert_that(family in text, f'{family} missing')
        return 'http_requests_total + duration histogram present'
    check('app exposes Prometheus metrics', app_exposes_metrics)

    def all_targets_up():
        data = get_json(f'{PROM}/api/v1/targets?state=active')
        targets = data['data']['activeTargets']
        down = [(t['labels'].get('job'), t.get('lastError')) for t in targets if t['health'] != 'up']
        assert_that(not down, f'targets down: {down}')
        return ', '.join(sorted(t['labels']['job'] for t in targets)) + ' — all up'
    check('every Prometheus target is up', all_targets_up)

    def app_metrics_are_scraped():
        result = wait_until(
            lambda: prom_query('http_requests_total{job="fastapi"}') or None, timeout=90,
        )
        return f'{len(result)} app series in Prometheus'
    check('Prometheus has scraped the app', app_metrics_are_scraped)

    def span_metrics_arrive():
        result = wait_until(lambda: prom_query('traces_spanmetrics_calls_total') or None, timeout=120)
        return f'{len(result)} span-metric series remote-written by Tempo'
    check('Tempo span metrics reach Prometheus', span_metrics_arrive)

    def service_graph_arrives():
        result = wait_until(
            lambda: prom_query('traces_service_graph_request_total') or None, timeout=120,
        )
        return f'{len(result)} service-graph series'
    check('Tempo service graph reaches Prometheus', service_graph_arrives)

    section('grafana (wiring)')

    def datasources_provisioned():
        found = {d['uid']: d['type'] for d in get_json(
            f'{GRAFANA}/api/datasources', auth=GRAFANA_AUTH)}
        for uid, expected in (('loki', 'loki'), ('tempo', 'tempo'), ('prometheus', 'prometheus')):
            assert_that(found.get(uid) == expected, f'{uid} datasource missing, got {found}')
        return 'loki, tempo, prometheus'
    check('datasources are provisioned', datasources_provisioned)

    def derived_fields_configured():
        ds = get_json(f'{GRAFANA}/api/datasources/uid/loki', auth=GRAFANA_AUTH)
        fields = ds['jsonData'].get('derivedFields') or []
        assert_that(fields, 'no derived fields on the Loki datasource')
        to_tempo = [f for f in fields if f.get('datasourceUid') == 'tempo']
        assert_that(to_tempo, 'no derived field points at Tempo')
        # $$ in provisioning must survive as a single $ here.
        assert_that(
            to_tempo[0]['url'] == '${__value.raw}',
            f"url interpolation broken: {to_tempo[0]['url']}",
        )
        return f'{len(to_tempo)} derived fields -> Tempo, $ escaping intact'
    check('Loki derived fields link to Tempo', derived_fields_configured)

    def traces_to_logs_configured():
        ds = get_json(f'{GRAFANA}/api/datasources/uid/tempo', auth=GRAFANA_AUTH)
        config = ds['jsonData'].get('tracesToLogsV2') or {}
        assert_that(config.get('datasourceUid') == 'loki', f'tracesToLogsV2 not set: {config}')
        assert_that('${__span.traceId}' in config.get('query', ''), 'span variable not interpolated')
        return config['query']
    check('Tempo links back to Loki', traces_to_logs_configured)

    def dashboard_provisioned():
        results = get_json(f'{GRAFANA}/api/search?type=dash-db', auth=GRAFANA_AUTH)
        uids = [d['uid'] for d in results]
        assert_that('fastapi-loki-tempo' in uids, f'dashboard missing, found {uids}')
        dashboard = get_json(
            f'{GRAFANA}/api/dashboards/uid/fastapi-loki-tempo', auth=GRAFANA_AUTH)
        panels = dashboard['dashboard']['panels']
        return f'{len(panels)} panels'
    check('dashboard is provisioned', dashboard_provisioned)

    def grafana_can_query_prometheus():
        data = grafana_query('prometheus', 'prometheus', {
            'expr': 'sum(rate(http_requests_total{job="fastapi"}[5m]))', 'instant': True,
        })
        frames = data['results']['A'].get('frames') or []
        assert_that(not data['results']['A'].get('error'), data['results']['A'].get('error'))
        assert_that(frames, 'no frames returned')
        return f'{len(frames)} frame(s)'
    check('Grafana runs the Prometheus panel query', grafana_can_query_prometheus)

    def grafana_can_query_loki():
        data = grafana_query('loki', 'loki', {
            'expr': '{job="fastapi"}', 'queryType': 'range', 'maxLines': 10,
        })
        result = data['results']['A']
        assert_that(not result.get('error'), result.get('error'))
        assert_that(result.get('frames'), 'no frames returned')
        return f"{len(result['frames'])} frame(s)"
    check('Grafana runs the Loki panel query', grafana_can_query_loki)

    def grafana_can_reach_tempo():
        """Via the datasource proxy, not /api/ds/query.

        Grafana's Tempo plugin executes TraceQL search in the browser and rejects it
        on the backend ("backend TraceQL search queries are not supported"), so the
        proxy is the path the dashboard's trace panel really uses.
        """
        traces = get_json(
            f'{GRAFANA}/api/datasources/proxy/uid/tempo/api/search?'
            + urllib.parse.urlencode({'q': f'{{resource.service.name="{SERVICE_NAME}"}}', 'limit': 5}),
            auth=GRAFANA_AUTH,
        ).get('traces') or []
        assert_that(traces, 'TraceQL search through Grafana returned nothing')
        trace = get_json(
            f"{GRAFANA}/api/datasources/proxy/uid/tempo/api/traces/{traces[0]['traceID']}",
            auth=GRAFANA_AUTH,
        )
        spans = sum(
            len(ss.get('spans', []))
            for b in trace.get('batches', []) for ss in b.get('scopeSpans', [])
        )
        assert_that(spans, 'trace fetched through Grafana had no spans')
        return f'{len(traces)} traces found, {spans} spans fetched through Grafana'
    check('Grafana reaches Tempo (search + trace fetch)', grafana_can_reach_tempo)

    def datasource_health_is_ok():
        healthy = []
        for uid in ('loki', 'prometheus'):
            data = get_json(f'{GRAFANA}/api/datasources/uid/{uid}/health', auth=GRAFANA_AUTH)
            assert_that(data.get('status') == 'OK', f'{uid} unhealthy: {data}')
            healthy.append(uid)
        # The Tempo plugin does not implement the health endpoint; covered above.
        return ', '.join(healthy) + ' report OK'
    check('datasource health checks pass', datasource_health_is_ok)

    # --- summary -------------------------------------------------------------------
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f'\n{"=" * 72}')
    if failed:
        print(f'{RED}{failed} failed{RESET}, {passed} passed')
        for name, ok, detail in RESULTS:
            if not ok:
                print(f'  {RED}FAIL{RESET} {name}: {detail}')
        return 1
    print(f'{GREEN}all {passed} checks passed{RESET}')
    print(f'\nGrafana: {GRAFANA}/d/fastapi-loki-tempo')
    print(f'Scalar:  {APP}/scalar')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
