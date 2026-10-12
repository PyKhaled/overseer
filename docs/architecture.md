# Architecture

## Components

The `overseer` Python package contains the application:

- `overseer/__init__.py` exports the Flask application and `create_app()` factory.
- `overseer/__main__.py` provides the `python -m overseer` development entry point.
- `overseer/app.py` defines routes and translates Docker SDK objects and Compose labels into JSON-safe service metadata.
- `overseer/operations.py` owns the bounded event collector, alert rules, cooldowns, and optional webhook queue.
- `overseer/operational_api.py` provides project-scoped history and log APIs with SSE leases and stream limits.
- `overseer/templates/operations.html` and `overseer/static/operations.js` provide the operational timeline and log viewer.
- `overseer/templates/base.html` provides the shared dashboard layout and navigation.
- `overseer/templates/dashboard.html` polls project metadata and one-shot container resource metrics every ten seconds.
- `overseer/templates/index.html` serves `/dependencies`, polls the services endpoint every five seconds, and renders declared dependencies as an SVG graph.
- `overseer/templates/services.html` polls the same endpoint to render container details and lifecycle controls.

Gunicorn imports `overseer:app` in the production container. Flask renders the dashboard, while the Docker SDK connects using `docker.from_env()`—normally through a mounted Unix socket.

## Request Flow

1. The browser requests `/` for the project overview, `/dependencies` for the graph, or `/services` for service controls.
2. The overview requests `GET /api/dashboard`; the other pages request `GET /api/services`.
3. Overseer detects its Compose project, queries matching containers, removes itself from the result, and serializes project metadata or container details. The overview additionally obtains one-shot CPU and memory stats for running containers.
4. A lifecycle button sends a CSRF-protected `POST` request; Overseer verifies that the target belongs to the detected Compose project before invoking the Docker SDK operation.

## Current Boundaries

The current implementation filters by the `com.docker.compose.project` label when it can detect a project from its own container or from `OVERSEER_COMPOSE_PROJECT`. It falls back to all visible containers for local, non-Compose development. Dependency edges come from the `com.docker.compose.depends_on` label; shared networks alone are not treated as dependencies. A custom request header protects lifecycle actions from cross-site form submissions, but the application does not authenticate users. Operational streams require a resolved project. Production uses one Gunicorn worker with eight threads so history and notifications have one owner. See [Project Operations](operations.md) for limits and deployment requirements. Keep Docker-specific behavior in helper functions so routes remain straightforward to test.
