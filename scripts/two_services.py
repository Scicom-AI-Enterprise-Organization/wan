#!/usr/bin/env python3
"""Simulate app A calling app B and prove they share one trace and one correlation id.

Two separate services, two separate containers, two separate `service.name` values,
both exporting to the same Tempo and both shipping logs to the same Loki.

    docker compose -f grafana/docker-compose.yaml --profile two-services up -d --build
    python3 scripts/two_services.py

Asserts:
  * an id supplied by the caller is reused by A *and* forwarded to B
  * one trace id covers both services
  * Tempo holds a single trace containing both service.name values
  * Loki returns both services' logs from one correlation_id filter
  * Tempo's service graph gained a real service-a -> service-b edge
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit('/', 1)[0])

from e2e import (  # noqa: E402  (reuse the same helpers)
    GREEN, RED, DIM, RESET, PORTS, LOKI, PROM, TEMPO,
    assert_that, check, get_json, loki_query, prom_query, wait_until, RESULTS,
)

SERVICE_A = f"http://localhost:{PORTS.get('SERVICE_A_PORT', '7080')}"
CORRELATION_ID = f'sim-from-caller-{int(time.time())}'


def call_service_a():
    """Hit A with a correlation id, as an upstream caller (or a gateway) would."""
    url = f'{SERVICE_A}/chain?depth=1'
    request = urllib.request.Request(url, headers={'X-Correlation-ID': CORRELATION_ID})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode()), dict(response.headers)


def main():
    print(f'{DIM}service-a={SERVICE_A}  correlation id={CORRELATION_ID}{RESET}\n')

    try:
        body, headers = call_service_a()
    except urllib.error.URLError as e:
        print(f'{RED}cannot reach service-a at {SERVICE_A}: {e}{RESET}')
        print('start it with: docker compose -f grafana/docker-compose.yaml '
              '--profile two-services up -d --build')
        return 1

    print(f'{DIM}A -> B response: {json.dumps(body)}{RESET}\n')

    def a_called_b():
        assert_that(body.get('service') == 'service-a', f'unexpected top service: {body}')
        downstream = body.get('downstream') or {}
        assert_that(
            downstream.get('service') == 'service-b',
            f'service-a did not reach service-b: {downstream}',
        )
        return 'service-a -> service-b'
    check('A actually called B', a_called_b)

    def id_was_reused():
        returned = headers.get('X-Correlation-ID') or headers.get('x-correlation-id')
        assert_that(returned == CORRELATION_ID, f'A echoed {returned!r}')
        return f'A echoed back {returned}'
    check('A reused the caller-supplied correlation id', id_was_reused)

    # --- logs ------------------------------------------------------------------------
    def both_services_logged_the_id():
        entries = wait_until(
            lambda: loki_query(f'{{job="fastapi"}} |= "{CORRELATION_ID}"') or None,
            timeout=120,
        )
        services = set()
        trace_ids = set()
        for _, line in entries:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('correlation_id') != CORRELATION_ID:
                continue
            services.add(obj.get('service'))
            if obj.get('traceID'):
                trace_ids.add(obj['traceID'])
        assert_that(
            {'service-a', 'service-b'} <= services,
            f'only these services logged the id: {sorted(services)}',
        )
        assert_that(
            len(trace_ids) == 1,
            f'the two services ended up on {len(trace_ids)} different traces: {trace_ids}',
        )
        globals()['TRACE_ID'] = trace_ids.pop()
        return f'{sorted(services)} both logged it, on 1 trace'
    check('one correlation_id filter returns both services\' logs', both_services_logged_the_id)

    trace_id = globals().get('TRACE_ID')

    def logs_are_reachable_per_service():
        counts = {}
        for service in ('service-a', 'service-b'):
            entries = loki_query(
                f'{{service="{service}"}} |= "{CORRELATION_ID}"')
            counts[service] = len(entries)
            assert_that(entries, f'no {service} logs for this correlation id')
        return ', '.join(f'{k}={v} lines' for k, v in counts.items())
    check('each service is independently queryable in Loki', logs_are_reachable_per_service)

    # --- traces ----------------------------------------------------------------------
    def one_trace_spans_both_services():
        data = wait_until(lambda: get_json(f'{TEMPO}/api/traces/{trace_id}'), timeout=60)
        batches = data.get('batches') or []
        services = {
            attribute['value']['stringValue']
            for batch in batches
            for attribute in batch.get('resource', {}).get('attributes', [])
            if attribute['key'] == 'service.name'
        }
        spans = [
            span
            for batch in batches
            for scope in batch.get('scopeSpans', [])
            for span in scope.get('spans', [])
        ]
        assert_that(
            {'service-a', 'service-b'} <= services,
            f'trace only contains {sorted(services)}',
        )
        return f'trace {trace_id[:16]}.. has {len(spans)} spans across {sorted(services)}'
    check('Tempo holds one trace covering both services', one_trace_spans_both_services)

    def trace_id_matches_the_logs():
        # The whole point: the id in the log line is the id Tempo indexes.
        entries = loki_query(f'{{job="fastapi"}} |= "traceID={trace_id}"')
        assert_that(entries, f'no log line references traceID={trace_id}')
        return f'{len(entries)} log lines carry traceID={trace_id[:16]}..'
    check('the logged traceID is the one Tempo indexed', trace_id_matches_the_logs)

    # --- service graph ---------------------------------------------------------------
    def service_graph_has_the_edge():
        result = wait_until(
            lambda: prom_query(
                'traces_service_graph_request_total{client="service-a",server="service-b"}'
            ) or None,
            timeout=180,
        )
        return f'{len(result)} series for service-a -> service-b'
    check('Tempo service graph shows the A -> B edge', service_graph_has_the_edge)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f'\n{"=" * 72}')
    if failed:
        print(f'{RED}{failed} failed{RESET}, {passed} passed')
        return 1
    print(f'{GREEN}all {passed} checks passed{RESET}')
    print(f'\ncorrelation id : {CORRELATION_ID}')
    print(f'trace id       : {trace_id}')

    from links import explore_url, loki_pane, tempo_pane

    log_filter = f'{{job="fastapi"}} | correlation_id="{CORRELATION_ID}"'
    print('\nOpen in Grafana:\n')
    print(f'  Tempo, the A -> B trace\n    {explore_url({"tr": tempo_pane(trace_id)})}\n')
    print(f'  Loki, both services from one correlation id\n'
          f'    {explore_url({"lg": loki_pane(log_filter)})}\n')
    print(f'  Both, side by side\n'
          f'    {explore_url({"lg": loki_pane(log_filter), "tr": tempo_pane(trace_id)})}\n')
    print(f'  Tempo, service graph with the A -> B edge\n'
          f'    {explore_url({"sg": tempo_pane("", query_type="serviceMap")})}\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
