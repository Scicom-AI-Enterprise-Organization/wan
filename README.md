# wan

**FastAPI observability boilerplate — correlated logs, traces and metrics in one call.**

Loki and Tempo are a very useful open source stack for combining logging with tracing,
but wiring them together correctly is fiddly and easy to get subtly wrong. `wan` does it
for you: every log line carries the trace id of the request that produced it, and one
correlation id follows a request across every service it touches.

```python
import wan
from fastapi import FastAPI

app = FastAPI()
wan.patch(app=app)
```

That single call gives the service JSON logs, OpenTelemetry traces exported to Tempo,
Prometheus metrics, health probes and a Scalar API reference. Everything is configurable
by environment variable, so the same image runs unchanged from laptop to production.

<img src="docs/grafana-dashboard.png" width="900px">

## What is this?

1. Every log from the `logging` module becomes one line of JSON containing the active
   trace id, so a log line can be traced back to the request that produced it:

```json
{"written_at": "2026-08-22T03:46:51.437Z", "written_ts": 1787370411437106000, "msg": "I sleep for 0.2068091112098681 seconds", "type": "log", "logger": "root", "thread": "MainThread", "level": "INFO", "module": "app", "line_no": 67, "correlation_id": "ebfcb160-2ea9-400e-b794-93729cbd6a3e", "traceID": "6aea6a078cee9a0962e1e9767fdfd85e", "trace_message": "traceID=6aea6a078cee9a0962e1e9767fdfd85e", "dd.trace_id": "7125232780637624414", "spanID": "81df20514186ce3f", "service": "fastapi"}
```

2. Every HTTP request produces a `type=request` line with the same trace id:

```json
{"written_at": "2026-08-22T03:46:51.646Z", "written_ts": 1787370411646326000, "type": "request", "correlation_id": "ebfcb160-2ea9-400e-b794-93729cbd6a3e", "remote_user": "-", "request": "/random", "referer": "http://localhost:7072/docs", "x_forwarded_for": "-", "protocol": "HTTP/1.1", "method": "GET", "remote_ip": "127.0.0.1", "request_size_b": -1, "remote_host": "127.0.0.1", "remote_port": 58468, "request_received_at": "2026-08-22T03:46:51.436Z", "response_time_ms": 209, "response_status": 200, "response_size_b": "50", "response_content_type": "application/json", "response_sent_at": "2026-08-22T03:46:51.646Z", "traceID": "6aea6a078cee9a0962e1e9767fdfd85e", "trace_message": "traceID=6aea6a078cee9a0962e1e9767fdfd85e", "dd.trace_id": "7125232780637624414", "spanID": "81df20514186ce3f", "service": "fastapi", "level": "INFO", "request_route": "/random"}
```

3. `trace_message` carries the `traceID=` form Loki's derived fields match on, so
   Grafana can link Loki straight to Tempo — and Tempo straight back to Loki.

In both shapes the keys up to and including `dd.trace_id` are the exact schema, in the
exact order, that `json_logging` produced, so existing Loki queries, dashboards and
alerts keep working. Everything after it is appended, never inserted: `spanID`,
`service`, `service_version`, `environment`, `level` (on request lines too) and
`request_route`.

## Installation

Installed straight from git; this is not published to PyPI.

```bash
pip3 install git+https://github.com/Scicom-AI-Enterprise-Organization/wan
```

Pin a tag or commit for anything you deploy — a bare git URL tracks the default branch,
so a colleague's merge changes what your next build installs:

```bash
pip3 install 'wan @ git+https://github.com/Scicom-AI-Enterprise-Organization/wan@v0.1.0'
```

In a `requirements.txt`:

```
wan @ git+https://github.com/Scicom-AI-Enterprise-Organization/wan@v0.1.0
```

Optional extras use PEP 508 direct-reference syntax, since there is no package index to
resolve `wan[extra]` against:

```bash
pip3 install 'wan[httpx] @ git+https://github.com/Scicom-AI-Enterprise-Organization/wan'
```

| Extra | Adds |
|---|---|
| `httpx` | tracing for outbound `httpx` calls, and correlation id propagation with them |
| `requests` | the same for `requests` |
| `jaeger` | the deprecated Jaeger thrift exporter |
| `dev` | pytest and the test dependencies |

## How to

Simple as,

```python
import wan
from fastapi import Request, FastAPI

app = FastAPI()
wan.patch(app=app)
```

`patch` then gives the app:

| | |
|---|---|
| JSON logs | trace id, span id and correlation id on every line |
| Request logs | one `type=request` line per request, with timing and status |
| Tracing | OpenTelemetry spans exported to Tempo, or any OTLP collector, over gRPC or HTTP |
| Metrics | Prometheus at `/metrics` |
| Health | `/healthz`, `/livez`, `/readyz`, excluded from traces and request logs |
| Errors | Sentry, tagged with the same trace and correlation ids |
| API docs | Scalar at `/scalar` |
| Headers | `X-Correlation-ID` and `X-Trace-Id` on every response |

Every argument defaults to an environment variable, so the same image runs
unchanged in local, staging and production.

```python
def patch(
    app,
    service_name: str = SERVICE_NAME,
    otlp_endpoint: Optional[str] = OTLP_ENDPOINT,
    jaeger_host: Optional[str] = JAEGER_HOST,
    jaeger_port: Optional[int] = JAEGER_PORT,
    tracing_sample: float = TRACING_SAMPLE,
    enable_prometheus_metrics: bool = ENABLE_PROMETHEUS_METRICS,
    enable_scalar_doc: bool = ENABLE_SCALAR_DOC,
    scalar_doc_endpoint: str = SCALAR_DOC_ENDPOINT,
    # ... see wan/os_env.py for the full list
)
```

