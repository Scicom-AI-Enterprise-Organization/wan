"""Ordinary application loggers must keep the JSON schema and the trace context.

`logging.getLogger(__name__)` at module scope is how essentially all application code
gets a logger, so this is the path that matters most in practice.
"""

import json
import logging
import subprocess
import sys

# Created at import time, before patch() has run -- the common real-world ordering.
module_logger = logging.getLogger('myapp.services.billing')


def test_named_logger_lines_carry_trace_context(client):
    test_client, capture = client

    @test_client.app.get('/named')
    async def named():
        module_logger.info('charging the card')
        return {'ok': True}

    test_client.get('/named')

    lines = [o for o in capture.of_type('log') if o['msg'] == 'charging the card']
    assert len(lines) == 1
    entry = lines[0]

    # Named after the module, not 'root': queryable as a Loki structured metadata field.
    assert entry['logger'] == 'myapp.services.billing'
    assert entry['type'] == 'log'
    assert entry['level'] == 'INFO'
    assert entry['traceID'] is not None
    assert entry['spanID'] is not None
    assert entry['correlation_id'] != '-'

    # Same request, so same ids as the request line.
    request_line = capture.of_type('request')[0]
    assert entry['traceID'] == request_line['traceID']
    assert entry['correlation_id'] == request_line['correlation_id']


def test_named_logger_reports_its_own_module_and_line(client):
    test_client, capture = client

    @test_client.app.get('/where')
    async def where():
        module_logger.info('from the test module')
        return {'ok': True}

    test_client.get('/where')
    entry = [o for o in capture.of_type('log') if o['msg'] == 'from the test module'][0]
    assert entry['module'] == 'test_named_loggers'
    assert isinstance(entry['line_no'], int) and entry['line_no'] > 0


def test_named_logger_exception_keeps_traceback_and_trace_id(client):
    test_client, capture = client

    @test_client.app.get('/handled')
    async def handled():
        try:
            raise ValueError('card declined')
        except ValueError:
            # Handled, so the request still succeeds -- but must stay correlated.
            module_logger.exception('charge failed')
        return {'ok': True}

    test_client.get('/handled')
    entry = [o for o in capture.of_type('log') if o['msg'] == 'charge failed'][0]
    assert entry['level'] == 'ERROR'
    assert 'ValueError: card declined' in entry['exc_info']
    assert entry['traceID'] is not None
    assert capture.of_type('request')[0]['response_status'] == 200


def test_child_logger_level_wins_over_root(client):
    """Propagation checks handler levels, not ancestor logger levels.

    So a module can opt into DEBUG on its own without turning up LOGLEVEL globally --
    surprising if you expect root's INFO to filter it out.
    """
    test_client, capture = client
    verbose = logging.getLogger('myapp.verbose')

    verbose.debug('should be filtered')
    assert not [o for o in capture.objects() if o.get('msg') == 'should be filtered']

    verbose.setLevel(logging.DEBUG)
    try:
        verbose.debug('should get through')
    finally:
        verbose.setLevel(logging.NOTSET)
    assert [o for o in capture.objects() if o.get('msg') == 'should get through']


def test_logger_created_after_patch_also_works(client):
    test_client, capture = client
    logging.getLogger('created.later').info('late logger')
    entry = [o for o in capture.objects() if o.get('msg') == 'late logger'][0]
    assert entry['logger'] == 'created.later'


def test_capture_warnings_hook_is_installed(client):
    """patch() turns on logging.captureWarnings.

    Asserted via logging's own saved-original slot rather than by raising a warning:
    pytest's warnings plugin wraps each test in catch_warnings(record=True), which
    replaces warnings.showwarning and would intercept it before logging sees it.
    """
    assert logging._warnings_showwarning is not None


def test_py_warnings_logger_emits_json(client):
    """The half we own: once routed to py.warnings, it formats like any other line."""
    test_client, capture = client
    logging.getLogger('py.warnings').warning('deprecated thing')
    entry = [o for o in capture.objects() if o.get('msg') == 'deprecated thing'][0]
    assert entry['logger'] == 'py.warnings'
    assert entry['level'] == 'WARNING'
    assert entry['type'] == 'log'


def test_warnings_reach_stdout_as_json_in_a_real_process(tmp_path):
    """End to end, in a subprocess, free of pytest's warning capture."""
    script = tmp_path / 'warn.py'
    script.write_text(
        'import warnings\n'
        'from fastapi import FastAPI\n'
        'import wan\n'
        'wan.patch(app=FastAPI(), service_name="warn-test")\n'
        'warnings.warn("subprocess deprecation", DeprecationWarning)\n'
    )
    result = subprocess.run(
        [sys.executable, '-W', 'always', str(script)],
        capture_output=True, text=True, timeout=120,
    )
    matching = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.strip().startswith('{') and 'subprocess deprecation' in line
    ]
    assert matching, f'warning not emitted as JSON. stdout={result.stdout[-500:]}'
    assert matching[0]['logger'] == 'py.warnings'
    assert matching[0]['service'] == 'warn-test'
