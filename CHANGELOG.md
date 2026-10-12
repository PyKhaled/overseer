# Changelog

All notable changes to Overseer are documented here. Releases follow
[Semantic Versioning](https://semver.org/).

## Unreleased

## [0.2.0] - 2026-10-12

This release consolidates the previous release numbering into the 0.2 series.
The previous GitHub releases have been retired; their changes are included here.

### Added

- Project dashboard with aggregate CPU and memory metrics, Compose dependency
  graph, and project-aware container lifecycle controls.
- Project Operations page with bounded recent/live logs, project-filtered Docker
  events, short-retention history, and successful Overseer action records.
- Configurable unhealthy, exit, restart-loop, CPU, and memory alerts with
  cooldowns and optional validated HTTPS webhook notifications.
- Sentinel Lens branding, responsive interfaces, and documentation.
- Health endpoint, automated tests, dependency auditing, CodeQL, and container
  vulnerability scanning.
- Multi-platform AMD64/ARM64 releases with SBOMs, provenance, and attestations.
- A canonical application version exposed by the UI and health endpoint, with
  release guards that require matching application and Git tag versions.

### Changed

- The application listens on port `8765`; migrate older `8000` port mappings,
  reverse-proxy targets, and health checks.
- Production runs one Gunicorn worker with eight threads to share operational
  history while allowing bounded live streams and regular requests.
- Project metrics use bounded concurrent collection rather than serial waits.
- Dependencies use pinned Python 3.14 and `pyproject.toml` dependency groups.
- Development uses Make commands for quality checks, serving, docs, and images.
- Integration targets existing Compose projects; the example stack is removed.

### Fixed

- Container action controls recover after failed requests and retain pending
  state across renders.
- Docker errors returned by operational endpoints omit internal exception text.

### Deployment notes

- Native authentication is not provided. Use a trusted authentication proxy and
  TLS; Docker socket access grants administrative control of the host.
- Operational APIs require a resolved Compose project. History is bounded and
  in memory, and webhook delivery is best-effort.

[0.2.0]: https://github.com/PyKhaled/overseer/releases/tag/v0.2.0
