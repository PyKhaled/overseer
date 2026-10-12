# Project Operations

Open `/operations` to view recent Docker transitions, actions initiated through
Overseer, alerts, and application logs. These are separate sources: log contents
are never copied into history or webhook notifications.

## Project Boundary

The operational APIs require a configured or automatically detected Compose
project. They return HTTP 503 when no project can be resolved, even when the
existing dashboard intentionally uses host-wide discovery. Set
`OVERSEER_COMPOSE_PROJECT` when running Overseer outside Compose. Each log
request checks the target's current project label before opening its stream.
Docker event filters are applied at the daemon and checked again on receipt.

These APIs have no native authentication, like the existing inspection API.
Put **all** Overseer routes behind the same trusted authentication proxy and
TLS boundary. Logs can contain credentials and personal information; access to
them should be restricted at least as tightly as container inspection.

## History and Events

A background collector starts on the first API request. It records lifecycle
and health transitions and reconnects automatically when Docker becomes
unavailable. Reconnects replay from the last observed second and deduplicate
recent event identities. History is an in-memory ring with a default maximum
of 1,000 records and a one-hour retention period. History starts when Overseer
connects and is cleared on process restart; it is not a durable audit trail.

Overseer records successful start, stop, and restart requests separately from
Docker transitions. It does not invent events for failed requests. The page
can filter Docker events, Overseer actions, or alerts and pause/resume updates.
The browser displays the latest 200 records. History lost through retention or
capacity limits cannot be recovered by reconnecting a browser.

The production command uses **one Gunicorn worker with eight threads**. One
worker owns the shared collector and history; multiple workers would create
separate histories and duplicate webhook notifications. Up to four live event
or log streams are allowed together, leaving threads available for ordinary
requests. SSE leases end after 25 seconds and reconnect automatically. Event
streams send heartbeats; the proxy should disable buffering and permit idle
connections for at least 30 seconds. `X-Accel-Buffering: no` is included.

## Logs

Select a container for recent logs. Tail count, timestamp visibility, text
search, stream pause/resume, and bounded downloads are available. Search
filters the retained browser output only; it does not query historical logs
on Docker. Downloads request the current bounded tail from Docker.

Defaults are 200 tail lines, 256 KiB per response/browser buffer, and 4 KiB per
line. Terminal escapes and control characters are removed; HTML is rendered
as text. Large output is truncated. Live connections use bounded Docker
requests and reconnect with an inclusive timestamp cursor. The browser
suppresses replayed timestamp/content pairs. Identical lines with identical
timestamps can be collapsed, and Docker history that disappears during a
long disconnect cannot be recovered. Docker must use a log driver that
supports reading logs.

## Alerts and Webhooks

Alerts appear in local history without external configuration:

- `unhealthy`: a Docker health transition becomes unhealthy.
- `exit`: a container exits, including a normal or operator-requested exit.
- `restart-loop`: at least three starts of one container occur within 60 seconds.
  Counting starts also detects loops caused by Docker restart policies.
- `memory`: sampled service memory usage reaches 90% of measured memory limits.
- `cpu`: disabled by default; enable by configuring a positive threshold.
  100% represents one fully utilized core, so thresholds may exceed 100%.

Resource alerts use current sampled service totals and can be partial when
some containers cannot be measured. Sampling runs every 30 seconds. Repeated
alerts of the same kind and target are suppressed for 300 seconds. Event
alerts depend on transitions observed since collection started; this version
does not backfill an already-unhealthy container from its current state.

Set `OVERSEER_ALERT_WEBHOOK_URL` to enable JSON POST notifications, optionally
with `OVERSEER_ALERT_WEBHOOK_TOKEN` for a Bearer token. Keep credentials in
secret environment configuration. Notifications contain the sanitized alert
record, never application logs or arbitrary Docker attributes.

Webhook destinations must use HTTPS port 443 without URL credentials or
fragments. Every delivery resolves DNS and rejects loopback, private,
link-local, reserved, or multicast addresses, including mixed public/private
answers. The connection pins a validated address while verifying the original
TLS hostname. Redirects are rejected. Connection/read timeouts are three
seconds; DNS resolution uses the platform resolver. A separate 64-entry queue
keeps delivery off request/collector threads. Queue overflow and failed
notification attempts are recorded without exposing destination secrets.
There are no automatic delivery retries; this is lightweight notification,
not guaranteed delivery.

## Configuration

Configuration is read from environment variables at process startup. Invalid
values fail startup rather than silently removing limits.

| Variable | Default | Accepted range |
| --- | --- | --- |
| `OVERSEER_HISTORY_SIZE` | 1000 | 1–10000 records |
| `OVERSEER_HISTORY_SECONDS` | 3600 | 1–86400 seconds |
| `OVERSEER_LOG_TAIL` | 200 | 1–5000 lines |
| `OVERSEER_LOG_BYTES` | 262144 | 1024–1048576 bytes |
| `OVERSEER_LINE_BYTES` | 4096 | 256–16384 bytes |
| `OVERSEER_STREAM_SECONDS` | 25 | 5–25 seconds |
| `OVERSEER_MAX_STREAMS` | 4 | 1–4 simultaneous streams |
| `OVERSEER_ALERT_COOLDOWN` | 300 | 1–86400 seconds |
| `OVERSEER_RESTART_WINDOW` | 60 | 1–3600 seconds |
| `OVERSEER_RESTART_COUNT` | 3 | 2–100 starts |
| `OVERSEER_METRICS_INTERVAL` | 30 | 10–3600 seconds |
| `OVERSEER_CPU_THRESHOLD` | 0 | 0 disables; up to 100000 percent |
| `OVERSEER_MEMORY_THRESHOLD` | 90 | 0 disables; up to 100 percent |
| `OVERSEER_ALERT_WEBHOOK_URL` | unset | Public HTTPS destination |
| `OVERSEER_ALERT_WEBHOOK_TOKEN` | unset | Optional Bearer token |

Persistence, long-term telemetry, durable delivery, and native authentication
remain separate features.
