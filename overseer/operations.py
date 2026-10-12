"""Bounded operational history, alert evaluation, and safe webhook delivery."""

import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import docker
import requests

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
DOCKER_FAILURES = (docker.errors.DockerException, requests.exceptions.RequestException)


def clean_text(value, limit=512):
    # Strip terminal escapes and control characters before API/UI/export use.
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "".join(
        c for c in value if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159
    )[:limit]


@dataclass(frozen=True)
class OperationsSettings:
    history_size: int = 1000
    history_seconds: int = 3600
    log_tail: int = 200
    log_bytes: int = 262144
    line_bytes: int = 4096
    stream_seconds: int = 25
    max_streams: int = 4
    alert_cooldown: int = 300
    restart_window: int = 60
    restart_count: int = 3
    metrics_interval: int = 30
    cpu_threshold: int = 0
    memory_threshold: int = 90
    webhook_url: str = ""
    webhook_token: str = ""

    @classmethod
    def from_config(cls, config):
        limits = {
            "history_size": (1, 10000),
            "history_seconds": (1, 86400),
            "log_tail": (1, 5000),
            "log_bytes": (1024, 1048576),
            "line_bytes": (256, 16384),
            "stream_seconds": (5, 25),
            "max_streams": (1, 4),
            "alert_cooldown": (1, 86400),
            "restart_window": (1, 3600),
            "restart_count": (2, 100),
            "metrics_interval": (10, 3600),
            "cpu_threshold": (0, 100000),
            "memory_threshold": (0, 100),
        }
        values = {}
        defaults = cls()
        for name, (low, high) in limits.items():
            key = "OVERSEER_" + name.upper()
            value = int(config.get(key, os.getenv(key, getattr(defaults, name))))
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            values[name] = value
        for name in ("webhook_url", "webhook_token"):
            key = "OVERSEER_ALERT_" + name.upper()
            values[name] = config.get(key, os.getenv(key, ""))
        if values["webhook_url"]:
            validate_webhook_url(values["webhook_url"])
        return cls(**values)


