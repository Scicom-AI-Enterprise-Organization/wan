# CLAUDE.md

`wan` is a FastAPI boilerplate: one `wan.patch(app=app)` call wires up JSON logging,
OpenTelemetry tracing, Prometheus metrics, health probes, Scalar docs and Sentry, all
sharing one trace id and one correlation id per request.

## Commands

```bash
pip install -e '.[dev]'      # or: make install
pytest -q                    # or: make test
make up                      # full local stack (app + Tempo/Loki/Alloy/Prometheus/Grafana)
make e2e                     # end-to-end check against a running stack
make restart-grafana         # apply provisioning changes (or a new SENTRY_AUTH_TOKEN)
make down                    # tear down, deleting volumes
```

CI runs `pytest -q` on Python 3.9–3.13, then the e2e script against the compose stack.
Keep new code 3.9-compatible: `Optional[str]`, not `str | None`.

## Layout

| Path | What it holds |
|---|---|
| `wan/os_env.py` | Every env var, read **at import time** into module constants |
| `wan/tracing.py` | Tracer provider, OTLP exporters (gRPC and HTTP), sampler |
| `wan/logs.py` | `JsonLogFormatter`, root-logger setup |
| `wan/middleware.py` | Request logging, correlation id lifecycle |
| `wan/grafana.py` | Explore deep-link builders (used for Sentry event links) |
| `wan/sentry.py` | SDK init and the `before_send` that stamps ids and links |
| `app.py` | Example service; loads a repo-root `.env` before `import wan` |
| `grafana/` | docker-compose stack, Alloy config, provisioning |
| `tests/` | pytest; `test_logs.py` pins the public log schema, `test_tracing.py` covers exporter wiring, `test_health.py` readiness |

Two things follow from `os_env.py` reading the environment at import time:

- a `.env` must be loaded **before** `import wan`, which is why `app.py` calls
  `load_dotenv()` above its imports;
- `patch()`'s defaults bind at function-definition time, so tests that need a different
  value pass it as a keyword argument rather than setting `os.environ`.

## OTLP HTTP push

Traces are pushed by the application itself (logs are not — Alloy collects those from
stdout). Any OTLP ingest works: Tempo, Grafana Cloud, VictoriaTraces, an OpenTelemetry
Collector. Three variables are the whole configuration:

```bash
OTLP_ENDPOINT=https://collector.example.com/insert/opentelemetry/v1/traces
OTLP_USERNAME=...
OTLP_PASSWORD=...
```

`OTLP_URL` is accepted as an alias for `OTLP_ENDPOINT`. The equivalent in code:

```python
wan.patch(
    app=app,
    otlp_endpoint='https://collector.example.com/insert/opentelemetry/v1/traces',
    otlp_username='...',
    otlp_password='...',
)
```

Two values are inferred from the endpoint rather than defaulted, because getting either
wrong fails **silently** — spans are dropped with nothing logged at the call site:

- ending in `/v1/traces` selects `OTLP_PROTOCOL=http`. Only the HTTP exporter takes a
  signal path; the gRPC exporter would dial an HTTPS ingest and drop every batch;
- an `https://` endpoint sets `OTLP_INSECURE=false`, so a remote push is not downgraded
  by the plaintext default that suits an in-cluster collector.

Both are still overridable. An endpoint given as the collector root gets `/v1/traces`
appended, so a URL copied from a vendor's docs works either way.

Credentials become an `Authorization: Basic` header. `OTLP_HEADERS`
(`key=value,key=value`) is merged over them, so an explicit bearer token wins. Never log
the resolved header dict — `build_otlp_exporter()` logs only the endpoint.

**Header keys are lowercased on the gRPC path.** gRPC metadata keys must be lowercase;
a capitalised one is rejected by the channel, not the collector, so every batch dies
with `Invalid metadata` and no request ever reaches the far end. HTTP header names are
case insensitive, which is why this hides until someone points a credentialled exporter
at a gRPC endpoint.

The relevant seams in `wan/tracing.py`: `build_otlp_exporter()` returns one configured
exporter, `build_headers()` merges credentials with `OTLP_HEADERS`, and
`http_traces_endpoint()` normalises the path. Test them directly; `setup_tracing()`
installs a process-global provider and is awkward to call twice.

### Verifying a push

`ENABLE_CONSOLE_SPAN_EXPORTER=true` separates "the app is not producing spans" from
"the collector is not accepting them". To confirm arrival, read the trace back — via
Grafana's datasource proxy if you have a token, so no direct route to the collector is
needed:

```bash
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  "$GRAFANA_URL/api/datasources/proxy/uid/$DATASOURCE_UID/api/traces/$TRACE_ID"
```

Backends index asynchronously; allow a few seconds before treating a 404 as a failure.

## Grafana deep links

`GRAFANA_URL` turns on the Loki/trace links attached to Sentry events. Which
datasources they open is configurable, because a shared Grafana does not name them
`loki` and `tempo`:

```bash
# Paste any Explore link with the trace datasource selected; the base URL, the
# datasource uid and its type are all read off it.
GRAFANA_DATASOURCE_URL='https://grafana.example.com/explore?schemaVersion=1&panes=...'

# Or set them outright.
GRAFANA_TRACE_DATASOURCE_UID=victoria-traces
GRAFANA_TRACE_DATASOURCE_TYPE=jaeger
GRAFANA_LOGS_DATASOURCE_UID=victoria-logs-loki
```

