"""Single line JSON logging with Loki/Tempo correlation fields.

The emitted schema is the one `json_logging` produced, so existing Loki queries,
dashboards and alerts keep working -- but it is implemented here directly.
`json_logging` resolved the correlation id by walking 11 stack frames on *every*
log call looking for a local named ``request``, which is both slow and silently
wrong when the frame depth changes; this reads a context variable instead.
"""

import json
import logging
import logging.handlers
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi_loki_tempo.context import get_correlation_id, trace_context

TYPE_LOG = 'log'
TYPE_REQUEST = 'request'

REQUEST_LOGGER_NAME = 'fastapi-request-logger'

#: Attribute the request middleware hangs its pre-built log object off of.
REQUEST_LOG_ATTR = '_fastapi_loki_tempo_request'

#: Everything the stdlib puts on a LogRecord. Attributes outside this set came
#: from ``logger.info(..., extra={...})`` and are merged into the log object.
_RESERVED_RECORD_ATTRS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName',
    'levelname', 'levelno', 'lineno', 'message', 'module', 'msecs', 'msg', 'name',
    'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
    'taskName', 'thread', 'threadName',
    REQUEST_LOG_ATTR,
})

#: Extras that some libraries attach and that are pure noise in a log store.
#: uvicorn puts an ANSI-escaped copy of every message in `color_message`.
_DROPPED_RECORD_ATTRS = frozenset({'color_message'})

_LOGGERS_TO_REROUTE = (
    'uvicorn',
    'uvicorn.error',
    'uvicorn.asgi',
    'gunicorn',
    'gunicorn.error',
    'fastapi',
)

_ACCESS_LOGGERS = ('uvicorn.access', 'gunicorn.access')


def iso_time(epoch_seconds: float) -> str:
    """``2023-10-01T15:16:27.952Z`` -- millisecond UTC, always ``Z`` suffixed."""
    dt = datetime.fromtimestamp(epoch_seconds, timezone.utc)
    return '%04d-%02d-%02dT%02d:%02d:%02d.%03dZ' % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond // 1000,
    )


def epoch_nano(epoch_seconds: float) -> int:
    """Nanoseconds since epoch, derived without float rounding at ns scale."""
    return int(round(epoch_seconds * 1_000_000)) * 1_000


class JsonLogFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON, with trace ids attached.

    Both ``type=log`` and ``type=request`` lines go through this one formatter, so
    a single handler covers the whole application.
    """

    def __init__(
        self,
        service_name: str = 'fastapi',
        service_version: Optional[str] = None,
        environment: Optional[str] = None,
        max_msg_length: int = 0,
        static_fields: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.max_msg_length = max_msg_length or 0
        self.static_fields: Dict[str, Any] = {'service': service_name}
        if service_version:
            self.static_fields['service_version'] = service_version
        if environment:
            self.static_fields['environment'] = environment
        if static_fields:
            self.static_fields.update(static_fields)

    # -- public ------------------------------------------------------------------------
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.log_object(record), default=repr, ensure_ascii=False)

    def log_object(self, record: logging.LogRecord) -> Dict[str, Any]:
        request_log = getattr(record, REQUEST_LOG_ATTR, None)
        if request_log is not None:
            obj: Dict[str, Any] = {
                'written_at': iso_time(record.created),
                'written_ts': epoch_nano(record.created),
            }
            obj.update(request_log)
        else:
            obj = {
                'written_at': iso_time(record.created),
                'written_ts': epoch_nano(record.created),
                'msg': self.render_msg(record),
                'type': TYPE_LOG,
                'logger': record.name,
                'thread': record.threadName,
                'level': record.levelname,
                'module': record.module,
                'line_no': record.lineno,
                'correlation_id': get_correlation_id(),
            }

        obj.update(trace_context())
        obj.update(self.static_fields)

        if request_log is not None:
            # Appended rather than inlined above so the `type=request` field order
            # stays byte-for-byte what json_logging emitted, while still giving
            # request lines a `level` for Loki labels and Grafana filtering.
            obj['level'] = record.levelname

        if record.exc_info or record.exc_text:
            obj['exc_info'] = self.render_exception(record)
            obj['filename'] = record.filename
        if record.stack_info:
            obj['stack_info'] = record.stack_info

        obj.update(self.extra_fields(record))
        return obj

    # -- internals ---------------------------------------------------------------------
    def render_msg(self, record: logging.LogRecord) -> str:
        """Message as a single line string.

        A dict message is serialised as JSON rather than left as Python ``repr``,
        so ``logger.info({'message': 'hi'})`` produces ``{"message": "hi"}``
        instead of the unparseable ``{'message': 'hi'}``.
        """
        if isinstance(record.msg, (dict, list, tuple)) and not record.args:
            msg = json.dumps(record.msg, default=repr, ensure_ascii=False)
        else:
            msg = record.getMessage()

        # One JSON object per line is the contract Loki's `json` stage relies on.
        msg = msg.replace('\n', '_').replace('\r', '_').replace('\t', '_')
        if self.max_msg_length and len(msg) > self.max_msg_length:
            msg = msg[:self.max_msg_length] + '...[truncated]'
        return msg

    @staticmethod
    def render_exception(record: logging.LogRecord) -> str:
        if record.exc_info:
            return ''.join(traceback.format_exception(*record.exc_info)).rstrip()
        return record.exc_text or ''

    @staticmethod
    def extra_fields(record: logging.LogRecord) -> Dict[str, Any]:
        fields = {}
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key in _DROPPED_RECORD_ATTRS:
                continue
            if key.startswith('_'):
                continue
            fields[key] = value
        # Legacy `json_logging` style: extra={'props': {...}} lands at the root.
        props = fields.pop('props', None)
        if isinstance(props, dict):
            fields.update(props)
        return fields


def build_handlers(
    formatter: logging.Formatter,
    stdout: bool = True,
    log_file: Optional[str] = None,
    log_file_max_bytes: int = 64 * 1024 * 1024,
    log_file_backup_count: int = 3,
) -> List[logging.Handler]:
    handlers: List[logging.Handler] = []
    if stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        directory = os.path.dirname(os.path.abspath(log_file))
        if directory:
            os.makedirs(directory, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=log_file_max_bytes,
                backupCount=log_file_backup_count,
                encoding='utf-8',
            )
        )
    if not handlers:
        # A logger with no handlers falls back to logging.lastResort, which prints
        # unformatted text to stderr -- worse than being explicit about it.
        handlers.append(logging.NullHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
    return handlers


def setup_logging(
    level: str = 'INFO',
    service_name: str = 'fastapi',
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
    stdout: bool = True,
    log_file: Optional[str] = None,
    log_file_max_bytes: int = 64 * 1024 * 1024,
    log_file_backup_count: int = 3,
    max_msg_length: int = 0,
    static_fields: Optional[Dict[str, Any]] = None,
    silence_access_logs: bool = True,
    reroute_loggers: Iterable[str] = _LOGGERS_TO_REROUTE,
) -> JsonLogFormatter:
    """Point the root logger at a JSON handler and make uvicorn use it too.

    uvicorn installs its own handlers on ``uvicorn.error`` / ``uvicorn.access``.
    Left alone, startup and error lines stay plain text and Loki cannot parse
    them, so their handlers are removed and propagation to root is forced --
    every line the process emits is then valid JSON.
    """
    formatter = JsonLogFormatter(
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        max_msg_length=max_msg_length or 0,
        static_fields=static_fields,
    )
    handlers = build_handlers(
        formatter,
        stdout=stdout,
        log_file=log_file,
        log_file_max_bytes=log_file_max_bytes,
        log_file_backup_count=log_file_backup_count,
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)

    for name in reroute_loggers:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    for name in _ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        # We emit a richer `type=request` line ourselves; keeping uvicorn's access
        # log too would double count every request in Loki.
        logger.propagate = not silence_access_logs
        logger.disabled = silence_access_logs

    return formatter
