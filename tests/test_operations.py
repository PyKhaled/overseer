import importlib
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from overseer.operational_api import log_lines
from overseer.operations import (
    OperationsMonitor,
    OperationsSettings,
    PinnedHTTPSConnection,
    clean_text,
    send_webhook,
    validate_webhook_url,
)

app_module = importlib.import_module("overseer.app")


class Stream:
    def __init__(self, values):
        self.values = iter(values)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.values)

    def close(self):
        self.closed = True


def event(kind="start", project="demo", timestamp=100, container="abc"):
    return {
        "Type": "container",
        "Action": kind,
        "time": timestamp,
        "timeNano": timestamp * 10**9,
        "Actor": {
            "ID": container,
            "Attributes": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "web",
                "exitCode": "1",
            },
        },
    }


def monitor(settings=None):
    return OperationsMonitor(
        settings or OperationsSettings(), Mock(), Mock(return_value="demo"), Mock()
    )


class HistoryAndAlertsTests(unittest.TestCase):
    def test_history_capacity_retention_scope_and_cursor(self):
        m = monitor(OperationsSettings(history_size=2, history_seconds=5))
        with patch("overseer.operations.time.time", return_value=100):
            m.record("other", "docker", "start")
            second = m.record(
                "demo", "overseer", "stop", message="<script>\x1b[31mhello\x00"
            )
            third = m.record("demo", "docker", "die")
            self.assertEqual(len(m.history), 2)
            self.assertEqual(m.snapshot("demo", second["id"]), [third])
            self.assertEqual(m.snapshot("other"), [])
            self.assertEqual(second["message"], "<script>hello")
        with patch("overseer.operations.time.time", return_value=106):
            self.assertEqual(m.snapshot("demo"), [])

    def test_cooldown_reopens_and_auxiliary_state_is_bounded(self):
        m = monitor(OperationsSettings(history_size=2, alert_cooldown=10))
        with patch("overseer.operations.time.time", return_value=100):
            self.assertIsNotNone(m.alert("demo", "exit", "one", "web", "exit"))
            self.assertIsNone(m.alert("demo", "exit", "one", "web", "exit"))
            m.alert("demo", "exit", "two", "web", "exit")
            m.alert("demo", "exit", "three", "web", "exit")
            self.assertEqual(len(m.cooldowns), 2)
        with patch("overseer.operations.time.time", return_value=111):
            self.assertIsNotNone(m.alert("demo", "exit", "three", "web", "exit"))

    def test_event_filters_deduplication_health_exit_and_restart_loops(self):
        m = monitor(OperationsSettings(history_size=10, restart_count=2))
        m.process_event("demo", event(project="other"))
        m.process_event("demo", {"Type": "image"})
        m.process_event("demo", event("exec_create"))
        self.assertEqual(len(m.history), 0)
        m.process_event("demo", event("health_status: unhealthy", timestamp=101))
        m.process_event("demo", event("health_status: unhealthy", timestamp=101))
        m.process_event("demo", event("die", timestamp=102))
        m.process_event("demo", event("start", timestamp=103))
        m.process_event("demo", event("start", timestamp=104))
        kinds = [r["kind"] for r in m.history if r["source"] == "alert"]
        self.assertEqual(kinds, ["unhealthy", "exit", "restart-loop"])
        self.assertEqual(m.last_event_second, 104)

    def test_restart_detection_expires_and_is_bounded(self):
        m = monitor(
            OperationsSettings(history_size=2, restart_count=2, restart_window=5)
        )
        with patch("overseer.operations.time.time", return_value=100):
            for i in range(3):
                m.process_event("demo", event(timestamp=i, container=str(i)))
            self.assertEqual(len(m.restarts), 2)
        with patch("overseer.operations.time.time", return_value=106):
            m.process_event("demo", event(timestamp=4, container="2"))
            self.assertFalse(any(r["kind"] == "restart-loop" for r in m.history))

    def test_resource_rules_skip_unavailable_wrong_scope_and_disabled_rules(self):
        m = monitor(OperationsSettings(cpu_threshold=100, memory_threshold=90))
        dashboard = {
            "project": {"name": "demo", "compose_scoped": True},
            "services": [
                {
                    "name": "web",
                    "metrics_available": True,
                    "cpu_percent": 125,
                    "memory_percent": 95,
                },
                {"name": "db", "metrics_available": False},
            ],
        }
        m.metrics_reader.return_value = dashboard
        m.check_resources(Mock(), "demo")
        self.assertEqual([r["kind"] for r in m.history], ["cpu", "memory"])
        m.check_resources(Mock(), "demo")
        self.assertEqual(m.metrics_reader.call_count, 1)
        m.last_metrics = 0
        m.check_resources(Mock(), "other")
        self.assertEqual(len(m.history), 2)
        m.settings = replace(m.settings, cpu_threshold=0, memory_threshold=0)
        m.last_metrics = 0
        m.check_resources(Mock(), "demo")
        self.assertEqual(len(m.history), 2)

    def test_notification_queue_is_bounded(self):
        m = monitor(OperationsSettings(webhook_url="https://example.com/hook"))
        for i in range(65):
            m.alert("demo", "exit", str(i), "web", "exit")
        self.assertEqual(m.notifications.qsize(), 64)
        self.assertEqual(m.history[-1]["kind"], "notification-dropped")

    @patch("overseer.operations.send_webhook", side_effect=ValueError("private"))
    def test_failed_notification_is_safe_and_queue_is_acknowledged(self, send):
        m = monitor(OperationsSettings(webhook_url="https://example.com/secret"))
        m.alert("demo", "exit", "abc", "web", "exit")
        m.stop_event = Mock()
        m.stop_event.is_set.side_effect = [False, True]
        m.notify()
        self.assertEqual(m.notifications.unfinished_tasks, 0)
        self.assertEqual(m.history[-1]["kind"], "notification-failed")
        self.assertNotIn("secret", m.history[-1]["message"])

    @patch("overseer.operations.send_webhook")
    def test_notification_success_and_empty_queue(self, send):
        m = monitor(OperationsSettings(webhook_url="https://example.com/hook"))
        m.alert("demo", "exit", "abc", "web", "exit")
        m.stop_event = Mock()
        m.stop_event.is_set.side_effect = [False, True]
        m.notify()
        send.assert_called_once()
        m.stop_event.is_set.side_effect = [False, True]
        with patch.object(
            m.notifications, "get", side_effect=__import__("queue").Empty
        ):
            m.notify()