Run FastAPI,

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 7072
```

### Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `SERVICE_NAME` | `fastapi` | `service.name` on spans, `service` on logs |
| `SERVICE_VERSION` | – | `service.version` on spans |
| `DEPLOYMENT_ENVIRONMENT` | – | `deployment.environment` on spans |
| `OTLP_ENDPOINT` | – | Where spans are pushed; `OTLP_URL` is accepted as an alias. Unset: trace ids are still logged, nothing is exported |
| `OTLP_PROTOCOL` | auto | `grpc` (port 4317) or `http` (port 4318, or an https push URL). Defaults to `http` when the endpoint ends in `/v1/traces`, else `grpc` |
| `OTLP_USERNAME` | – | Basic auth username for the collector |
| `OTLP_PASSWORD` | – | Basic auth password for the collector |
| `OTLP_HEADERS` | – | `key=value,key=value`, e.g. a bearer token. Overrides the basic auth header |
| `OTLP_INSECURE` | auto | Skip TLS for the gRPC exporter. `false` once the endpoint is `https://` |
| `GRAFANA_DATASOURCE_URL` | – | A link copied from Grafana Explore; the base URL and trace datasource are read off it |
| `GRAFANA_TRACE_DATASOURCE_UID` | `tempo` | Datasource the trace links open |
| `GRAFANA_TRACE_DATASOURCE_TYPE` | `tempo` | `tempo`, or `jaeger` for VictoriaTraces/Jaeger — they are queried differently |
| `GRAFANA_LOGS_DATASOURCE_UID` | `loki` | Datasource the log links open |
| `GRAFANA_LOGS_DATASOURCE_TYPE` | `loki` | Type of that datasource |
| `TRACING_SAMPLE` | `1.0` | Head sampling ratio in (0, 1], via a ParentBased sampler |
| `SPAN_EXPORT_DELAY_MS` | `2000` | Batch export interval |
| `ENABLE_CONSOLE_SPAN_EXPORTER` | `false` | Print spans to stdout, for debugging without a backend |
| `TRACE_EXCLUDE_URLS` | `healthz,livez,readyz,metrics,favicon.ico` | Regexes that must not create spans |
| `LOGLEVEL` | `INFO` | Root log level |
| `LOG_STDOUT` | `true` | Write JSON logs to stdout. This is how logs reach Loki — see below |
| `LOG_FILE` | – | Also write to this rotating file, for file-tailing agents |
| `LOG_MAX_MSG_LENGTH` | `0` | Truncate `msg` above this length (0 = never) |
| `ENABLE_REQUEST_LOG` | `true` | Emit `type=request` lines |
| `CAPTURE_WARNINGS` | `true` | Route `warnings.warn()` through logging so it is JSON too |
| `LOG_EXCLUDE_URLS` | `/healthz,/livez,/readyz,/metrics` | Path prefixes not to request-log |
| `CORRELATION_ID_HEADERS` | `x-correlation-id,correlation-id,x-request-id,request-id` | Inbound headers to reuse as the correlation id |
| `CORRELATION_ID_HEADER` | `X-Correlation-ID` | Header written on responses and on outbound calls |
| `ENABLE_CORRELATION_ID_PROPAGATION` | `true` | Forward the correlation id on outbound calls |
| `SENTRY_DSN` | – | Enables Sentry error reporting. Unset = off |
| `ENABLE_SENTRY` | `true` | Master switch, independent of the DSN |
| `SENTRY_ENVIRONMENT` | `DEPLOYMENT_ENVIRONMENT` | Sentry environment |
| `SENTRY_RELEASE` | `SERVICE_VERSION` | Sentry release, for regression tracking |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | 0 = errors only, Tempo owns tracing. See below |
| `SENTRY_INSTRUMENTER` | `sentry` | `otel` makes Sentry read spans from the OTel provider |
| `SENTRY_EVENT_LEVEL` | `ERROR` | Log level that becomes a Sentry event |
| `SENTRY_BREADCRUMB_LEVEL` | `INFO` | Log level that becomes a breadcrumb |
| `SENTRY_SEND_DEFAULT_PII` | `false` | Send request bodies, headers and client IP |
| `GRAFANA_URL` | – | Set it and every Sentry event gains clickable Loki/Tempo links |
| `GRAFANA_LOKI_SELECTOR` | `{job="fastapi"}` | Stream selector the generated LogQL starts from |
| `GRAFANA_DASHBOARD_UID` | `wan` | Dashboard the generated link points at |
| `ENABLE_PROMETHEUS_METRICS` | `true` | Expose `/metrics` |
| `METRICS_ENDPOINT` | `/metrics` | Where |
| `ENABLE_HEALTH_ENDPOINTS` | `true` | Add `/healthz`, `/livez`, `/readyz` |
| `ENABLE_SCALAR_DOC` | `true` | Serve the Scalar reference |
| `SCALAR_DOC_ENDPOINT` | `/scalar` | Where |
| `SCALAR_THEME` | `purple` | Scalar theme name |
| `SCALAR_DARK_MODE` | `true` | Dark mode |
| `SCALAR_JS_URL` | pinned CDN | Point at a self hosted bundle for air-gapped deploys |
| `ENABLE_HTTPX_INSTRUMENTATION` | `false` | Trace outbound `httpx` calls (needs the `httpx` extra) |
| `ENABLE_REQUESTS_INSTRUMENTATION` | `false` | Trace outbound `requests` calls (needs the `requests` extra) |
| `JAEGER_HOST` / `JAEGER_PORT` | – / `6831` | Deprecated, see below |