def validate_webhook_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Webhook must be an HTTPS URL on port 443 without credentials")
    return parsed


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=3, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw_socket = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def send_webhook(url, token, payload):
    parsed = validate_webhook_url(url)
    addresses = {
        entry[4][0]
        for entry in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(
        not ipaddress.ip_address(address).is_global
        or ipaddress.ip_address(address).is_multicast
        for address in addresses
    ):
        raise ValueError("Webhook destination must resolve only to public addresses")
    # Pin the validated address while preserving TLS hostname verification.
    connection = PinnedHTTPSConnection(parsed.hostname, sorted(addresses)[0])
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request("POST", target, json.dumps(payload).encode(), headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ValueError("Webhook returned an unsuccessful response")
        # No redirects, response-body reads, or automatic retries.
    finally:
        connection.close()


class OperationsMonitor:
    def __init__(self, settings, client_factory, project_resolver, metrics_reader):
        self.settings = settings
        self.client_factory = client_factory
        self.project_resolver = project_resolver
        self.metrics_reader = metrics_reader
        self.condition = threading.Condition()
        self.history = deque(maxlen=settings.history_size)
        self.sequence = 0
        self.instance = uuid4().hex
        self.cooldowns = {}
        self.restarts = {}
        self.notifications = queue.Queue(maxsize=64)
        self.stream_slots = threading.BoundedSemaphore(settings.max_streams)
        self.stop_event = threading.Event()
        self.thread = None
        self.project = None
        self.status = "not-started"
        self.last_event_second = None
        self.event_keys = deque(maxlen=settings.history_size)
        self.last_metrics = 0

    def start(self):
        with self.condition:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self.run, daemon=True, name="operations"
            )
            self.thread.start()
            if self.settings.webhook_url:
                threading.Thread(target=self.notify, daemon=True, name="alerts").start()

    def prune(self, now):
        while (
            self.history
            and self.history[0]["epoch"] < now - self.settings.history_seconds
        ):
            self.history.popleft()
        self.cooldowns = {
            k: v
            for k, v in self.cooldowns.items()
            if v > now - self.settings.alert_cooldown
        }
        self.restarts = {
            k: deque(t for t in times if t > now - self.settings.restart_window)
            for k, times in self.restarts.items()
            if times and times[-1] > now - self.settings.restart_window
        }

    def record(self, project, source, kind, container_id="", service="", message=""):
        now = time.time()
        with self.condition:
            self.prune(now)
            self.sequence += 1
            record = {
                "id": self.sequence,
                "epoch": now,
                "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "project": clean_text(project),
                "source": source,
                "kind": kind,
                "container_id": clean_text(container_id, 64),
                "service": clean_text(service, 128),
                "message": clean_text(message),
            }
            self.history.append(record)
            self.condition.notify_all()
            return record

    def snapshot(self, project, after=0, limit=200):
        with self.condition:
            self.prune(time.time())
            return [
                dict(record)
                for record in self.history
                if record["project"] == project and record["id"] > after
            ][-limit:]

    def alert(self, project, kind, container_id, service, message):
        key = (project, kind, container_id)
        now = time.time()
        with self.condition:
            self.prune(now)
            if key in self.cooldowns:
                return None
            # Bound auxiliary state as well as the history ring.
            if len(self.cooldowns) >= self.settings.history_size:
                self.cooldowns.pop(next(iter(self.cooldowns)))
            self.cooldowns[key] = now
        record = self.record(project, "alert", kind, container_id, service, message)
        if self.settings.webhook_url:
            try:
                self.notifications.put_nowait(record)
            except queue.Full:
                self.record(
                    project,
                    "overseer",
                    "notification-dropped",
                    message="Webhook queue is full",
                )
        return record

    def process_event(self, project, event):
        attrs = event.get("Actor", {}).get("Attributes", {})
        if event.get("Type") != "container" or attrs.get(PROJECT_LABEL) != project:
            return
        kind = clean_text(event.get("Action", ""), 80)
        if not (
            kind
            in {
                "create",
                "start",
                "stop",
                "die",
                "destroy",
                "restart",
                "pause",
                "unpause",
            }
            or kind.startswith("health_status:")
        ):
            return
        container_id = event.get("Actor", {}).get("ID", "")
        key = (event.get("timeNano", event.get("time")), container_id, kind)
        if key in self.event_keys:
            return
        self.event_keys.append(key)
        service = attrs.get(SERVICE_LABEL, attrs.get("name", ""))
        self.record(project, "docker", kind, container_id, service, kind)
        event_second = event.get("time")
        if isinstance(event_second, int):
            self.last_event_second = max(self.last_event_second or 0, event_second)
        if kind == "health_status: unhealthy":
            self.alert(
                project,
                "unhealthy",
                container_id,
                service,
                "Container became unhealthy",
            )
        if kind == "die":
            self.alert(
                project,
                "exit",
                container_id,
                service,
                "Container exited (code "
                + clean_text(attrs.get("exitCode", "unknown"), 16)
                + ")",
            )
        # start catches restart-policy loops, which need not emit restart events.
        if kind == "start":
            now = time.time()
            with self.condition:
                self.prune(now)
                if (
                    container_id not in self.restarts
                    and len(self.restarts) >= self.settings.history_size
                ):
                    self.restarts.pop(next(iter(self.restarts)))
                times = self.restarts.setdefault(
                    container_id, deque(maxlen=self.settings.restart_count)
                )
                times.append(now)
                repeated = len(times) >= self.settings.restart_count
            if repeated:
                self.alert(
                    project,
                    "restart-loop",
                    container_id,
                    service,
                    "Repeated starts within the restart detection window",
                )

    def check_resources(self, client, project):
        now = time.monotonic()
        if now - self.last_metrics < self.settings.metrics_interval:
            return
        self.last_metrics = now
        dashboard = self.metrics_reader(client)
        if (
            dashboard["project"]["name"] != project
            or not dashboard["project"]["compose_scoped"]
        ):
            return
        for service in dashboard["services"]:
            if not service["metrics_available"]:
                continue
            for kind, value, threshold in (
                ("cpu", service["cpu_percent"], self.settings.cpu_threshold),
                ("memory", service["memory_percent"], self.settings.memory_threshold),
            ):
                if threshold and value >= threshold:
                    self.alert(
                        project,
                        kind,
                        service["name"],
                        service["name"],
                        f"Service {kind} usage reached {value:.2f}% (threshold {threshold}%)",
                    )

    def run_once(self):
        client = self.client_factory()
        stream = None
        try:
            project = self.project_resolver(client)
            if not isinstance(project, str) or not project:
                self.status = "scope-unavailable"
                return
            if project != self.project:
                self.project = project
                self.last_event_second = int(time.time()) - 1
                self.event_keys.clear()
            self.status = "connected"
            options = {
                "decode": True,
                "filters": {"type": "container", "label": f"{PROJECT_LABEL}={project}"},
                "until": int(time.time()) + 5,
            }
            if self.last_event_second is not None:
                options["since"] = self.last_event_second
            stream = client.events(**options)
            for event in stream:
                if self.stop_event.is_set():
                    break
                self.process_event(project, event)
            if not self.stop_event.is_set():
                self.check_resources(client, project)
        finally:
            if stream is not None:
                stream.close()
            client.close()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except DOCKER_FAILURES:
                self.status = "reconnecting"
            self.stop_event.wait(1)

    def notify(self):
        while not self.stop_event.is_set():
            try:
                record = self.notifications.get(timeout=1)
            except queue.Empty:
                continue
            try:
                send_webhook(
                    self.settings.webhook_url, self.settings.webhook_token, record
                )
            except (OSError, ValueError, http.client.HTTPException):
                self.record(
                    record["project"],
                    "overseer",
                    "notification-failed",
                    message="Webhook delivery failed; check configured destination",
                )
            finally:
                self.notifications.task_done()
