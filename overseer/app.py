import os
import socket
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

import docker
import requests
from flask import Flask, jsonify, render_template, request

from .operational_api import register_operations
from .version import __version__

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_DEPENDS_ON_LABEL = "com.docker.compose.depends_on"
CSRF_HEADER = "X-Overseer-CSRF"
CSRF_HEADER_VALUE = "1"
METRICS_BUDGET_SECONDS = 8
METRICS_REQUEST_TIMEOUT = 3
# Shared per Gunicorn worker: repeated requests cannot create unbounded threads.
METRICS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stats")


def unavailable_metrics():
    return {
        "cpu_percent": None,
        "memory_usage": None,
        "memory_limit": None,
        "memory_percent": None,
    }


def get_client(timeout=None):
    return docker.from_env() if timeout is None else docker.from_env(timeout=timeout)


def get_ports(container):
    ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
    result = {}
    for container_port, mappings in ports.items():
        if mappings:
            result[container_port] = [
                {
                    "host_ip": mapping.get("HostIp", ""),
                    "host_port": mapping.get("HostPort", ""),
                }
                for mapping in mappings
            ]
        else:
            result[container_port] = None
    return result


def get_compose_dependencies(container):
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    dependency_value = labels.get(COMPOSE_DEPENDS_ON_LABEL, "")
    dependencies = []
    for dependency in dependency_value.split(","):
        service_name = dependency.split(":", 1)[0].strip()
        if service_name and service_name not in dependencies:
            dependencies.append(service_name)
    return dependencies


def calculate_cpu_percent(stats):
    cpu_stats = stats.get("cpu_stats") or {}
    previous_cpu_stats = stats.get("precpu_stats") or {}
    cpu_usage = cpu_stats.get("cpu_usage") or {}
    previous_cpu_usage = previous_cpu_stats.get("cpu_usage") or {}
    cpu_delta = cpu_usage.get("total_usage", 0) - previous_cpu_usage.get(
        "total_usage",
        0,
    )
    system_delta = cpu_stats.get("system_cpu_usage", 0) - (
        previous_cpu_stats.get("system_cpu_usage", 0)
    )
    online_cpus = (
        cpu_stats.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or 1
    )
    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    return round((cpu_delta / system_delta) * online_cpus * 100, 2)


def get_memory_metrics(stats):
    memory_stats = stats.get("memory_stats") or {}
    raw_usage = memory_stats.get("usage", 0)
    memory_details = memory_stats.get("stats") or {}
    cache = memory_details.get(
        "inactive_file",
        memory_details.get("total_inactive_file", 0),
    )
    usage = max(0, raw_usage - cache)
    limit = memory_stats.get("limit", 0)
    percent = (usage / limit) * 100 if limit else 0.0
    return {
        "usage": usage,
        "limit": limit,
        "percent": round(percent, 2),
    }


def inspect_container_metrics(container, stats_client=None):
    if container.status != "running":
        return unavailable_metrics()

    try:
        if stats_client is None:
            stats = container.stats(stream=False)
        else:
            stats = stats_client.api.stats(container.id, stream=False)
    except (docker.errors.DockerException, requests.exceptions.RequestException):
        return unavailable_metrics()

    memory = get_memory_metrics(stats)
    return {
        "cpu_percent": calculate_cpu_percent(stats),
        "memory_usage": memory["usage"],
        "memory_limit": memory["limit"],
        "memory_percent": memory["percent"],
    }


def sample_container_metrics(container, api_version):
    # Independent sessions keep worker requests isolated. Reuse the negotiated
    # API version to avoid another daemon handshake for every sample.
    stats_client = None
    try:
        stats_client = docker.from_env(
            timeout=METRICS_REQUEST_TIMEOUT, version=api_version
        )
        return inspect_container_metrics(container, stats_client)
    except (docker.errors.DockerException, requests.exceptions.RequestException):
        return unavailable_metrics()
    finally:
        if stats_client is not None:
            stats_client.close()


def collect_project_metrics(client, containers, timeout=METRICS_BUDGET_SECONDS):
    metrics = {container.id: unavailable_metrics() for container in containers}
    futures = {
        METRICS_EXECUTOR.submit(
            sample_container_metrics, container, client.api.api_version
        ): container.id
        for container in containers
        if container.status == "running"
    }
    if not futures:
        return metrics
    completed, pending = wait(futures, timeout=timeout)
    for future in pending:
        future.cancel()
    for future in completed:
        metrics[futures[future]] = future.result()
    # Do not wait for already-running requests beyond the collection budget.
    # Their sessions close when their bounded Docker request finishes.
    return metrics


