# Deployment and Security

## Container Image

Build and run the image from the repository root:

```bash
docker build -t overseer .
docker run --rm -p 8765:8765 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  overseer
```

The image runs Gunicorn on port `8765` with one worker and eight threads and exposes a Docker
health check through `GET /healthz`. The Python base image and direct
dependencies are pinned.

`.github/workflows/release.yml` publishes an image only when a GitHub Release
with a valid `vMAJOR.MINOR.PATCH` semantic-version tag is published. The
release reruns the complete CI workflow before publishing AMD64 and ARM64
images to `ghcr.io/<owner>/<repository>`. Stable releases receive
exact-version, major/minor, major, commit-SHA, and `latest` tags. Pre-releases
never update `latest`. Published images include SBOM and provenance metadata
and a GitHub artifact attestation.

Maintainers should follow the [release runbook](releasing.md) when publishing a
new version.

## Compose Project Detection

When Overseer runs as part of a Compose application, it reads the `com.docker.compose.project` label from its own container and lists other containers with the same label. The Overseer container itself is excluded. Set `OVERSEER_COMPOSE_PROJECT` to override automatic detection:

```yaml
services:
  overseer:
    image: ghcr.io/pykhaled/overseer:latest
    environment:
      OVERSEER_COMPOSE_PROJECT: my-application
```

When no project can be detected, Overseer lists every container visible through the configured Docker daemon.

## Operational Streams

The [Operations page](operations.md) requires resolved Compose scope. Keep the
single-worker production topology: multiple workers would split in-memory
history and duplicate notifications. Configure proxy timeouts of at least 30
seconds and disable buffering for SSE. Apply authentication to logs and events
as well as the existing inspection and control endpoints.

## Docker Socket Access

Access to `/var/run/docker.sock` is effectively administrative access to the Docker host. A process with socket access can inspect containers, mount host paths, and create privileged workloads.

- Deploy Overseer only on trusted networks.
- Put authentication and TLS at a trusted reverse proxy before allowing remote access.
- Do not expose Overseer's port directly to the public internet.
- Avoid mounting the socket into unrelated containers.
- Review changes to lifecycle routes as security-sensitive code.

Overseer has no built-in authentication. It rejects state-changing requests without its custom CSRF header and restricts lifecycle actions to the detected Compose project, but this does not eliminate the host-level risk of mounting the Docker socket. Treat it as a trusted-administrator interface.
