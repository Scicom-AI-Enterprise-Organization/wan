"""Third-party library loggers, and where the context does and does not follow.

Correlation is resolved per record from a ContextVar, not per logger, so any library
logging inside a request gets it for free. The exception is thread boundaries:
ContextVars are copied into a new asyncio task, but not into a raw thread.
"""

import asyncio
import concurrent.futures
import contextvars
import logging
import threading

import httpx
import pytest

CID = 'third-party-cid'


async def call(app, path, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.get(path, headers={'X-Correlation-ID': CID}, **kwargs)


@pytest.mark.asyncio
async def test_third_party_loggers_inside_a_request_are_correlated(make_app):
    """httpx/httpcore/urllib3 name their own loggers; they still get the ids."""
    app, capture = make_app(loglevel='DEBUG')

    @app.get('/outbound')
    async def outbound():
        # Logs to 'httpx' and 'httpcore.*' from inside the request context.
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.get('http://127.0.0.1:1/nope')
            except Exception:
                pass
        return {'ok': True}

    await call(app, '/outbound')

    # Only httpcore: ASGITransport short-circuits the network, so every httpcore line
    # comes from the outbound call inside the request. The 'httpx' logger is ambiguous
    # here because this test drives the app with httpx from *outside* any request, and
    # that line correctly has no correlation id.
    inner = [o for o in capture.objects() if str(o.get('logger', '')).startswith('httpcore')]
    assert inner, 'no httpcore log lines captured'
    for entry in inner:
        assert entry['correlation_id'] == CID, f"{entry['logger']} lost the id"
        assert entry['traceID'] is not None

    # The 'httpx' logger is deliberately not asserted on: it only logs a line for a
    # *completed* response, and the outbound call above fails on purpose, so the only
    # httpx lines here come from the client driving this test from outside the request.
    # httpcore above is the real proof -- it is a separate library, with its own
    # getLogger(__name__), logging from inside the request.


@pytest.mark.asyncio
async def test_asyncio_to_thread_keeps_the_context(make_app):
    app, capture = make_app()

    @app.get('/to-thread')
    async def to_thread():
        # asyncio.to_thread copies the current context into the worker.
        await asyncio.to_thread(logging.getLogger('probe.to_thread').info, 'to-thread')
        return {'ok': True}

    await call(app, '/to-thread')
    entry = [o for o in capture.objects() if o.get('msg') == 'to-thread'][0]
    assert entry['correlation_id'] == CID


@pytest.mark.asyncio
async def test_raw_thread_loses_the_context(make_app):
    """Characterising a real limitation, so it stays visible rather than surprising.

    threading.Thread does not copy ContextVars, so a library that logs from its own
    worker thread reports no correlation id.
    """
    app, capture = make_app()

    @app.get('/raw-thread')
    async def raw_thread():
        thread = threading.Thread(
            target=logging.getLogger('probe.raw').info, args=('raw-thread',))
        thread.start()
        thread.join()
        return {'ok': True}

    await call(app, '/raw-thread')
    entry = [o for o in capture.objects() if o.get('msg') == 'raw-thread'][0]
    assert entry['correlation_id'] == '-'
    assert entry['traceID'] is None


@pytest.mark.asyncio
async def test_copy_context_restores_it_across_a_thread(make_app):
    """The documented fix: copy the context at submit time and run inside it."""
    app, capture = make_app()

    @app.get('/fixed-thread')
    async def fixed_thread():
        def work():
            logging.getLogger('probe.fixed').info('fixed-thread')

        # copy_context() must be called here, in the request, not in the worker.
        ctx = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            await loop.run_in_executor(pool, ctx.run, work)
        return {'ok': True}

    await call(app, '/fixed-thread')
    entry = [o for o in capture.objects() if o.get('msg') == 'fixed-thread'][0]
    assert entry['correlation_id'] == CID
    assert entry['traceID'] is not None
