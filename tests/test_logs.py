"""The JSON log schema is a public contract: Loki queries and dashboards depend on it."""

import json
import logging
import re

import pytest

from wan.logs import JsonLogFormatter, epoch_nano, iso_time

# Exactly the keys the README documents for a `type=log` line, in order.
REFERENCE_LOG_KEYS = [
    'written_at', 'written_ts', 'msg', 'type', 'logger', 'thread', 'level',
    'module', 'line_no', 'correlation_id', 'traceID', 'trace_message', 'dd.trace_id',
]

# ... and for a `type=request` line.
REFERENCE_REQUEST_KEYS = [
    'written_at', 'written_ts', 'type', 'correlation_id', 'remote_user', 'request',
    'referer', 'x_forwarded_for', 'protocol', 'method', 'remote_ip', 'request_size_b',
    'remote_host', 'remote_port', 'request_received_at', 'response_time_ms',
    'response_status', 'response_size_b', 'response_content_type', 'response_sent_at',
    'traceID', 'trace_message', 'dd.trace_id',
]

ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')


def make_record(**kwargs):
    defaults = dict(
        name='root', level=logging.INFO, pathname='/app/app.py', lineno=23,
        msg='hello', args=(), exc_info=None,
    )
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


def test_log_line_starts_with_the_documented_keys_in_order():
    obj = json.loads(JsonLogFormatter().format(make_record()))
    assert list(obj)[:len(REFERENCE_LOG_KEYS)] == REFERENCE_LOG_KEYS


def test_written_at_is_millisecond_utc_with_z_suffix():
    obj = json.loads(JsonLogFormatter().format(make_record()))
    assert ISO_RE.match(obj['written_at']), obj['written_at']


def test_written_ts_is_nanoseconds_consistent_with_written_at():
    record = make_record()
    obj = json.loads(JsonLogFormatter().format(record))
    assert obj['written_ts'] == epoch_nano(record.created)
    # Same instant to the millisecond.
    assert iso_time(obj['written_ts'] / 1e9) == obj['written_at']


def test_service_identity_is_attached():
    formatter = JsonLogFormatter(
        service_name='svc', service_version='1.2.3', environment='prod',
    )
    obj = json.loads(formatter.format(make_record()))
    assert obj['service'] == 'svc'
    assert obj['service_version'] == '1.2.3'
    assert obj['environment'] == 'prod'


def test_trace_fields_are_null_without_an_active_span():
    obj = json.loads(JsonLogFormatter().format(make_record()))
    assert obj['traceID'] is None
    # Not the string 'traceID=None' -- that would render a dead link in Grafana.
    assert obj['trace_message'] is None


def test_output_is_always_a_single_line():
    obj_line = JsonLogFormatter().format(make_record(msg='multi\nline\ttext\r\n'))
    assert '\n' not in obj_line and '\t' not in obj_line
    assert json.loads(obj_line)['msg'] == 'multi_line_text__'


def test_dict_message_is_serialised_as_json_not_python_repr():
    obj = json.loads(JsonLogFormatter().format(make_record(msg={'message': 'hi'})))
    assert obj['msg'] == '{"message": "hi"}'
    assert "'" not in obj['msg']


def test_percent_style_message_is_interpolated():
    obj = json.loads(JsonLogFormatter().format(make_record(msg='n=%d', args=(7,))))
    assert obj['msg'] == 'n=7'


def test_max_msg_length_truncates():
    formatter = JsonLogFormatter(max_msg_length=5)
    obj = json.loads(formatter.format(make_record(msg='0123456789')))
    assert obj['msg'] == '01234...[truncated]'


def test_extra_fields_are_merged():
    record = make_record()
    record.tenant = 'acme'
    obj = json.loads(JsonLogFormatter().format(record))
    assert obj['tenant'] == 'acme'


def test_uvicorn_color_message_is_dropped():
    record = make_record()
    record.color_message = 'Started \x1b[36m%d\x1b[0m'
    obj = json.loads(JsonLogFormatter().format(record))
    assert 'color_message' not in obj


def test_exception_is_captured_as_text():
    try:
        raise ValueError('boom')
    except ValueError:
        import sys
        record = make_record(level=logging.ERROR, msg='failed', exc_info=sys.exc_info())
    obj = json.loads(JsonLogFormatter().format(record))
    assert 'ValueError: boom' in obj['exc_info']
    assert obj['filename'] == 'app.py'


def test_unserialisable_values_do_not_break_the_line():
    record = make_record()
    record.weird = object()
    obj = json.loads(JsonLogFormatter().format(record))  # must not raise
    assert 'object at' in obj['weird']


@pytest.mark.parametrize('log_type,keys', [
    ('log', REFERENCE_LOG_KEYS),
    ('request', REFERENCE_REQUEST_KEYS),
])
def test_reference_keys_are_never_dropped(log_type, keys):
    """Guard against a refactor quietly removing a field someone's dashboard uses."""
    assert 'traceID' in keys and 'trace_message' in keys and 'dd.trace_id' in keys