### Using it from your own modules

Nothing to import. `patch()` installs the JSON handler on the **root** logger, and any
logger made the usual way propagates to it:

```python
import logging

logger = logging.getLogger(__name__)   # at module scope, as normal

def charge(amount):
    logger.info(f'charging {amount}')          # JSON, with the request's trace id
    logger.warning('large charge')
    logger.exception('charge failed')          # traceback included, still correlated
```

The trace id is resolved when the line is formatted, not when the logger is created, so
this works regardless of import order -- a module imported before `patch()` runs is fine.
`logger`, `module` and `line_no` report the real call site:

```json
{"msg": "charging 150", "type": "log", "logger": "billing.service", "level": "INFO",
 "module": "service", "line_no": 8, "correlation_id": "50b24274-...",
 "traceID": "1ae8303987e6118b316779da6f4e2eeb", "spanID": "e030b51293bcc1b1", ...}
```

Because `logger` is the dotted module path rather than `root`, it is worth filtering on.
Alloy attaches it as structured metadata:

```logql
{job="fastapi"} | logger="billing.service" | level="ERROR"
```

As many loggers as you like, including hierarchies — trace context is resolved per
*record*, not per logger, so it does not matter how many you have or when you made them:

```python
log1 = logging.getLogger('log1')
log2 = logging.getLogger('log2')
pool = logging.getLogger('app.db.pool')
```

All three emit the same `traceID` and `correlation_id` within one request, and stay
correct under concurrency because the correlation id lives in a `ContextVar` — each
request (and each task spawned from it) gets its own copy. Verified with 12 interleaved
requests × 4 loggers: 48 lines, zero stamped with a neighbour's id, 12 distinct traces.
Records also emit exactly once despite propagating through `app.db` and `app` on the way
to root, since only root carries a handler.

Two things that trip people up:

- **A child logger's own level wins.** Propagation consults *handler* levels, not
  ancestor *logger* levels, so `logging.getLogger(__name__).setLevel(logging.DEBUG)`
  emits DEBUG even while `LOGLEVEL=INFO`. Handy for turning up one module; surprising if
  you expected root to filter it.
- **`logger.propagate = False` or your own handler opts out.** Records then never reach
  the JSON handler and you get plain text. Don't add handlers to your own loggers.

`warnings.warn()` bypasses `logging` altogether and would be the one plain-text line
Loki cannot parse, so `patch()` enables `logging.captureWarnings(True)`; warnings arrive
as JSON under the `py.warnings` logger. Disable with `CAPTURE_WARNINGS=false`.

### Correlation ids across services

One id for one logical request, however many services it touches. Both directions are
automatic.

**Inbound.** If the caller already sent a correlation id, it is reused rather than
regenerated. Any header in `CORRELATION_ID_HEADERS` is accepted, checked in order, so
services that call it `X-Request-ID` work without config:

```bash
curl localhost:7072/random -H 'X-Correlation-ID: id-from-service-a'
# every log line for this request logs correlation_id=id-from-service-a
# and the response echoes back X-Correlation-ID: id-from-service-a
```

Only when no header is present is a fresh uuid4 minted.

**Outbound.** The id is attached to calls this service makes, so the next service
receives it and reuses it. An outgoing request starts with empty headers — it does not
inherit the inbound ones — so this is a real step, not a no-op:

```python
# no headers passed by hand; the id rides along next to `traceparent`
async with httpx.AsyncClient() as client:
    await client.get('http://service-b/work')
```

That works through an OpenTelemetry `TextMapPropagator`
(`wan/propagation.py`) composed onto the global propagator, which is what
every OpenTelemetry HTTP instrumentation injects into its outbound headers. So it covers
httpx, requests, aiohttp, urllib and grpc alike instead of needing an integration per
client library. The value is read from the request context at the moment of the call, so
it is always the current request's id.

**It requires the client to be instrumented.** Set `ENABLE_HTTPX_INSTRUMENTATION=true`
or `ENABLE_REQUESTS_INSTRUMENTATION=true` (the `httpx` / `requests` extras above). For a client
that is not instrumented, pass the header yourself:

```python
async with httpx.AsyncClient(headers=wan.correlation_headers()) as client:
    ...
```

Reading the current ids anywhere in a request:

```python
trace_id, span_id, dd_trace_id = wan.get_trace_ids()
correlation_id = wan.get_correlation_id()
```

Then in Loki, one id gives you every service's logs for that request:

```logql
{job=~"fastapi|service-b"} | correlation_id="id-from-service-a"
```

### Sentry

Set a DSN and errors start flowing; unset, it is completely off.

```bash
pip3 install 'wan[sentry] @ git+https://github.com/Scicom-AI-Enterprise-Organization/wan'
SENTRY_DSN=https://key@o0.ingest.sentry.io/0 uvicorn app:app --port 7072
```

Sentry earns its place by doing what logs and traces cannot: grouping the same error
across deploys, deduplicating it, alerting on it, and keeping the stack trace with local
variables. It is a fourth signal, not a replacement for the other three.

**Every event carries the same ids as Loki and Tempo**, which is the whole point:

```json
"tags": {
  "correlation_id": "SENTRY-CID-42",
  "traceID": "6ae51777c29ffd57d3e05174d4638299",
  "spanID": "07454da51ff17487",
  "service": "wan"
},
"contexts": {"trace": {"trace_id": "6ae51777c29ffd57d3e05174d4638299"}}
```

So from a Sentry issue you can go straight to the other two:

```logql
{job="fastapi"} | correlation_id="SENTRY-CID-42"     # Loki: every log line for it
```
```
6ae51777c29ffd57d3e05174d4638299                     # Tempo: the trace
```

Search Sentry itself by `correlation_id:SENTRY-CID-42` to come the other way.

#### Linking a Sentry issue to Grafana

Set `GRAFANA_URL` to the address a human reaches Grafana on, and every event gains ready
made Explore links:

```bash
GRAFANA_URL=http://192.168.88.102:3010
```

They land in two places on the issue, because Sentry versions differ in what they render
as a clickable link — a **GRAFANA** context card, and **Additional Data**:

```
contexts.grafana:
  logs_and_trace  -> Explore, logs and flame graph side by side
  logs            -> Loki, filtered to this request's correlation id
  trace           -> Tempo, this trace
  dashboard       -> the service dashboard

extra:
  grafana_logs, grafana_trace, grafana_logs_and_trace, grafana_dashboard
```

The logs link filters by correlation id rather than trace id, because a correlation id
covers every service the request touched and survives trace sampling:

```logql
{job="fastapi"} | correlation_id="grafana-link-001"
```

`GRAFANA_URL` must be reachable from your browser. An in-cluster address like
`http://grafana:3000` produces links nobody can open.

The builders are importable if you want links elsewhere:

```python
from wan.grafana import links_for

links_for('http://grafana.example.com:3010', correlation_id=..., trace_id=...)
```

#### Why this needed work

Two defaults would otherwise break the correlation, and neither fails loudly:

- **Sentry mints its own trace id.** An issue would carry an id that exists nowhere in
  Tempo or Loki. Every event's `contexts.trace.trace_id` is rewritten to the
  OpenTelemetry id instead.
- **Sentry's ASGI middleware wraps the whole app**, outside every user middleware. By
  the time it captures an exception, this library's correlation ContextVar has been reset
  and the OpenTelemetry span has ended, so reading the ids at send time finds nothing.
  They are written onto Sentry's per-request isolation scope while the request is still
  live instead (`wan/sentry.py:bind_request_scope`).

Also handled: an unhandled 500 is logged by this library at ERROR *with* the traceback,
and Sentry's FastAPI integration captures the same exception, so the request logger is
passed to `ignore_logger()` — otherwise every failure arrives in Sentry twice.

#### Errors only, by default

`SENTRY_TRACES_SAMPLE_RATE` defaults to `0`: Sentry handles errors, Tempo handles
tracing. Running both tracers over one request is what produces two different trace ids
for the same work.

To use Sentry's performance monitoring as well, raise the rate *and* set
`SENTRY_INSTRUMENTER=otel` so Sentry reads spans from the existing OpenTelemetry provider
rather than creating its own:

```bash
SENTRY_TRACES_SAMPLE_RATE=0.1 SENTRY_INSTRUMENTER=otel
```

With `instrumenter=otel` the trace context is left alone, since there Sentry's trace id
is the key to its own span data. This path is wired up but not covered by the test suite,
which mocks the Sentry ingest endpoint rather than using a real project — verify it
against your own Sentry before relying on it.

### Reading Sentry inside Grafana

The stack provisions a Sentry datasource (uid `sentry`) and a dashboard panel of
unresolved issues, so an issue can be read next to the logs and traces of the same
request. It is the only datasource here that is not self-contained — Sentry lives
outside the stack — so it stays inert until three variables are set:

```bash
# grafana/.env -- tracked, so non-secret values only
SENTRY_GRAFANA_URL=https://sentry.io   # what the Grafana *container* dials
SENTRY_PUBLIC_URL=https://sentry.io    # what a *browser* opens
SENTRY_ORG_SLUG=your-org
```

Those two are the same address on sentry.io and different ones for a self-hosted
Sentry on this machine: `http://host.docker.internal:9090` for the container,
`http://localhost:9090` for the browser.

```bash
# the token is secret, and grafana/.env is tracked -- export it instead
export SENTRY_AUTH_TOKEN=...
make restart-grafana
```

The auth token is **not** the DSN. A DSN's key only authorises *writing* events;
reading them back needs a Sentry auth token (User settings → Auth Tokens, or an
organisation token) with `project:read` and `event:read`.

`http://localhost:9090` will not work: inside the Grafana container `localhost` is
Grafana. Use `host.docker.internal`, which the compose file maps to the host gateway so
it resolves on Linux too.

The `grafana-sentry-datasource` plugin is installed by `GF_INSTALL_PLUGINS` at container
start, so that one service needs outbound internet on its first run.

#### From a trace to its Sentry issues

The Tempo datasource carries a provisioned correlation on `traceID`: select that field
on a span in Explore and Grafana runs `traceID:<id>` against Sentry beside the trace.
Every event `wan` sends is tagged with `traceID`, so the match is exact — the bare id
and `trace:<id>` both return nothing.

Each row includes Sentry's own `Permalink`, which is the link to follow. A URL built by
hand is a guess: a self-hosted Sentry does not necessarily use sentry.io's
`/organizations/<org>/issues/` layout, and every path on one answers `303` to an
unauthenticated request, so a wrong guess cannot be told from a right one.

Two things about provisioning correlations are worth knowing before editing them, both
found the hard way:

