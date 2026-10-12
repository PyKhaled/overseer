# HTTP API

The API has no built-in authentication. Run it only inside a trusted Compose project on a trusted network.

## Dashboard

### `GET /`

Returns the project overview with CPU, memory, and Compose metadata.

### `GET /dependencies`

Returns the service dependency graph page.

### `GET /services`

Returns the service inspection and lifecycle-controls page.

## Health

### `GET /healthz`

Returns `{"status": "ok"}` without contacting Docker. The container health
check and deployment smoke tests use this endpoint.

## Project Metrics

### `GET /api/dashboard`

Returns project metadata, aggregate CPU and memory usage, and resource usage grouped by Compose service. CPU and memory values come from one-shot Docker stats for running containers; stopped containers do not contribute resource metrics. Sampling uses a fixed pool of four workers per application process, three-second Docker request timeouts, and an eight-second collection budget. Slow or failed samples are omitted from resource totals. `resources.measured_containers` and each service's `measured_containers` report how many running containers contributed; compare these with the corresponding running count to detect partial totals. CPU sampling retains Docker's two-cycle behavior. The collection budget covers stats sampling, not preceding container discovery and inspection.

```json
{
  "project": {
    "name": "example-project",
    "compose_scoped": true,
    "services": 3,
    "containers": 3,
    "images": 3,
    "running": 2,
    "restarting": 0,
    "stopped": 1,
    "healthy": 2,
    "unhealthy": 0
  },
  "resources": {
    "metrics_available": true,
    "cpu_percent": 4.25,
    "memory_usage": 157286400,
    "memory_limit": 2147483648,
    "memory_percent": 7.32
  },
  "services": [],
  "updated_at": "2026-08-18T00:00:00+00:00"
}
```

## List Containers

### `GET /api/services`

Returns containers in Overseer's Docker Compose project, excluding the Overseer container itself. Overseer detects the project from its own container label or the `OVERSEER_COMPOSE_PROJECT` environment variable. When neither is available, such as during local development outside Compose, it falls back to every other container visible to the configured Docker daemon.

```json
[
  {
    "id": "1234567890ab",
    "name": "web",
    "service": "web",
    "dependencies": ["database", "redis"],
    "status": "running",
    "image": "example/web:latest",
    "ports": {
      "80/tcp": [
        {"host_ip": "0.0.0.0", "host_port": "8080"}
      ]
    },
    "started_at": "2026-08-10T10:00:00+00:00",
    "uptime": "1:05:12.345678"
  }
]
```

Exposed but unpublished ports have a `null` value.

`service` comes from the `com.docker.compose.service` label. `dependencies` contains service names parsed from Compose's declared `depends_on` metadata. Overseer does not infer dependencies from shared networks or observed traffic.

## Container Actions

The following endpoints accept `POST` requests:

- `/api/service/<container_id>/start`
- `/api/service/<container_id>/stop`
- `/api/service/<container_id>/restart`

Action requests must include the following CSRF-protection header:

```text
X-Overseer-CSRF: 1
```

```bash
curl --header "X-Overseer-CSRF: 1" \
  --request POST \
  http://localhost:8765/api/service/container-id/restart
```

On success, each returns:

```json
{"success": true}
```

`container_id` may be any identifier accepted by the Docker SDK, including a container ID or name. Missing containers return HTTP `404`; other Docker daemon failures return HTTP `502`. API errors use the following JSON shape:

```json
{"error": "Docker operation failed"}
```

## Operational Visibility

All operational APIs require a resolved Compose project and fail with HTTP 503
when scope is unavailable. They use the same trusted-network/reverse-proxy
authentication boundary as the inspection API. See [Project Operations](operations.md)
for retention, alerts, stream limits, webhook validation, and deployment details.

### `GET /operations`

Returns the event history and container logs page.

### `GET /api/history?limit=200&after=0`

Returns `{project, instance, status, records}`. The `instance` identifier changes
on process restart; reset the record cursor when it changes. Records have a monotonic process-local
`id`, `timestamp`, `project`, `source` (`docker`, `overseer`, or `alert`),
`kind`, `container_id`, `service`, and a sanitized `message`. `after` filters
by record ID. `limit` cannot exceed configured history capacity.

### `GET /api/events?after=0`

Returns Server-Sent Events: `status`, `record`, and `end`, with heartbeat
comments. Record IDs support the `Last-Event-ID` header; an explicit `after`
query takes precedence. On `end`, close the connection and reconnect using the
last record ID. Stream capacity exhaustion returns HTTP 429. History is
bounded, so a cursor older than retained records cannot guarantee gapless replay.

### `GET /api/service/<container_id>/logs`

Returns sanitized UTF-8 plain text for the selected project container.
Parameters: `tail` (0 through configured maximum), `timestamps` (0 or 1,
default 1), `since` (nonnegative Unix seconds), `download=1` (attachment), and
`follow=1` (SSE instead of plain text). Downloads cannot follow a live stream.
Live SSE uses `log` events with `{text, truncated}`, `unavailable` on Docker
connection failure, and `end` on normal lease/cap completion. Reconnect with a
`since` cursor; Docker's inclusive timestamps may replay lines. Output bytes,
individual lines, connection duration, and simultaneous live streams are
bounded. HTTP 404 hides containers outside the active project; invalid query
parameters return HTTP 400. No logs are retained server-side.