def parse_docker_datetime(value):
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_started_at(container):
    started_at = parse_docker_datetime(
        container.attrs.get("State", {}).get("StartedAt")
    )
    return started_at.isoformat() if started_at else None


def get_uptime(container):
    state = container.attrs.get("State", {})
    started = parse_docker_datetime(state.get("StartedAt"))
    if not started:
        return None

    finished = parse_docker_datetime(state.get("FinishedAt"))
    end = finished if finished and finished >= started else datetime.now(timezone.utc)
    return str(end - started)


def get_overseer_container(client):
    container_identifier = os.getenv("HOSTNAME") or socket.gethostname()
    try:
        return client.containers.get(container_identifier)
    except docker.errors.NotFound:
        return None


def get_compose_project(client, overseer_container=None):
    configured_project = os.getenv("OVERSEER_COMPOSE_PROJECT")
    if configured_project:
        return configured_project

    if overseer_container is None:
        overseer_container = get_overseer_container(client)
    if overseer_container is None:
        return None

    return (overseer_container.labels or {}).get(COMPOSE_PROJECT_LABEL)


def list_project_containers(client):
    overseer_container = get_overseer_container(client)
    project = get_compose_project(client, overseer_container)
    filters = {"label": f"{COMPOSE_PROJECT_LABEL}={project}"} if project else None
    list_options = {"all": True, "ignore_removed": True}
    if filters:
        list_options["filters"] = filters
    containers = client.containers.list(**list_options)
    if overseer_container is None:
        return containers
    return [
        container for container in containers if container.id != overseer_container.id
    ]


def get_project_container(client, container_id):
    container = client.containers.get(container_id)
    project = get_compose_project(client)
    container_project = (container.labels or {}).get(COMPOSE_PROJECT_LABEL)
    if project and container_project != project:
        raise docker.errors.NotFound("Container not found in Overseer project")
    return container


def inspect_container(container):
    config = container.attrs.get("Config", {})
    labels = config.get("Labels") or {}
    health = container.attrs.get("State", {}).get("Health", {}).get("Status")
    image = config.get("Image")
    if not image:
        image = container.attrs.get("Image", "unknown")

    return {
        "id": container.id[:12],
        "name": container.name,
        "service": labels.get(COMPOSE_SERVICE_LABEL, container.name),
        "dependencies": get_compose_dependencies(container),
        "status": container.status,
        "health": health or "not-configured",
        "image": image,
        "ports": get_ports(container),
        "started_at": get_started_at(container),
        "uptime": get_uptime(container),
    }