class CollectorTests(unittest.TestCase):
    def test_scoped_collection_closes_resources_and_replays_from_cursor(self):
        m = monitor()
        client = m.client_factory.return_value
        first = Stream([event("die", timestamp=100)])
        second = Stream([event("die", timestamp=100), event("start", timestamp=101)])
        client.events.side_effect = [first, second]
        m.metrics_reader.return_value = {
            "project": {"name": "demo", "compose_scoped": True},
            "services": [],
        }
        with patch("overseer.operations.time.time", return_value=100):
            m.run_once()
            m.run_once()
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(client.close.call_count, 2)
        kwargs = client.events.call_args.kwargs
        self.assertEqual(
            kwargs["filters"],
            {"type": "container", "label": "com.docker.compose.project=demo"},
        )
        self.assertEqual(kwargs["since"], 100)
        self.assertEqual(
            [r["kind"] for r in m.history if r["source"] == "docker"], ["die", "start"]
        )

    def test_missing_scope_does_not_request_host_events(self):
        m = monitor()
        m.project_resolver.return_value = None
        m.run_once()
        self.assertEqual(m.status, "scope-unavailable")
        m.client_factory.return_value.events.assert_not_called()
        m.client_factory.return_value.close.assert_called_once()

    def test_stopping_collector_closes_stream(self):
        m = monitor()
        stream = Stream([event()])
        m.client_factory.return_value.events.return_value = stream
        m.stop_event.set()
        m.run_once()
        self.assertTrue(stream.closed)
        self.assertEqual(len(m.history), 0)

    def test_daemon_disconnect_is_retried_without_restart(self):
        m = monitor()
        m.stop_event = Mock()
        m.stop_event.is_set.side_effect = [False, False, True]
        m.run_once = Mock(
            side_effect=[app_module.docker.errors.APIError("offline"), None]
        )
        m.run()
        self.assertEqual(m.run_once.call_count, 2)
        self.assertEqual(m.status, "reconnecting")

    @patch("overseer.operations.threading.Thread")
    def test_start_is_idempotent_and_starts_optional_notifier(self, thread):
        m = monitor(OperationsSettings(webhook_url="https://example.com/hook"))
        m.start()
        m.start()
        self.assertEqual(thread.call_count, 2)


