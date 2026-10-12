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