- only `query` correlations can be provisioned. `type: external` **panics Grafana 11.6
  on startup** (`interface conversion` in `makeCreateCorrelationCommand`) — the HTTP API
  accepts it, provisioning does not;
- `config.type: query` must be spelled out. Omitted, it is the Go zero value, which
  Grafana rejects as *"correlation contains non default value in config.type"* and then
  refuses to start.

Both failures take Grafana down rather than skipping the correlation, so change that
block with a container restart in hand.

#### Searching Sentry by trace id

In Explore with the Sentry datasource: **Query Type** `Issues`, **Query**
`traceID:<id>`. Leave Project and Environment empty.

```
traceID:b6d6a9904b549230cb96f5cdc0833d86
```

The tag qualifier is not optional. A bare trace id matches nothing, and neither does
`trace:<id>` — `traceID` is the tag `wan` puts on every event, and it is case
sensitive. Two other things read as "it is broken" when it is not:

- **the time range applies.** Explore defaults to the last hour; an older trace needs
  the range widened before its issue appears;
- **`Count` is the issue's total, not this trace's.** Sentry groups by fingerprint, so
  one issue covers every occurrence. The search answers "which issue contains this
  trace", which is the useful question — it is not a count of failures for that one
  request.

The datasource offers Issues, Events, Events Stats, Stats, Spans, Spans Stats and
Metrics. Which of those answer depends on the Sentry behind it: issues, events, event
time series and outcome stats are the four a dashboard usually draws.

### Correlation id vs trace id

They answer different questions and both are on every line:

| | Set by | Survives hops via | Use it for |
|---|---|---|---|
| `traceID` | OpenTelemetry | `traceparent` (W3C standard) | jumping to the trace in Tempo; span timings |
| `correlation_id` | this library | `X-Correlation-ID` | grepping logs, and giving the id to a customer in an error response |

A trace id only exists while tracing is on and is dropped by sampling; a correlation id
is always present and is safe to show a user or put in a support ticket.

## Pushing to a remote backend

### Traces: OTLP/HTTP with basic auth

Nothing about the library assumes Tempo. Any OTLP ingest works — Grafana Cloud,
VictoriaTraces, an OpenTelemetry Collector behind a reverse proxy — and most of them
authenticate the push with HTTP basic auth:

```bash
OTLP_ENDPOINT=https://collector.example.com/insert/opentelemetry/v1/traces
OTLP_USERNAME=...
OTLP_PASSWORD=...
```

That is the whole configuration. Two things are inferred from the endpoint so a remote
deploy cannot fail silently on them:

- an endpoint ending in `/v1/traces` selects **OTLP/HTTP**, because only the HTTP
  exporter takes a signal path — the gRPC exporter would dial an HTTPS ingest and drop
  every batch. Set `OTLP_PROTOCOL` explicitly to override;
- an `https://` endpoint turns **`OTLP_INSECURE` off**, so a remote push is not
  downgraded by the plaintext default that suits an in-cluster collector.

The endpoint may also be given as the collector root (`https://host/insert/opentelemetry`)
and `/v1/traces` is appended. `OTLP_USERNAME` / `OTLP_PASSWORD` are encoded into an
`Authorization: Basic` header; if `OTLP_HEADERS` sets `Authorization` itself — a bearer
token, say — that wins, and the credentials are never logged.