class WebhookAndSettingsTests(unittest.TestCase):
    def test_configuration_limits_and_text_sanitization(self):
        self.assertEqual(OperationsSettings.from_config({}).history_size, 1000)
        self.assertEqual(
            OperationsSettings.from_config({"OVERSEER_HISTORY_SIZE": "4"}).history_size,
            4,
        )
        with self.assertRaises(ValueError):
            OperationsSettings.from_config({"OVERSEER_MAX_STREAMS": 5})
        with self.assertRaises(ValueError):
            OperationsSettings.from_config(
                {"OVERSEER_ALERT_WEBHOOK_URL": "http://localhost"}
            )
        self.assertEqual(clean_text("a\x00\x1b[31mb", 1), "a")

    def test_webhook_rejects_unsafe_urls(self):
        for url in (
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com:8443",
            "https://example.com/#fragment",
            "file:///tmp/test",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_webhook_url(url)

    @patch("overseer.operations.PinnedHTTPSConnection")
    @patch("overseer.operations.socket.getaddrinfo")
    def test_webhook_rejects_private_loopback_mixed_and_multicast_dns(
        self, resolve, connect
    ):
        for addresses in (
            ["127.0.0.1"],
            ["10.0.0.1"],
            ["::1"],
            ["169.254.169.254"],
            ["93.184.216.34", "192.168.1.1"],
            ["224.0.0.1"],
            [],
        ):
            resolve.return_value = [
                (None, None, None, None, (ip, 443)) for ip in addresses
            ]
            with self.assertRaises(ValueError):
                send_webhook("https://example.com/hook", "", {})
        connect.assert_not_called()

    @patch("overseer.operations.PinnedHTTPSConnection")
    @patch(
        "overseer.operations.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("93.184.216.34", 443))],
    )
    def test_webhook_pins_address_verifies_host_and_rejects_redirects(
        self, resolve, factory
    ):
        connection = factory.return_value
        connection.getresponse.return_value.status = 204
        send_webhook("https://example.com/hook?key=value", "token", {"kind": "exit"})
        factory.assert_called_with("example.com", "93.184.216.34")
        args = connection.request.call_args.args
        self.assertEqual(args[:2], ("POST", "/hook?key=value"))
        self.assertEqual(args[3]["Authorization"], "Bearer token")
        connection.close.assert_called_once()
        connection.getresponse.return_value.status = 302
        with self.assertRaises(ValueError):
            send_webhook("https://example.com", "", {})
        self.assertEqual(connection.close.call_count, 2)

    @patch("overseer.operations.socket.create_connection")
    def test_pinned_connection_uses_tls_hostname_and_closes_on_failure(self, create):
        connection = PinnedHTTPSConnection("example.com", "93.184.216.34")
        connection._context = Mock()
        connection.connect()
        create.assert_called_with(("93.184.216.34", 443), 3)
        connection._context.wrap_socket.assert_called_with(
            create.return_value, server_hostname="example.com"
        )
        connection._context.wrap_socket.side_effect = OSError("tls failed")
        with self.assertRaises(OSError):
            connection.connect()
        create.return_value.close.assert_called_once()


class OperationalRouteTests(unittest.TestCase):
    def setUp(self):
        self.application = app_module.create_app(
            {
                "TESTING": True,
                "OVERSEER_MAX_STREAMS": 1,
                "OVERSEER_LOG_BYTES": 1024,
                "OVERSEER_LINE_BYTES": 256,
            }
        )
        self.client = self.application.test_client()
        self.monitor = self.application.extensions["operations"]
        self.docker = Mock()
        self.container = self.docker.containers.get.return_value
        self.container.labels = {
            "com.docker.compose.project": "demo",
            "com.docker.compose.service": "web",
        }
        self.container.id = "abc"
        self.container.name = "web"
        self.factory = patch.object(app_module, "get_client", return_value=self.docker)
        self.scope = patch.object(
            app_module, "get_compose_project", return_value="demo"
        )
        self.factory.start()
        self.scope.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.scope.stop)

    def test_operations_page_and_history_scope_limits(self):
        response = self.client.get("/operations")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Application logs", response.data)
        self.monitor.record("demo", "docker", "start")
        self.monitor.record("other", "docker", "start")
        response = self.client.get("/api/history?limit=1")
        self.assertEqual(len(response.json["records"]), 1)
        self.assertEqual(response.json["records"][0]["project"], "demo")
        for query in ("limit=no", "limit=0", "after=-1"):
            self.assertEqual(self.client.get("/api/history?" + query).status_code, 400)
        self.assertEqual(self.client.get("/api/events?after=no").status_code, 400)

    def test_default_history_limit_respects_small_capacity(self):
        application = app_module.create_app(
            {"TESTING": True, "OVERSEER_HISTORY_SIZE": 1}
        )
        application.extensions["operations"].record("demo", "docker", "start")
        response = application.test_client().get("/api/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["records"]), 1)

    def test_no_scope_fails_closed_before_log_lookup_or_events(self):
        with patch.object(app_module, "get_compose_project", return_value=None):
            for path in ("/api/history", "/api/events", "/api/service/abc/logs"):
                self.assertEqual(self.client.get(path).status_code, 503)
        self.docker.containers.get.assert_not_called()

    def test_cross_project_logs_are_rejected(self):
        self.container.labels = {"com.docker.compose.project": "other"}
        response = self.client.get("/api/service/abc/logs")
        self.assertEqual(response.status_code, 404)
        self.container.logs.assert_not_called()
        self.docker.close.assert_called_once()

    def test_logs_are_bounded_sanitized_and_downloadable(self):
        stream = Stream([b"\x1b[31mhello\x00\n", b"x" * 5000])
        self.container.logs.return_value = stream
        response = self.client.get(
            "/api/service/abc/logs?tail=2&download=1&timestamps=0"
        )
        self.assertLessEqual(len(response.data), 1024)
        self.assertTrue(response.data.startswith(b"hello\n"))
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(stream.closed)
        self.docker.close.assert_called_once()
        self.container.logs.assert_called_once_with(
            stream=True, follow=False, tail=2, timestamps=False
        )
        response.close()

    def test_log_query_validation(self):
        for query in ("tail=all", "tail=999999", "since=-1", "follow=1&download=1"):
            response = self.client.get("/api/service/abc/logs?" + query)
            self.assertEqual(response.status_code, 400)
        self.container.logs.assert_not_called()

    def test_event_disconnect_releases_slot_and_filters_history(self):
        self.monitor.record("other", "docker", "die")
        record = self.monitor.record("demo", "overseer", "stop", message="manual stop")
        response = self.client.get("/api/events", buffered=False)
        self.assertIn(b"event: status", next(response.response))
        self.assertEqual(self.client.get("/api/events").status_code, 429)
        data = next(response.response)
        self.assertIn(str(record["id"]).encode(), data)
        self.assertNotIn(b'"project": "other"', data)
        response.close()
        self.assertTrue(self.monitor.stream_slots.acquire(blocking=False))
        self.monitor.stream_slots.release()

    def test_event_end_and_heartbeat(self):
        with patch(
            "overseer.operational_api.time.monotonic", side_effect=[0, 1, 2, 30]
        ):
            with patch.object(self.monitor.condition, "wait"):
                response = self.client.get("/api/events", buffered=True)
        self.assertIn(b": heartbeat", response.data)
        self.assertIn(b"event: end", response.data)
        response.close()
        self.assertTrue(self.monitor.stream_slots.acquire(blocking=False))
        self.monitor.stream_slots.release()

    def test_live_logs_disconnect_closes_docker_stream_and_client(self):
        stream = Stream([b"line\n", b"next\n"])
        self.container.logs.return_value = stream
        response = self.client.get("/api/service/abc/logs?follow=1", buffered=False)
        self.assertIn(b"event: log", next(response.response))
        self.assertEqual(self.client.get("/api/events").status_code, 429)
        response.close()
        self.assertTrue(stream.closed)
        self.docker.close.assert_called()
        self.assertTrue(self.monitor.stream_slots.acquire(blocking=False))
        self.monitor.stream_slots.release()
        self.assertIn("until", self.container.logs.call_args.kwargs)

    def test_stream_limit_closes_rejected_logs_client(self):
        self.monitor.stream_slots.acquire()
        response = self.client.get("/api/service/abc/logs?follow=1")
        self.assertEqual(response.status_code, 429)
        self.docker.close.assert_called_once()
        self.container.logs.assert_not_called()
        self.monitor.stream_slots.release()

    def test_log_initial_and_streaming_docker_errors_close_resources(self):
        self.container.logs.side_effect = app_module.docker.errors.APIError(
            "unavailable"
        )
        response = self.client.get("/api/service/abc/logs")
        self.assertEqual(response.status_code, 502)
        self.docker.close.assert_called_once()

        def fail():
            yield b"first\n"
            raise app_module.requests.exceptions.ReadTimeout()

        self.container.logs.side_effect = None
        self.container.logs.return_value = fail()
        response = self.client.get("/api/service/abc/logs?follow=1")
        self.assertIn(b"event: unavailable", response.data)
        response.close()
        self.container.logs.return_value = fail()
        response = self.client.get("/api/service/abc/logs")
        self.assertIn(b"retry the request", response.data)
        response.close()

    def test_overseer_actions_record_only_successful_project_actions(self):
        response = self.client.post(
            "/api/service/abc/restart", headers={"X-Overseer-CSRF": "1"}
        )
        self.assertEqual(response.status_code, 200)
        record = self.monitor.snapshot("demo")[0]
        self.assertEqual((record["source"], record["kind"]), ("overseer", "restart"))
        self.assertEqual(self.client.post("/api/service/abc/stop").status_code, 403)
        self.assertEqual(len(self.monitor.snapshot("demo")), 1)

    def test_monitor_is_lazy_and_not_started_in_tests(self):
        with patch.object(self.monitor, "start") as start:
            self.client.get("/healthz")
            start.assert_not_called()
            self.application.testing = False
            self.client.get("/api/history")
            start.assert_called_once()

    def test_byte_cap_stops_log_output_with_many_short_lines(self):
        self.container.logs.return_value = Stream([b"\n" * 2000])
        response = self.client.get("/api/service/abc/logs?follow=1")
        self.assertLessEqual(len(response.data), 1024)
        response.close()


class LogLineTests(unittest.TestCase):
    def test_utf8_split_long_lines_and_unterminated_output(self):
        parts = [b"\xe2", b"\x82\xac\n", b"a" * 100, b"end"]
        lines = list(log_lines(parts, 200, 16))
        self.assertEqual(lines[0]["text"], "€")
        self.assertEqual(lines[1]["text"], "a" * 16)
        self.assertTrue(lines[1]["truncated"])
