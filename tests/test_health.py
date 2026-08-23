"""/readyz with readiness checks: 503 on failure, per-check detail either way."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import wan


def make_client(**patch_kwargs):
    app = FastAPI()
    wan.patch(app=app, otlp_endpoint=None, log_stdout=False, **patch_kwargs)
    return TestClient(app, raise_server_exceptions=False)


def test_readyz_without_checks_stays_a_plain_200():
    with make_client() as client:
        response = client.get('/readyz')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_passing_checks_are_reported_by_name():
    def database():
        return True

    async def queue():
        return None  # returning nothing is a pass; only False and raising fail

    with make_client(readiness_checks=[database, queue]) as client:
        body = client.get('/readyz').json()
    assert body['status'] == 'ok'
    assert body['checks'] == {'database': 'ok', 'queue': 'ok'}


def test_a_raising_check_turns_readyz_into_503_naming_the_check():
    def database():
        raise ConnectionError('connection refused')

    with make_client(readiness_checks=[database]) as client:
        response = client.get('/readyz')
    assert response.status_code == 503
    body = response.json()
    assert body['status'] == 'unready'
    assert body['checks']['database'] == 'ConnectionError: connection refused'


def test_a_check_returning_false_fails_without_an_exception():
    def flag():
        return False

    with make_client(readiness_checks=[flag]) as client:
        response = client.get('/readyz')
    assert response.status_code == 503
    assert response.json()['checks']['flag'] == 'failed'


def test_one_failure_does_not_hide_the_other_results():
    """The 503 must say which dependency is down, not just that one is."""
    def healthy():
        return True

    def broken():
        raise TimeoutError('no route')

    with make_client(readiness_checks=[healthy, broken]) as client:
        body = client.get('/readyz').json()
    assert body['checks'] == {'healthy': 'ok', 'broken': 'TimeoutError: no route'}


def test_async_checks_are_awaited():
    async def down():
        raise RuntimeError('async boom')

    with make_client(readiness_checks=[down]) as client:
        response = client.get('/readyz')
    assert response.status_code == 503
    assert 'async boom' in response.json()['checks']['down']


def test_healthz_and_livez_stay_unconditional():
    """Liveness must not depend on downstreams: a dead database is not a reason for
    Kubernetes to restart the pod."""
    def database():
        raise ConnectionError('down')

    with make_client(readiness_checks=[database]) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/livez').status_code == 200
        assert client.get('/readyz').status_code == 503
