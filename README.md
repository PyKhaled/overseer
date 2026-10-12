# Overseer

Current release: [v0.2.0](https://github.com/PyKhaled/overseer/releases/tag/v0.2.0).

[![Continuous Integration](https://github.com/pykhaled/overseer/actions/workflows/ci.yml/badge.svg)](https://github.com/pykhaled/overseer/actions/workflows/ci.yml)
[![Documentation](https://github.com/pykhaled/overseer/actions/workflows/docs.yml/badge.svg)](https://github.com/pykhaled/overseer/actions/workflows/docs.yml)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="overseer/static/images/overseer-sentinel-logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="overseer/static/images/overseer-sentinel-logo-light.svg">
  <img alt="Overseer" src="overseer/static/images/overseer-sentinel-logo-light.svg" width="520">
</picture>

Overseer is a lightweight project observability and control service for Docker Compose applications.

Drop Overseer into your existing Docker Compose stack and instantly gain visibility into the services that make up your project.

Unlike container management tools that focus on the entire Docker host, Overseer focuses on a single application stack and provides a project-centric view of your services.

---

## Features

### Service Discovery

Automatically discovers services running under the same Docker Compose project.

### Service Control

- Start services
- Stop services
- Restart services

### Service Visibility

View:

- Running services
- Stopped services
- Container status
- Images
- Container identifiers
- Declared service dependencies as an interactive graph
- Project-level and per-service CPU and memory usage
- Project-scoped live and recent logs, bounded event history, and configurable alerts

### Project-Oriented

Overseer groups services by Docker Compose project, making it easier to understand and manage a complete application stack.

---

## Quick Start

Add Overseer to your existing `docker-compose.yml`.

```yaml
services:
  overseer:
    image: ghcr.io/pykhaled/overseer:0.2.0
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

Start your stack:

```bash
docker compose up -d
```

Open:

```text
http://localhost:8765
```

Use the dashboard navigation to switch between the project overview at `/`, dependency graph at `/dependencies`, and container controls at `/services`. Selecting a service in the graph opens its matching control card. Use `/operations` for Docker events, operator actions, alerts, and bounded live logs. Operational APIs require a resolved Compose project.

---

## Example

```yaml
services:
  overseer:
    image: ghcr.io/pykhaled/overseer:0.2.0

  api-gateway:
    image: my-api-gateway
    depends_on:
      - service-a
      - service-b

  service-a:
    image: my-service-a
    depends_on:
      - service-a-db

  service-a-db:
    image: postgres

  service-b:
    image: my-service-b
    depends_on:
      - service-b-db
      - redis

  service-b-db:
    image: postgres

  redis:
    image: redis
```

Overseer automatically discovers the services belonging to the project and uses declared `depends_on` relationships to render the architecture graph.

---

## Why Overseer?

Most Docker tools are host-centric.

Overseer is project-centric.

Instead of showing every container on the machine, Overseer helps you understand:

- What services belong to this application
- Which services are healthy
- Which services are consuming resources
- How services relate to one another

---

## Development

The production image uses Python 3.14, and CI tests Python 3.11 through 3.14.
Dependencies and development-tool settings are kept in `pyproject.toml`. For
local development, create a virtual environment with the provided Make target.
Set `PYTHON` to any supported Python 3.11–3.14 interpreter available on your
system:

```bash
make setup PYTHON=python3.14
```

Setuptools is removed after installation because the application does not need
it at runtime and the newest available release is affected by a known
vulnerability. Make targets use `.venv` directly, so activating it is optional.
Run `make help` to list all available commands.

For a production-only environment, install the smaller runtime group instead:

```bash
python -m pip install --group runtime
```

Run the Flask development server at `http://localhost:8765`:

```bash
make run
```

Run the automated test suite. Tests mock the Docker client and do not modify real containers:

```bash
make test
```

Run the complete local quality gate before opening a pull request:

```bash
make check
```

Run the production server locally with Gunicorn:

```bash
make serve
```

Build and smoke-test the production container image:

```bash
make image
make smoke
```

Read the [Overseer documentation](https://pykhaled.github.io/Overseer/) for
architecture, API, development, releasing, and deployment details. Its Markdown
source is maintained in the [`docs/`](docs/index.md) directory.

---

## Roadmap

Planned features include:

- Project metadata
- Multi-host support
- User authentication

---

## Security

Overseer requires access to the Docker socket:

```text
/var/run/docker.sock
```

This allows Overseer to inspect and manage Docker containers on the host.

Overseer has no built-in authentication and rejects lifecycle actions that do not include its CSRF header. Deploy it only as part of a trusted Compose project on a trusted network.

Report vulnerabilities privately by following [`SECURITY.md`](SECURITY.md).

---

## License

MIT