Run the example app against it. It reads a `.env` next to `app.py` (via the `example`
extra's `python-dotenv`), so the credentials stay out of your shell history:

```bash
cat > .env <<'EOF'
OTLP_ENDPOINT=https://collector.example.com/insert/opentelemetry/v1/traces
OTLP_USERNAME=...
OTLP_PASSWORD=...
EOF

uvicorn app:app --port 7072
curl 'http://localhost:7072/nested'
```

The root `.env` is gitignored. The library itself never reads a file — it only reads the
environment — so in production these come from your secret store as normal variables.

For the docker stack, put the same three in `grafana/.env`; compose passes them through
and they override the local Tempo default:

```bash
echo 'OTLP_ENDPOINT=https://collector.example.com/insert/opentelemetry/v1/traces' >> grafana/.env
make restart
```

### Deep links into a remote Grafana

The links Sentry events carry default to the local stack: a Tempo datasource with uid
`tempo`, a Loki one with uid `loki`. A shared Grafana names them differently, and a
Jaeger-API backend — VictoriaTraces, or Jaeger itself — is *queried* differently from
Tempo: it looks a trace up by id with no `queryType`, where Tempo wants
`queryType: traceql`. Send Tempo's query model to it and the pane comes up empty.

Rather than make you look those up, open Explore on that Grafana, pick the trace
datasource, and paste the URL out of the address bar:

```bash
GRAFANA_DATASOURCE_URL='https://grafana.example.com/explore?schemaVersion=1&panes=...'
```

The base URL, the datasource uid and its type all come off that one link.
`GRAFANA_URL`, `GRAFANA_TRACE_DATASOURCE_UID` and `GRAFANA_TRACE_DATASOURCE_TYPE`
override it piecemeal if you would rather be explicit:

```bash
GRAFANA_URL=https://grafana.example.com
GRAFANA_TRACE_DATASOURCE_UID=victoria-traces
GRAFANA_TRACE_DATASOURCE_TYPE=jaeger
GRAFANA_LOGS_DATASOURCE_UID=victoria-logs-loki
GRAFANA_DASHBOARD_UID=            # empty: that Grafana does not host this dashboard
```

### Verifying the push landed

The exporter is quiet on success and logs `Failed to export` on failure, so check the
app's own logs first. Then read the trace back out of the backend. Through Grafana,
with a service-account token, the datasource proxy needs no direct network route to the
collector:

```bash
TRACE_ID=$(curl -sD- -o /dev/null http://localhost:7072/nested \
  | awk 'tolower($1) == "x-trace-id:" {print $2}' | tr -d '\r')

# Jaeger-API backends (VictoriaTraces, Jaeger). Tempo: /api/traces/$TRACE_ID
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "$GRAFANA_URL/api/datasources/proxy/uid/$DATASOURCE_UID/api/traces/$TRACE_ID"
```

Allow a few seconds: every backend indexes asynchronously, so a lookup fired
immediately after the request can 404 on a trace that is on its way in. Failing that,
`ENABLE_CONSOLE_SPAN_EXPORTER=true` prints every span to stdout, which separates "the
app is not producing spans" from "the collector is not accepting them".

## Example

<img src="docs/scalar.png" width="900px">

The `grafana/` directory is a complete working stack: the app, Tempo, Loki, Alloy,
Prometheus and Grafana, with datasources and a dashboard already provisioned.

1. Run the whole thing,

```bash
docker compose -f grafana/docker-compose.yaml up -d --build
```

| Service | URL |
|---|---|
| App | http://localhost:7072 |
| Scalar docs | http://localhost:7072/scalar |
| Grafana | http://localhost:3010/d/wan |
| Loki | http://localhost:3110 |
| Tempo | http://localhost:3210 |
| Prometheus | http://localhost:9092 |
| Alloy | http://localhost:12345 |

Ports live in `grafana/.env`. They are shifted off the canonical
3000/3100/3200/4317/9090 so this stack can run next to another Grafana stack on the
same machine; delete that file to use the defaults.

#### Reaching it from another machine

Ports publish on `BIND_HOST`, which defaults to `0.0.0.0`, so Grafana is already
reachable on this host's LAN or VPN address — no change needed:

```
http://192.168.88.102:3010/d/wan
```

Two things to set when you do share it:

- **`GRAFANA_ROOT_URL`** — Grafana builds share links and alert notification links from
  its `root_url`. Left at `localhost` it hands out URLs that only work on this machine.
  Point it at the address people actually use:
  `GRAFANA_ROOT_URL=http://192.168.88.102:3010/`
- **Anonymous access is on.** `GF_AUTH_ANONYMOUS_ENABLED=true` means anyone who can
  route to the port can read every dashboard and query every datasource, with no login.
  Fine on a trusted network, not on an untrusted one. Either set
  `BIND_HOST=127.0.0.1` to keep it local, or turn anonymous auth off and use
  `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`.

Note this publishes Loki, Tempo and Prometheus too, none of which have any
authentication (`auth_enabled: false`). If you only want Grafana exposed, set
`BIND_HOST=127.0.0.1` and give Grafana alone a `"0.0.0.0:${GRAFANA_PORT}:3000"` mapping.

To generate links against the shareable address:

```bash
python3 scripts/links.py --grafana http://192.168.88.102:3010
```

2. Request the API,

```bash
curl -X 'GET' \
  'http://localhost:7072/random?minimum=0.1&maximum=2' \
  -H 'accept: application/json'
```

3. Open Grafana, and follow the trace id in either direction:

- **Loki → Tempo.** Explore → Loki → `{job="fastapi"}`. Expand any line and click
  **View trace**. That link comes from a derived field matching `traceID=(\w+)` in the
  raw line.
- **Tempo → Loki.** Open a trace, select a span, click **Logs for this span**. That
  runs `{job="fastapi"} |= "<traceId>"` against Loki.

### Running the app on the host instead

The stack also tails `grafana/logs/*.log`, so an app run outside docker still lands in
Loki:

```bash
docker compose -f grafana/docker-compose.yaml up -d tempo loki alloy prometheus grafana

OTLP_ENDPOINT=http://localhost:4327 \
LOG_FILE=grafana/logs/app.log \
uvicorn app:app --reload --host 0.0.0.0 --port 7072
```

### Two services sharing one correlation id

`app` calls itself, which shows propagation but keeps a single `service.name`. The
`two-services` profile runs two genuinely separate services, both exporting to the same
Tempo and both shipping logs to the same Loki:

```bash
docker compose -f grafana/docker-compose.yaml --profile two-services up -d --build
python3 scripts/two_services.py
```

It calls `service-a` with a correlation id, as a gateway or upstream service would, and
asserts the id is reused by A, forwarded to B, and that one trace covers both:

```
A -> B response: {"service": "service-a", "depth": 1, "downstream": {"service": "service-b", "message": "leaf"}}

PASS  A actually called B                                    service-a -> service-b
PASS  A reused the caller-supplied correlation id            A echoed back sim-from-caller-...
PASS  one correlation_id filter returns both services' logs  ['service-a', 'service-b'] on 1 trace
PASS  each service is independently queryable in Loki        service-a=3 lines, service-b=2 lines
PASS  Tempo holds one trace covering both services           7 spans across ['service-a', 'service-b']
PASS  the logged traceID is the one Tempo indexed            5 log lines carry traceID=...
PASS  Tempo service graph shows the A -> B edge              1 series for service-a -> service-b
```

The resulting log lines, from two different containers, one correlation id and one
trace id:

```
service=service-a  type=log      correlation_id=sim-from-caller-...  traceID=7a15c7cc..  spanID=2e41f7133b0d528a
  chain depth=1 on service-a
service=service-a  type=log      correlation_id=sim-from-caller-...  traceID=7a15c7cc..  spanID=2e41f7133b0d528a
  HTTP Request: GET http://service-b:8000/chain?depth=0 "HTTP/1.1 200 OK"
service=service-a  type=request  correlation_id=sim-from-caller-...  traceID=7a15c7cc..  spanID=2e41f7133b0d528a
  GET /chain -> 200
service=service-b  type=log      correlation_id=sim-from-caller-...  traceID=7a15c7cc..  spanID=a0af0b131e94f8ad
  chain depth=0 on service-b
service=service-b  type=request  correlation_id=sim-from-caller-...  traceID=7a15c7cc..  spanID=a0af0b131e94f8ad
  GET /chain -> 200
```

Same `traceID`, same `correlation_id`, different `spanID` per service. In Grafana:

```logql
{job="fastapi"} | correlation_id="<the id>"      # both services' logs
{service="service-b"} | correlation_id="<the id>" # just B's
```

Tempo's service graph gains a real edge, visible under the Tempo datasource's
**Service Graph** tab:

```
user       -> service-a   count=1
service-a  -> service-b   count=1
```

Point a service at any other instance with `DOWNSTREAM_URL` to extend the chain.

### Deep links into Grafana

`two_services.py` prints ready-to-open Grafana links when it finishes. For any other
trace, generate them:

```bash
python3 scripts/links.py                                  # newest trace in Tempo
python3 scripts/links.py --trace 7408a823301db5736ca02a4c2f163630
python3 scripts/links.py --correlation sim-from-caller-1786967724
python3 scripts/links.py --service service-a
```

It prints six links: the trace in Tempo, its logs in Loki, **both side by side in one
Explore view**, a TraceQL search, the service graph, and the dashboard.

Grafana's Explore state is a JSON blob in the query string, so these are generated
rather than hand written — the shape is `?schemaVersion=1&orgId=1&panes={...}`, with one
entry per pane:

```python
panes = {
  "lg": {"datasource": "loki",  "queries": [{"expr": '{job="fastapi"} | correlation_id="..."'}], ...},
  "tr": {"datasource": "tempo", "queries": [{"queryType": "traceql", "query": "<traceID>"}], ...},
}
```

Two keys in `panes` is what gives you logs and the flame graph in one screen, which is
usually what you want when debugging a cross-service request.

`scripts/links.py` is importable if you want the URLs elsewhere:

```python
from links import explore_url, loki_pane, tempo_pane

explore_url({'lg': loki_pane('{job="fastapi"}'), 'tr': tempo_pane(trace_id)})
```

### Verifying it actually works

```bash
python3 scripts/e2e.py
```

Standard library only, no dependencies. It generates traffic and then asserts the
whole chain: that every log line is valid JSON with a well formed trace id, that the
trace id from a log line resolves in Tempo, that the same id finds those logs back in
Loki, that Scalar serves and its bundle is pinned and reachable, that Prometheus
scrapes the app and receives Tempo's generated span metrics, and that Grafana can run
each dashboard query through its own datasource proxy. 40 checks.

```
== loki (logs) ==
PASS  app logs reach Loki                                    46 entries under {job="fastapi"}
PASS  Tempo -> Loki: trace id finds its log lines            (Tempo -> Loki link)
PASS  Loki -> Tempo: derived field regex matches the raw line 48 lines match traceID=(\w+)
PASS  traceID is structured metadata, not a label            traceID is not a label
...
all 40 checks passed
```

Unit tests:

```bash
pip install -e '.[dev]'
pytest
```

## How logs reach Loki (there is no LOKI_URL on the app)

The four signals do not all work the same way, which is the most common source of
confusion. Two are **pushed by the application**, two are **collected from outside it**:

| Signal | Backend | How it gets there | App config |
|---|---|---|---|
| Traces | Tempo (or any OTLP ingest) | app pushes OTLP | `OTLP_ENDPOINT=http://tempo:4317` |
| Errors | Sentry | app pushes HTTPS | `SENTRY_DSN=https://...` |
| **Logs** | **Loki** | **Alloy reads the app's stdout and pushes** | **none — just write to stdout** |
| Metrics | Prometheus | Prometheus scrapes `/metrics` | none — just expose the endpoint |

So there is deliberately **no `LOKI_URL` on the application**. Its entire responsibility
for logging is to write one JSON object per line to stdout, which `LOG_STDOUT=true` (the
default) already does. Alloy tails the container and pushes to Loki:

```
app (stdout, JSON)  ──▶  Alloy  ──▶  Loki
```

That indirection is deliberate. Writing to stdout cannot block a request, cannot fail
because Loki is down, and survives the process crashing — Alloy buffers and retries on
its own. An in-process Loki handler couples request latency to your log backend.

### Pointing at a different Loki

The URL lives in `grafana/alloy.alloy`, and is overridable:

```bash
LOKI_URL=http://loki.internal:3100/loki/api/v1/push \
  docker compose -f grafana/docker-compose.yaml up -d alloy
```

For Grafana Cloud, which needs auth, add a `basic_auth` block to the same endpoint:

```alloy
loki.write "default" {
  endpoint {
    url = coalesce(sys.env("LOKI_URL"), "http://loki:3100/loki/api/v1/push")
    basic_auth {
      username = sys.env("LOKI_USERNAME")   // your numeric Grafana Cloud user id
      password = sys.env("LOKI_PASSWORD")   // an access policy token
    }
  }
}
```

### If Alloy is not an option

When you cannot run an agent — some PaaS hosts give you no sidecar and no log drain —
the fallback is to have the app write to a file and tail it, which this stack already
supports via `grafana/logs/*.log`:

```bash
LOG_FILE=grafana/logs/app.log uvicorn app:app --port 7072
```

Only as a last resort, push from inside the process with a Loki logging handler
(`python-logging-loki` and similar). Use `wan`'s formatter so the JSON schema stays the
same, and be aware you are accepting the coupling described above:

```python
import logging, logging_loki
handler = logging_loki.LokiHandler(url='http://loki:3100/loki/api/v1/push', version='1')
handler.setFormatter(wan.patch(app=app)['formatter'])
logging.getLogger().addHandler(handler)
```

## How the stack fits together

```
                       ┌──────────────┐
        OTLP :4317 ───▶ │    Tempo     │ ──── span metrics ──┐
       (traces)        └──────────────┘   (remote_write)     │
             ▲                                              ▼
      ┌──────┴──────┐   stdout JSON    ┌───────┐      ┌────────────┐
      │  FastAPI    │ ───────────────▶ │ Alloy │ ───▶ │ Prometheus │
      │  + patch()  │                  └───────┘      └────────────┘
      └─────────────┘                       │ push          ▲
             │ /metrics scrape              ▼               │
             └────────────────────────▶ ┌──────┐            │
                                        │ Loki │            │
                                        └──────┘            │
                                            └───── Grafana ─┘
```

Alloy reads container stdout, parses the JSON, sets low cardinality Loki labels
(`level`, `log_type`, `service`, `environment`) and attaches the high cardinality
fields (`traceID`, `spanID`, `correlation_id`, `response_status`, `request_route`) as
**structured metadata** rather than labels — filterable without multiplying streams.

## Notes

### Deprecated Jaeger exporter

`jaeger_host` / `jaeger_port` still work but the Jaeger thrift exporter was deprecated
upstream in OpenTelemetry 1.16 and is not installed by default. Tempo ingests OTLP
natively, so prefer `otlp_endpoint`. If you need it:

```bash
pip3 install 'wan[jaeger] @ git+https://github.com/Scicom-AI-Enterprise-Organization/wan'
JAEGER_HOST=localhost JAEGER_PORT=6841 uvicorn app:app --port 7072
```

### Duplicate exception logs

An unhandled exception is logged twice: once by this library, attached to the
`type=request` line with the trace id, correlation id and traceback; and once by
uvicorn from outside the request, with `traceID: null`. The uvicorn one is deliberately
left alone — it is the only record if a failure happens outside the middleware stack.
Filter on `log_type="request"` if you only want the correlated copy.

### Sampling

`TRACING_SAMPLE` uses a `ParentBased(TraceIdRatioBased(...))` sampler, so a trace is
never sampled halfway: if an upstream service sampled it, this service keeps its spans
too, and traces never come out with holes in the middle.

### Air-gapped Scalar

The Scalar bundle is pinned to an exact version rather than `@latest`, so an upstream
release cannot break the docs page of an already deployed service. With no internet
access, download `standalone.min.js`, serve it yourself and set `SCALAR_JS_URL`; the
page shows an explicit error and a link to the raw spec if the bundle cannot load.

## What changed from the original

This is a rewrite of [huseinzol05/fastapi-loki-tempo](https://github.com/huseinzol05/fastapi-loki-tempo).
`patch()` keeps the same signature, and the log schema is unchanged, so it is a drop-in
replacement. Behaviour that was wrong or missing before:

- **Loki, Grafana and Prometheus were not in the stack at all.** `grafana/docker-compose.yaml`
  only ran Tempo and a Jaeger UI, so nothing in a repo called `wan`
  actually shipped a log to Loki. Now Loki, Alloy, Prometheus and Grafana are all
  there, with datasources, derived fields and a dashboard provisioned.
- **Trace ids were not zero padded.** `hex(trace_id)[2:]` drops leading zeros, so
  roughly 1 in 16 trace ids was shorter than 32 characters and could not be looked up
  in Tempo. Now formatted as `032x`.
- **`tracing_sample` was accepted and ignored.** No sampler was ever built, so
  sampling was always 100%.
- **`enable_prometheus_metrics` was accepted and ignored.** `/metrics` was always
  exposed.
- **Correlation ids cost a stack walk per log line.** `json_logging` searched 11 stack
  frames on every log call looking for a local named `request`, which is slow and
  silently wrong when the frame depth changes. It is a `ContextVar` now, and
  `json_logging` (unmaintained since 2021) is no longer a dependency.
- **`request.user` was read unguarded**, raising `AssertionError` on every request
  unless Starlette's `AuthenticationMiddleware` happened to be installed.
- **Non-JSON startup logs.** uvicorn kept its own handlers, so its lines stayed plain
  text and Loki could not parse them. All loggers are rerouted now.
- **`uvicorn.access` duplicated every request** in a second, less useful format.
- Unhandled exceptions, 4xx/5xx levels, response sizes for streaming responses,
  `spanID`, `X-Trace-Id`, health endpoints, `root_path` aware docs, an unpinned Scalar
  CDN, `min`/`max` being ignored by the example `/random` handler, and the
  `__version__` / `setup.py` version mismatch.

## Development

```bash
pip install -e '.[dev]'
pytest                                                    # unit tests
docker compose -f grafana/docker-compose.yaml up -d --build
python3 scripts/e2e.py                                    # end to end
docker compose -f grafana/docker-compose.yaml down -v     # tear down
```

## License

MIT
