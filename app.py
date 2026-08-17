"""Example service.

Run it, hit the endpoints, then correlate the logs and traces in Grafana:

    docker compose -f grafana/docker-compose.yaml up -d --build
    curl 'http://localhost:7072/random?minimum=0.1&maximum=2'
"""

import asyncio
import logging
import os
import random

from fastapi import FastAPI, HTTPException, Request
from opentelemetry import trace

import fastapi_loki_tempo

app = FastAPI(
    title='fastapi-loki-tempo',
    description='FastAPI boilerplate for Loki and Tempo.',
    version=fastapi_loki_tempo.__version__,
)
fastapi_loki_tempo.patch(app=app)

tracer = trace.get_tracer(__name__)

#: Used by /chain to call itself, which is enough to show trace propagation
#: across a service boundary without running two services.
SELF_URL = os.environ.get('SELF_URL', 'http://localhost:7072')


@app.get('/')
async def index(request: Request = None):
    return {'message': 'hello'}


@app.get('/random')
async def random_sleep(
    minimum: float = 0.1,
    maximum: float = 2.0,
    request: Request = None,
):
    if minimum > maximum:
        raise HTTPException(status_code=422, detail='minimum must be <= maximum')

    how_long = random.uniform(minimum, maximum)
    logging.info(f'I sleep for {how_long} seconds')
    await asyncio.sleep(how_long)
    return {'message': f'sleep for {how_long} seconds'}


@app.get('/nested')
async def nested(request: Request = None):
    """Manual child spans, each with its own log line carrying the same trace id."""
    with tracer.start_as_current_span('load-user') as span:
        span.set_attribute('user.id', 42)
        await asyncio.sleep(0.05)
        logging.info('loaded user 42')

    with tracer.start_as_current_span('render') as span:
        span.add_event('cache miss')
        await asyncio.sleep(0.02)
        logging.info('rendered response')

    return {'message': 'done'}


@app.get('/chain')
async def chain(depth: int = 1, request: Request = None):
    """Call itself `depth` more times: one trace, several spans, several log lines."""
    logging.info(f'chain depth={depth}')
    if depth <= 0:
        return {'message': 'leaf'}

    import httpx

    # `traceparent` is injected automatically by the httpx instrumentation, which is
    # what keeps the trace joined. The correlation id is not a W3C standard header,
    # so forward it explicitly to keep one id across all hops too.
    async with httpx.AsyncClient(
        timeout=10.0,
        headers=fastapi_loki_tempo.correlation_headers(),
    ) as client:
        response = await client.get(f'{SELF_URL}/chain', params={'depth': depth - 1})
        response.raise_for_status()
        return {'depth': depth, 'downstream': response.json()}


@app.get('/boom')
async def boom(request: Request = None):
    """Unhandled error: the span is marked failed and the traceback is logged with the trace id."""
    logging.warning('about to divide by zero')
    return {'result': 1 / 0}


@app.get('/not-found')
async def not_found(request: Request = None):
    raise HTTPException(status_code=404, detail='nothing here')
