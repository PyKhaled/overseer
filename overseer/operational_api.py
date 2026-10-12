"""Project-scoped operational APIs. Long-lived responses have finite leases."""

import json
import time

import docker
import requests
from flask import Response, jsonify, render_template, request

from .operations import OperationsMonitor, OperationsSettings, clean_text


class ScopeUnavailable(Exception):
    pass


def integer_arg(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def sse(kind, data, sequence=None):
    identifier = f"id: {sequence}\n" if sequence is not None else ""
    return identifier + f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def log_lines(stream, byte_limit, line_limit):
    consumed = 0
    pending = b""
    truncated = False
    for chunk in stream:
        remaining = byte_limit - consumed
        chunk = chunk[:remaining]
        consumed += len(chunk)
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            room = line_limit - len(pending)
            pending += part[:room]
            truncated = truncated or len(part) > room
            if index < len(parts) - 1:
                yield {
                    "text": clean_text(pending.decode("utf-8", "replace"), line_limit),
                    "truncated": truncated,
                }
                pending, truncated = b"", False
        if consumed >= byte_limit:
            truncated = True
            break
    if pending:
        yield {
            "text": clean_text(pending.decode("utf-8", "replace"), line_limit),
            "truncated": truncated,
        }


def register_operations(application, client_factory, project_resolver, metrics_reader):
    settings = OperationsSettings.from_config(application.config)
    monitor = OperationsMonitor(
        settings, client_factory, project_resolver, metrics_reader
    )
    application.extensions["operations"] = monitor

    def resolve_scope(client):
        project = project_resolver(client)
        if not isinstance(project, str) or not project:
            raise ScopeUnavailable(
                "Configure OVERSEER_COMPOSE_PROJECT or run inside Compose"
            )
        return project

    def scoped_client():
        client = client_factory()
        try:
            project = resolve_scope(client)
        except BaseException:
            client.close()
            raise
        return client, project

    def stream_response(generator, cleanup):
        response = Response(generator, mimetype="text/event-stream")
        response.headers["X-Accel-Buffering"] = "no"
        response.call_on_close(cleanup)
        return response

    @application.before_request
    def start_monitor():
        if not application.testing and request.path.startswith("/api/"):
            monitor.start()

    @application.errorhandler(ScopeUnavailable)
    def scope_error(error):
        return jsonify({"error": str(error)}), 503

    @application.route("/operations")
    def operations():
        return render_template(
            "operations.html", log_tail=settings.log_tail, log_bytes=settings.log_bytes
        )

    @application.route("/api/history")
    def history():
        try:
            limit = integer_arg(
                "limit", min(200, settings.history_size), 1, settings.history_size
            )
            after = integer_arg("after", 0, 0, 2**63 - 1)
        except ValueError:
            return jsonify({"error": "Invalid request parameters"}), 400
        client, project = scoped_client()
        client.close()
        return jsonify(
            {
                "project": project,
                "instance": monitor.instance,
                "status": monitor.status,
                "records": monitor.snapshot(project, after, limit),
            }
        )

    @application.route("/api/events")
    def events():
        try:
            after = integer_arg(
                "after", request.headers.get("Last-Event-ID", 0), 0, 2**63 - 1
            )
        except ValueError:
            return jsonify({"error": "Invalid request parameters"}), 400
        client, project = scoped_client()
        client.close()
        if not monitor.stream_slots.acquire(blocking=False):
            return jsonify(
                {"error": "Stream limit reached; pause another live view"}
            ), 429
        released = False

        def cleanup():
            nonlocal released
            if not released:
                released = True
                monitor.stream_slots.release()

        def generate():
            cursor = after
            deadline = time.monotonic() + settings.stream_seconds
            try:
                yield sse(
                    "status",
                    {
                        "status": monitor.status,
                        "project": project,
                        "instance": monitor.instance,
                    },
                )
                while time.monotonic() < deadline:
                    with monitor.condition:
                        records = monitor.snapshot(
                            project, cursor, settings.history_size
                        )
                        if not records:
                            monitor.condition.wait(
                                timeout=min(5, max(0, deadline - time.monotonic()))
                            )
                            records = monitor.snapshot(
                                project, cursor, settings.history_size
                            )
                    for record in records:
                        cursor = record["id"]
                        yield sse("record", record, cursor)
                    yield ": heartbeat\n\n"
                yield sse("end", {"reconnect": True})
            finally:
                cleanup()

        return stream_response(generate(), cleanup)

    @application.route("/api/service/<container_id>/logs")
    def logs(container_id):
        try:
            tail = integer_arg("tail", settings.log_tail, 0, settings.log_tail)
            since = integer_arg("since", 0, 0, 2**63 - 1)
            live = request.args.get("follow", "0") == "1"
            download = request.args.get("download", "0") == "1"
            timestamps = request.args.get("timestamps", "1") == "1"
            if live and download:
                raise ValueError("Downloads cannot follow a live stream")
        except ValueError:
            return jsonify({"error": "Invalid request parameters"}), 400
        client, project = scoped_client()
        stream = None
        acquired = False
        closed = False

        def cleanup():
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                if stream is not None:
                    stream.close()
            finally:
                client.close()
                if acquired:
                    monitor.stream_slots.release()

        try:
            container = client.containers.get(container_id)
            if (container.labels or {}).get("com.docker.compose.project") != project:
                raise docker.errors.NotFound("Container not found in Overseer project")
            if live:
                acquired = monitor.stream_slots.acquire(blocking=False)
                if not acquired:
                    cleanup()
                    return jsonify(
                        {"error": "Stream limit reached; pause another live view"}
                    ), 429
            options = {
                "stream": True,
                "follow": live,
                "tail": tail,
                "timestamps": timestamps,
            }
            if since:
                options["since"] = since
            if live:
                options["until"] = int(time.time()) + settings.stream_seconds
            stream = container.logs(**options)
        except BaseException:
            cleanup()
            raise

        def generate():
            size = 0
            try:
                for line in log_lines(stream, settings.log_bytes, settings.line_bytes):
                    chunk = sse("log", line) if live else line["text"] + "\n"
                    encoded = chunk.encode()
                    if size + len(encoded) > settings.log_bytes - 256:
                        break
                    size += len(encoded)
                    yield encoded
                if live:
                    yield sse("end", {"reconnect": True}).encode()
            except (
                docker.errors.DockerException,
                requests.exceptions.RequestException,
            ):
                if live:
                    yield sse(
                        "unavailable",
                        {"error": "Docker logs unavailable; reconnecting"},
                    ).encode()
                else:
                    yield b"\n[Docker logs unavailable; retry the request]\n"
            finally:
                cleanup()

        if live:
            return stream_response(generate(), cleanup)
        response = Response(generate(), mimetype="text/plain")
        response.call_on_close(cleanup)
        if download:
            response.headers["Content-Disposition"] = (
                'attachment; filename="container.log"'
            )
        return response

    return monitor