def build_project_dashboard(client):
    containers = list_project_containers(client)
    project_name = get_compose_project(client)
    container_metrics = collect_project_metrics(client, containers)
    service_metrics = {}
    images = set()
    running = 0
    restarting = 0
    healthy = 0
    unhealthy = 0

    for container in containers:
        details = inspect_container(container)
        metrics = container_metrics[container.id]
        service_name = details["service"]
        service = service_metrics.setdefault(
            service_name,
            {
                "name": service_name,
                "containers": 0,
                "running": 0,
                "cpu_percent": 0.0,
                "memory_usage": 0,
                "memory_limit": 0,
                "metrics_available": False,
                "measured_containers": 0,
            },
        )
        service["containers"] += 1
        if container.status == "running":
            running += 1
            service["running"] += 1
        elif container.status == "restarting":
            restarting += 1

        health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        if health == "healthy":
            healthy += 1
        elif health == "unhealthy":
            unhealthy += 1

        images.add(details["image"])
        if metrics["cpu_percent"] is not None:
            service["metrics_available"] = True
            service["measured_containers"] += 1
            service["cpu_percent"] += metrics["cpu_percent"]
            service["memory_usage"] += metrics["memory_usage"]
            service["memory_limit"] += metrics["memory_limit"]

    total_cpu = 0.0
    total_memory = 0
    total_memory_limit = 0
    metrics_available = False
    services = []
    for service in sorted(service_metrics.values(), key=lambda item: item["name"]):
        service["cpu_percent"] = round(service["cpu_percent"], 2)
        service["memory_percent"] = (
            round(
                (service["memory_usage"] / service["memory_limit"]) * 100,
                2,
            )
            if service["memory_limit"]
            else 0.0
        )
        total_cpu += service["cpu_percent"]
        total_memory += service["memory_usage"]
        total_memory_limit += service["memory_limit"]
        metrics_available = metrics_available or service["metrics_available"]
        services.append(service)

    container_count = len(containers)
    return {
        "project": {
            "name": project_name or "Docker host",
            "compose_scoped": project_name is not None,
            "services": len(service_metrics),
            "containers": container_count,
            "images": len(images),
            "running": running,
            "restarting": restarting,
            "stopped": container_count - running - restarting,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "health_unreported": container_count - healthy - unhealthy,
        },
        "resources": {
            "metrics_available": metrics_available,
            "measured_containers": sum(s["measured_containers"] for s in services),
            "cpu_percent": round(total_cpu, 2),
            "memory_usage": total_memory,
            "memory_limit": total_memory_limit,
            "memory_percent": round(
                (total_memory / total_memory_limit) * 100,
                2,
            )
            if total_memory_limit
            else 0.0,
        },
        "services": services,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def create_app(config=None):
    application = Flask(__name__, template_folder="templates")
    if config:
        application.config.update(config)

    monitor = register_operations(
        application,
        lambda: get_client(timeout=6),
        lambda client: get_compose_project(client),
        lambda client: build_project_dashboard(client),
    )

    @application.before_request
    def protect_state_changing_requests():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied_csrf_header = request.headers.get(CSRF_HEADER, "")
            if supplied_csrf_header != CSRF_HEADER_VALUE:
                return jsonify({"error": "CSRF validation failed"}), 403

    @application.after_request
    def add_security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'none'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @application.context_processor
    def application_version():
        return {"overseer_version": __version__}

    @application.route("/")
    def index():
        return render_template("dashboard.html")

    @application.route("/dependencies")
    def dependencies():
        return render_template("index.html")

    @application.route("/services")
    def service_controls():
        return render_template("services.html")

    @application.route("/healthz")
    def health():
        return {"status": "ok", "version": __version__}

    @application.route("/api/services")
    def services():
        containers = list_project_containers(get_client())
        return jsonify([inspect_container(container) for container in containers])

    @application.route("/api/dashboard")
    def dashboard():
        return jsonify(build_project_dashboard(get_client()))

    @application.route("/api/service/<container_id>/restart", methods=["POST"])
    def restart_service(container_id):
        client = get_client()
        container = get_project_container(client, container_id)
        container.restart()
        project = get_compose_project(client)
        if isinstance(project, str) and project:
            monitor.record(
                project,
                "overseer",
                "restart",
                container.id,
                (container.labels or {}).get(COMPOSE_SERVICE_LABEL, container.name),
                "Restart requested through Overseer",
            )
        return {"success": True}

    @application.route("/api/service/<container_id>/stop", methods=["POST"])
    def stop_service(container_id):
        client = get_client()
        container = get_project_container(client, container_id)
        container.stop()
        project = get_compose_project(client)
        if isinstance(project, str) and project:
            monitor.record(
                project,
                "overseer",
                "stop",
                container.id,
                (container.labels or {}).get(COMPOSE_SERVICE_LABEL, container.name),
                "Stop requested through Overseer",
            )
        return {"success": True}

    @application.route("/api/service/<container_id>/start", methods=["POST"])
    def start_service(container_id):
        client = get_client()
        container = get_project_container(client, container_id)
        container.start()
        project = get_compose_project(client)
        if isinstance(project, str) and project:
            monitor.record(
                project,
                "overseer",
                "start",
                container.id,
                (container.labels or {}).get(COMPOSE_SERVICE_LABEL, container.name),
                "Start requested through Overseer",
            )
        return {"success": True}

    @application.errorhandler(docker.errors.NotFound)
    def handle_container_not_found(error):
        application.logger.warning("Docker resource not found: %s", error)
        return jsonify({"error": "Container not found"}), 404

    @application.errorhandler(docker.errors.DockerException)
    def handle_docker_error(error):
        application.logger.error("Docker operation failed: %s", error)
        return jsonify({"error": "Docker operation failed"}), 502

    return application


app = create_app()
