"""Many loggers, many simultaneous requests, no cross-talk.

This is the guarantee that would break first if the correlation id were held anywhere
other than a ContextVar: under load, lines would be stamped with another request's id.
"""

import asyncio
import logging

import httpx
import pytest

# Independent loggers: flat, and a hierarchy.
log1 = logging.getLogger('log1')
log2 = logging.getLogger('log2')
log3 = logging.getLogger('app.db.pool')
log4 = logging.getLogger('app.db')

LOGGER_NAMES = {'log1', 'log2', 'app.db.pool', 'app.db'}
CONCURRENT_REQUESTS = 12


@pytest.mark.asyncio
async def test_many_loggers_stay_correlated_under_concurrency(make_app):
    app, capture = make_app()

    @app.get('/work')
    async def work(tag: str):
        log1.info(f'{tag}|log1')
        # Yield control so requests genuinely interleave rather than running serially.
        await asyncio.sleep(0.01)
        log2.warning(f'{tag}|log2')
        await asyncio.sleep(0.01)
        log3.info(f'{tag}|log3')
        log4.error(f'{tag}|log4')
        return {'tag': tag}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        await asyncio.gather(*[
            client.get('/work', params={'tag': f'req{i}'},
                       headers={'X-Correlation-ID': f'cid-{i}'})
            for i in range(CONCURRENT_REQUESTS)
        ])

    app_lines = [o for o in capture.objects() if '|' in str(o.get('msg', ''))]
    assert len(app_lines) == CONCURRENT_REQUESTS * 4

    # Every logger is represented, and every line is JSON with full context.
    assert {o['logger'] for o in app_lines} == LOGGER_NAMES

    by_request = {}
    for entry in app_lines:
        tag, _, which = entry['msg'].partition('|')
        index = tag.removeprefix('req')

        # The line must carry the id of the request that produced it, not a neighbour's.
        assert entry['correlation_id'] == f'cid-{index}', (
            f"{entry['logger']} leaked: got {entry['correlation_id']}, want cid-{index}"
        )
        assert entry['traceID'] is not None
        assert entry['spanID'] is not None
        by_request.setdefault(tag, []).append(entry)

    assert len(by_request) == CONCURRENT_REQUESTS
    for tag, entries in by_request.items():
        assert len(entries) == 4
        # One trace per request, shared by all four loggers.
        assert len({e['traceID'] for e in entries}) == 1, f'{tag} split across traces'

    # And the requests are separate traces, not one merged trace.
    traces = {e['traceID'] for e in app_lines}
    assert len(traces) == CONCURRENT_REQUESTS


@pytest.mark.asyncio
async def test_logger_hierarchy_does_not_duplicate_lines(make_app):
    """'app.db.pool' propagates through 'app.db' and 'app' to root -- but only root
    has a handler, so each record is emitted exactly once."""
    app, capture = make_app()

    @app.get('/once')
    async def once():
        log3.info('emitted-once')
        return {'ok': True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        await client.get('/once')

    assert len([o for o in capture.objects() if o.get('msg') == 'emitted-once']) == 1


@pytest.mark.asyncio
async def test_background_task_keeps_the_request_context(make_app):
    """A task spawned from a handler inherits the ContextVar copy, so it stays correlated."""
    app, capture = make_app()

    @app.get('/spawn')
    async def spawn():
        async def later():
            await asyncio.sleep(0.01)
            log1.info('from-background-task')
        # asyncio.create_task copies the current context, correlation id included.
        await asyncio.create_task(later())
        return {'ok': True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        await client.get('/spawn', headers={'X-Correlation-ID': 'bg-task-id'})

    entry = [o for o in capture.objects() if o.get('msg') == 'from-background-task'][0]
    assert entry['correlation_id'] == 'bg-task-id'
    assert entry['traceID'] is not None
