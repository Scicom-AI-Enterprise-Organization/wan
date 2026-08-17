import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import fastapi_loki_tempo


class LogCapture:
    """Collects the JSON objects the library actually writes to a handler."""

    def __init__(self, stream: io.StringIO):
        self.stream = stream

    def objects(self):
        self.stream.flush()
        lines = [line for line in self.stream.getvalue().splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def of_type(self, log_type: str):
        return [obj for obj in self.objects() if obj.get('type') == log_type]

    def clear(self):
        self.stream.seek(0)
        self.stream.truncate(0)


@pytest.fixture
def make_app():
    """Build a patched FastAPI app plus a capture of everything it logs."""
    created = []

    def factory(**patch_kwargs):
        app = FastAPI(title='test-app', version='9.9.9')
        kwargs = {
            'service_name': 'test-service',
            'service_version': '9.9.9',
            'environment': 'test',
            # No exporter: spans are still created and trace ids still logged.
            'otlp_endpoint': None,
            'log_stdout': False,
        }
        kwargs.update(patch_kwargs)
        state = fastapi_loki_tempo.patch(app=app, **kwargs)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(state['formatter'])
        root = logging.getLogger()
        root.addHandler(handler)
        created.append((root, handler))

        @app.get('/ping')
        async def ping():
            logging.info('pinged')
            return {'pong': True}

        @app.get('/kaboom')
        async def kaboom():
            raise RuntimeError('nope')

        return app, LogCapture(stream)

    yield factory

    for root, handler in created:
        root.removeHandler(handler)


@pytest.fixture
def client(make_app):
    app, capture = make_app()
    # raise_server_exceptions=False so the 500 path can be asserted like a real server.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, capture
