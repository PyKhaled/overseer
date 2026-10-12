# Overseer

Overseer is a lightweight, project-centric observability and control dashboard
for Docker Compose applications. It discovers the containers in one Compose
project, displays their status and resource usage, renders declared service
dependencies, and provides container lifecycle controls.

## Quick Start

Add Overseer to an existing Compose file:

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

Start the project and open `http://localhost:8765`:

```bash
docker compose up -d
```

!!! warning "Protect access to Overseer"

    Overseer has no built-in authentication, and access to the Docker socket is
    effectively administrative access to the Docker host. Run it only on a
    trusted network and put authentication and TLS at a trusted reverse proxy
    before allowing remote access.

## Documentation

- [Architecture](architecture.md) explains the package, request flow, and
  current design boundaries.
- [Development](development.md) covers environment setup, running the app,
  tests, and documentation development.
- [Deployment and Security](deployment.md) describes the production image,
  Compose project detection, and Docker socket risks.
- [HTTP API](api.md) documents dashboard and container-control endpoints.
- [Releasing](releasing.md) is the maintainer runbook for publishing a GitHub
  Release and its GHCR image.

For release history, see the repository
[changelog](https://github.com/PyKhaled/Overseer/blob/main/CHANGELOG.md). The
[project repository](https://github.com/PyKhaled/Overseer) contains the source,
issue tracker, and contribution guidelines.