The **type** matters as much as the uid: Tempo wants `queryType: traceql`, while a
Jaeger-API backend (VictoriaTraces, Jaeger) looks a trace up by id with no `queryType`
at all. `trace_pane()` shapes the query from the type; sending the wrong one renders an
empty pane rather than an error.

## Local stack datasources

`grafana/provisioning/datasources/datasources.yaml` provisions Loki, Tempo, Prometheus
and Sentry (uid `sentry`, plugin `grafana-sentry-datasource`, installed via
`GF_INSTALL_PLUGINS`). Grafana expands `$VAR` in provisioning files from its own
environment, which is how the Sentry credentials get in — note the file's existing
warning that a *literal* `$` must be written `$$`.

Sentry is the one datasource that cannot work out of the box: it lives outside the
stack, so it needs `SENTRY_GRAFANA_URL` and `SENTRY_ORG_SLUG` in `grafana/.env`, plus
`SENTRY_AUTH_TOKEN` **exported in the shell** — `grafana/.env` is tracked, so a token
put there would be committed. The token is not the DSN: a DSN key only writes events;
reading them needs an auth token with `project:read` and `event:read`. From inside the
container, a Sentry on the host is `host.docker.internal`, never `localhost`.

The issues panel (id 40 in `wan.json`) uses the plugin's query model, whose shape is
easy to get wrong: `issuesQuery` is a plain **string**, alongside sibling keys
`issuesSort` / `issuesLimit` — not a nested object. Confirm against the plugin's own
`applyTemplateVariables` before changing it.

Two addresses for the same Sentry, deliberately: `SENTRY_GRAFANA_URL` is what the
container dials, `SENTRY_PUBLIC_URL` what a browser opens. On a self-hosted Sentry they
differ (`host.docker.internal` vs `localhost`).

The Tempo datasource carries a provisioned correlation on `traceID` that queries Sentry
for `traceID:<id>` — exact, because every event `wan` sends is tagged with it. Editing
that block is unusually hazardous: **both mistakes take Grafana down at startup**, they
do not skip the correlation.

- `type: external` panics 11.6 in `makeCreateCorrelationCommand`. Only `query`
  correlations can be provisioned; the HTTP API accepts `external`, provisioning does not.
- `config.type: query` must be explicit. Omitted, the Go zero value trips
  *"correlation contains non default value in config.type"* and the server exits.

Link to Sentry via the `Permalink` field the API returns, never a hand-built URL: a
self-hosted install need not use sentry.io's `/organizations/<org>/issues/` layout, and
every path answers `303` unauthenticated, so a wrong guess looks identical to a right one.

Searching Sentry for a trace is `traceID:<id>` — tag-qualified and case sensitive. A
bare id and `trace:<id>` both return nothing, so a wrong guess here also looks like an
empty result rather than an error. `Count` on a result row is the issue's lifetime
total, not that trace's: Sentry groups by fingerprint, so the query answers "which issue
contains this trace".

## Production behaviour

- **Multiple workers**: `patch()` warns when `WEB_CONCURRENCY > 1` and either
  `PROMETHEUS_MULTIPROC_DIR` is unset (per-worker registries make `/metrics`
  undercount by the worker count) or `LOG_FILE` is set (`RotatingFileHandler` races
  across processes). The check is `_multiworker_warnings()` — pure, tested directly.
  An explicit `--workers` flag is invisible to it; only the env var is detectable.
- **Readiness**: `/readyz` runs `patch(readiness_checks=[...])` (sync or async
  callables; raising or returning False → 503 naming the check). No env var — checks
  are callables. `/healthz` and `/livez` stay unconditional on purpose: liveness must
  not depend on downstreams.
- **Backpressure**: `BatchSpanProcessor` drops past a 2048-span queue. The standard
  `OTEL_BSP_*` env vars tune it and the SDK reads them directly — don't add wan knobs
  that shadow them. `OTEL_RESOURCE_ATTRIBUTES` also works (merged by
  `Resource.create`), and `OTEL_EXPORTER_OTLP(_TRACES)_ENDPOINT` / `_PROTOCOL` /
  `_HEADERS` are accepted as fallbacks behind wan's own `OTLP_*` names.
- **Correlation ids are attacker-controlled**: inbound values are truncated to
  `CORRELATION_ID_MAX_LENGTH` (default 128, 0 = no cap) in
  `RequestLoggingMiddleware.correlation_id()`. Truncate, never reject — a chain whose
  first hop minted an oversized id must still correlate from this hop on.
- **Releases are git tags** (`vX.Y.Z`) on main, with `__version__` in
  `wan/__init__.py` bumped in the same commit. The README tells consumers to pin the
  tag in `wan @ git+...@vX.Y.Z` form; an untagged repo makes that instruction a lie.

## Conventions

- Single quotes, 4-space indent, ~90 column lines, f-strings in log calls.
- Comments explain **why**, especially where a plausible alternative is wrong (the
  exporter and middleware ordering notes are the model to follow). Do not narrate what
  the code already says.
- Log-line key order is public API. `tests/test_logs.py::REFERENCE_REQUEST_KEYS` pins
  the prefix that `json_logging` produced; append new fields, never insert.
- Test names are sentences describing the behaviour being protected.
- Config goes through `os_env.py` and becomes a `patch()` keyword argument, so the same
  image is reconfigured without a code change. Do not read `os.environ` elsewhere.
- The repo-root `.env` holds live credentials and is gitignored. `grafana/.env` is
  **tracked**: ports and non-secret defaults only. Anything secret is exported in the
  shell or lives in the root `.env`.
