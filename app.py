"""Example service.

Run it, hit the endpoints, then correlate the logs and traces in Grafana:

    docker compose -f grafana/docker-compose.yaml up -d --build
    curl 'http://localhost:7072/random?minimum=0.1&maximum=2'

Running it outside compose reads a `.env` next to this file, so a remote OTLP
push (OTLP_ENDPOINT, OTLP_USERNAME, OTLP_PASSWORD) needs no exported variables:

    uvicorn app:app --port 7072
"""

import asyncio
import logging
import os
import random

# Before `import wan`: every wan.patch() default is read from the environment when
# wan is imported, so a .env loaded after that would be ignored. python-dotenv is
# in the `example` extra only -- the library itself never reads a file.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from opentelemetry import trace  # noqa: E402

import wan  # noqa: E402

app = FastAPI(
    title='wan',
    description='FastAPI boilerplate for Loki and Tempo.',
    version=wan.__version__,
)
wan.patch(app=app)

tracer = trace.get_tracer(__name__)

SERVICE_NAME = os.environ.get('SERVICE_NAME', 'fastapi')

#: Where /chain sends its next hop. Point it at another instance to get a real
#: two-service trace; left at itself it still demonstrates propagation.
DOWNSTREAM_URL = os.environ.get(
    'DOWNSTREAM_URL', os.environ.get('SELF_URL', 'http://localhost:7072'),
)


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
    """Call `DOWNSTREAM_URL` `depth` more times: one trace and one correlation id."""
    logging.info(f'chain depth={depth} on {SERVICE_NAME}')
    if depth <= 0:
        return {'service': SERVICE_NAME, 'message': 'leaf'}

    import httpx

    # No correlation headers passed by hand: the httpx instrumentation injects both
    # `traceparent` and `X-Correlation-ID` from the global propagator, so the trace
    # and the correlation id both survive the hop.
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f'{DOWNSTREAM_URL}/chain', params={'depth': depth - 1})
        response.raise_for_status()
        return {
            'service': SERVICE_NAME,
            'depth': depth,
            'downstream': response.json(),
        }


@app.get('/boom')
async def boom(request: Request = None):
    """Unhandled error: the span is marked failed and the traceback is logged with the trace id."""
    logging.warning('about to divide by zero')
    return {'result': 1 / 0}


@app.get('/sentry-debug')
async def trigger_error(request: Request = None):
    """Sentry's onboarding check. Also proves the event carries the trace id."""
    logging.info('about to divide by zero for sentry')
    division_by_zero = 1 / 0
    return {'unreachable': division_by_zero}


@app.get('/not-found')
async def not_found(request: Request = None):
    raise HTTPException(status_code=404, detail='nothing here')
