import importlib
import runpy
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

app_module = importlib.import_module("overseer.app")


def make_container():
    container = MagicMock()
    container.id = "1234567890abcdef"
    container.name = "web"
    container.status = "running"
    container.image = SimpleNamespace(tags=["example/web:latest"])
    container.attrs = {
        "NetworkSettings": {
            "Ports": {
                "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
                "443/tcp": None,
            }
        },
        "Config": {
            "Image": "example/web:latest",
            "Labels": {
                "com.docker.compose.service": "web",
                "com.docker.compose.depends_on": (
                    "database:service_healthy:true,redis:service_started:false"
                ),
            },
        },
        "Image": "sha256:abcdef",
        "State": {
            "StartedAt": "2026-08-10T10:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
    }
    container.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 300, "percpu_usage": [150, 150]},
            "system_cpu_usage": 2000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 1000,
        },
        "memory_stats": {
            "usage": 120 * 1024 * 1024,
            "limit": 1024 * 1024 * 1024,
            "stats": {"inactive_file": 20 * 1024 * 1024},
        },
    }
    return container


class HelperTests(unittest.TestCase):
    def test_directory_entry_point_imports_without_package_context(self):
        namespace = runpy.run_path("overseer/__main__.py")

        self.assertIs(namespace["app"], app_module.app)

    def test_directory_entry_point_uses_secure_server_defaults(self):
        with patch.dict(app_module.os.environ, {}, clear=True):
            with patch.object(app_module.app, "run") as run:
                runpy.run_path("overseer/__main__.py", run_name="__main__")

        run.assert_called_once_with(
            debug=False,
            host="127.0.0.1",
            port=8765,
        )

    def test_get_ports_formats_published_and_exposed_ports(self):
        self.assertEqual(
            app_module.get_ports(make_container()),
            {
                "80/tcp": [{"host_ip": "0.0.0.0", "host_port": "8080"}],
                "443/tcp": None,
            },
        )

    def test_get_compose_dependencies_ignores_conditions_and_duplicates(self):
        container = make_container()
        container.attrs["Config"]["Labels"][app_module.COMPOSE_DEPENDS_ON_LABEL] = (
            "database:service_healthy:true,"
            "database:service_started:false,"
            "redis:service_started:false"
        )

        self.assertEqual(
            app_module.get_compose_dependencies(container),
            ["database", "redis"],
        )

    def test_inspect_container_metrics_calculates_cpu_and_memory(self):
        metrics = app_module.inspect_container_metrics(make_container())

        self.assertEqual(metrics["cpu_percent"], 40.0)
        self.assertEqual(metrics["memory_usage"], 100 * 1024 * 1024)
        self.assertEqual(metrics["memory_limit"], 1024 * 1024 * 1024)
        self.assertEqual(metrics["memory_percent"], 9.77)

    def test_inspect_container_metrics_skips_stopped_container(self):
        container = make_container()
        container.status = "exited"

        metrics = app_module.inspect_container_metrics(container)

        self.assertIsNone(metrics["cpu_percent"])
        self.assertIsNone(metrics["memory_usage"])
        container.stats.assert_not_called()

    def test_inspect_container_metrics_tolerates_docker_failure(self):
        container = make_container()
        container.stats.side_effect = app_module.docker.errors.APIError(
            "stats unavailable"
        )

        metrics = app_module.inspect_container_metrics(container)

        self.assertIsNone(metrics["cpu_percent"])
        self.assertIsNone(metrics["memory_usage"])

    @patch.object(app_module, "sample_container_metrics")
    def test_build_project_dashboard_aggregates_project_metadata(self, sample):
        sample.side_effect = lambda container, version: (
            app_module.inspect_container_metrics(container)
        )
        container = make_container()

        with patch.object(
            app_module,
            "list_project_containers",
            return_value=[container],
        ):
            with patch.object(
                app_module,
                "get_compose_project",
                return_value="example-project",
            ):
                dashboard = app_module.build_project_dashboard(MagicMock())

        self.assertEqual(dashboard["project"]["name"], "example-project")
        self.assertEqual(dashboard["project"]["services"], 1)
        self.assertEqual(dashboard["project"]["containers"], 1)
        self.assertEqual(dashboard["project"]["running"], 1)
        self.assertEqual(dashboard["project"]["healthy"], 1)
        self.assertEqual(dashboard["project"]["health_unreported"], 0)
        self.assertEqual(dashboard["resources"]["cpu_percent"], 40.0)
        self.assertTrue(dashboard["resources"]["metrics_available"])
        self.assertEqual(
            dashboard["resources"]["memory_usage"],
            100 * 1024 * 1024,
        )
        self.assertEqual(dashboard["services"][0]["name"], "web")

    def test_metrics_budget_returns_partial_results_and_cancels_queued_work(self):
        containers = [make_container() for _ in range(10)]
        for index, container in enumerate(containers):
            container.id = str(index)
        release = Event()
        lock = Lock()
        active = 0
        peak = 0
        calls = []
        good_metrics = app_module.inspect_container_metrics(containers[0])

        def sample(container, version):
            nonlocal active, peak
            with lock:
                calls.append(container.id)
                active += 1
                peak = max(peak, active)
            try:
                if container.id != "0":
                    release.wait(5)
                return good_metrics
            finally:
                with lock:
                    active -= 1

        executor = ThreadPoolExecutor(max_workers=2)
        try:
            with patch.object(app_module, "METRICS_EXECUTOR", executor):
                with patch.object(app_module, "sample_container_metrics", sample):
                    start = monotonic()
                    metrics = app_module.collect_project_metrics(
                        MagicMock(), containers, timeout=0.15
                    )
                    self.assertLess(monotonic() - start, 2)
                    self.assertEqual(metrics["0"], good_metrics)
                    self.assertIsNone(metrics["9"]["cpu_percent"])
                    self.assertLessEqual(peak, 2)
                    self.assertLessEqual(len(calls), 3)
        finally:
            release.set()
            executor.shutdown(wait=True)
        self.assertLessEqual(len(calls), 3)

    @patch.object(app_module.docker, "from_env")
    def test_stats_session_has_timeout_preserves_cpu_samples_and_closes(self, factory):
        container = make_container()
        client = factory.return_value = Mock()
        client.api.stats.return_value = container.stats.return_value

        metrics = app_module.sample_container_metrics(container, "1.45")

        factory.assert_called_once_with(timeout=3, version="1.45")
        client.api.stats.assert_called_once_with(container.id, stream=False)
        client.close.assert_called_once_with()
        self.assertEqual(metrics["cpu_percent"], 40.0)

    @patch.object(app_module.docker, "from_env")
    def test_timed_out_stats_are_unavailable_and_session_closes(self, factory):
        client = factory.return_value = Mock()
        client.api.stats.side_effect = app_module.requests.exceptions.ReadTimeout()

        metrics = app_module.sample_container_metrics(make_container(), "1.45")

        self.assertIsNone(metrics["cpu_percent"])
        client.close.assert_called_once_with()

    @patch.object(app_module.docker, "from_env")
    def test_failed_stats_client_creation_is_unavailable(self, factory):
        factory.side_effect = app_module.docker.errors.DockerException("offline")
        metrics = app_module.sample_container_metrics(make_container(), "1.45")
        self.assertIsNone(metrics["cpu_percent"])

    @patch.object(app_module, "sample_container_metrics")
    def test_stopped_containers_do_not_schedule_stats(self, sample):
        container = make_container()
        container.status = "exited"
        metrics = app_module.collect_project_metrics(MagicMock(), [container])
        self.assertIsNone(metrics[container.id]["cpu_percent"])
        sample.assert_not_called()

    def test_dashboard_reports_only_successfully_measured_containers(self):
        first, second = make_container(), make_container()
        second.id = "second"
        metrics = {
            first.id: app_module.inspect_container_metrics(first),
            second.id: app_module.unavailable_metrics(),
        }
        with patch.object(
            app_module, "list_project_containers", return_value=[first, second]
        ):
            with patch.object(app_module, "get_compose_project", return_value="demo"):
                with patch.object(
                    app_module, "collect_project_metrics", return_value=metrics
                ):
                    dashboard = app_module.build_project_dashboard(MagicMock())
        self.assertEqual(dashboard["project"]["running"], 2)
        self.assertEqual(dashboard["resources"]["measured_containers"], 1)
        self.assertEqual(dashboard["services"][0]["measured_containers"], 1)
        self.assertEqual(dashboard["resources"]["cpu_percent"], 40.0)

    def test_inspect_container_serializes_container_details(self):
        container = make_container()

        result = app_module.inspect_container(container)

        container.reload.assert_not_called()
        self.assertEqual(result["id"], "1234567890ab")
        self.assertEqual(result["name"], "web")
        self.assertEqual(result["service"], "web")
        self.assertEqual(result["dependencies"], ["database", "redis"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["health"], "healthy")
        self.assertEqual(result["image"], "example/web:latest")
        self.assertEqual(result["started_at"], "2026-08-10T10:00:00+00:00")
        self.assertIn("uptime", result)

    def test_inspect_container_uses_image_id_when_original_name_is_missing(self):
        container = make_container()
        container.attrs["Config"]["Image"] = ""

        result = app_module.inspect_container(container)

        self.assertEqual(result["image"], "sha256:abcdef")

    def test_stopped_container_uptime_ends_at_finished_time(self):
        container = make_container()
        container.status = "exited"
        container.attrs["State"]["FinishedAt"] = "2026-08-10T11:30:00Z"

        self.assertEqual(app_module.get_uptime(container), "1:30:00")

    def test_get_compose_project_uses_configured_project(self):
        with patch.dict(
            app_module.os.environ,
            {"OVERSEER_COMPOSE_PROJECT": "example-project"},
        ):
            project = app_module.get_compose_project(MagicMock())

        self.assertEqual(project, "example-project")

    def test_get_compose_project_detects_own_container_label(self):
        client = MagicMock()
        client.containers.get.return_value.labels = {
            app_module.COMPOSE_PROJECT_LABEL: "detected-project",
        }

        with patch.dict(app_module.os.environ, {}, clear=True):
            with patch.object(
                app_module.socket,
                "gethostname",
                return_value="overseer-container",
            ):
                project = app_module.get_compose_project(client)

        self.assertEqual(project, "detected-project")
        client.containers.get.assert_called_once_with("overseer-container")

    def test_list_project_containers_falls_back_to_all_containers(self):
        client = MagicMock()

        with patch.object(
            app_module,
            "get_overseer_container",
            return_value=None,
        ):
            with patch.object(
                app_module,
                "get_compose_project",
                return_value=None,
            ):
                app_module.list_project_containers(client)

        client.containers.list.assert_called_once_with(
            all=True,
            ignore_removed=True,
        )

    def test_list_project_containers_excludes_overseer(self):
        client = MagicMock()
        overseer_container = MagicMock()
        overseer_container.id = "overseer-id"
        overseer_container.labels = {
            app_module.COMPOSE_PROJECT_LABEL: "example-project",
        }
        service_container = MagicMock()
        service_container.id = "service-id"
        client.containers.list.return_value = [
            overseer_container,
            service_container,
        ]

        with patch.object(
            app_module,
            "get_overseer_container",
            return_value=overseer_container,
        ):
            containers = app_module.list_project_containers(client)

        self.assertEqual(containers, [service_container])
        client.containers.list.assert_called_once_with(
            all=True,
            ignore_removed=True,
            filters={
                "label": "com.docker.compose.project=example-project",
            },
        )


class RouteTests(unittest.TestCase):
    def setUp(self):
        application = app_module.create_app({"TESTING": True})
        self.client = application.test_client()
        self.action_headers = {
            app_module.CSRF_HEADER: app_module.CSRF_HEADER_VALUE,
        }

    @patch.object(app_module, "get_client")
    def test_lifecycle_action_requires_csrf_header(self, get_client):
        response = self.client.post("/api/service/container-1/start")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "CSRF validation failed"})
        get_client.assert_not_called()

    def test_index_renders_dashboard(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Overseer", response.data)
        self.assertIn(b"Project overview", response.data)
        self.assertIn(b"CPU usage", response.data)
        self.assertIn(b"Memory usage", response.data)
        self.assertIn(b"No containers found", response.data)
        self.assertIn(b"100% represents one fully utilized CPU core", response.data)
        self.assertIn(b'href="/dependencies"', response.data)
        self.assertIn(b'href="/services"', response.data)
        self.assertIn(
            b'href="/static/images/overseer-sentinel-icon.svg"',
            response.data,
        )
        self.assertIn(
            b'src="/static/images/overseer-sentinel-logo-dark.svg"',
            response.data,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_brand_assets_are_served(self):
        assets = {
            "/static/images/overseer-sentinel-icon.svg": "image/svg+xml",
            "/static/images/overseer-sentinel-logo-dark.svg": "image/svg+xml",
            "/static/images/overseer-sentinel-logo-light.svg": "image/svg+xml",
            "/static/images/overseer-sentinel-icon-512.png": "image/png",
            "/static/images/favicon-32.png": "image/png",
            "/static/images/apple-touch-icon.png": "image/png",
        }

        for path, content_type in assets.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, content_type)
                finally:
                    response.close()

    def test_dependencies_renders_graph(self):
        response = self.client.get("/dependencies")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Service dependencies", response.data)
        self.assertIn(b"View dependencies as a list", response.data)
        self.assertIn(b'role: "group"', response.data)
        self.assertIn(b'aria-current="page"', response.data)

    def test_service_controls_renders_dashboard(self):
        response = self.client.get("/services")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Container controls", response.data)
        self.assertIn(b"Search containers", response.data)
        self.assertIn(b'href="/"', response.data)
        self.assertIn(b'aria-current="page"', response.data)
        self.assertNotIn(b"View dependencies as a list", response.data)

    def test_health_check_does_not_require_docker(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok", "version": "0.2.0"})

    @patch.object(app_module, "get_client")
    def test_services_returns_inspected_containers(self, get_client):
        container = make_container()
        get_client.return_value.containers.list.return_value = [container]

        with patch.object(
            app_module,
            "get_compose_project",
            return_value="example-project",
        ):
            response = self.client.get("/api/services")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["id"], "1234567890ab")
        self.assertEqual(
            response.get_json()[0]["dependencies"],
            ["database", "redis"],
        )
        get_client.return_value.containers.list.assert_called_once_with(
            all=True,
            ignore_removed=True,
            filters={
                "label": "com.docker.compose.project=example-project",
            },
        )

    @patch.object(app_module, "get_client")
    @patch.object(app_module, "build_project_dashboard")
    def test_dashboard_returns_project_metrics(
        self,
        build_project_dashboard,
        get_client,
    ):
        build_project_dashboard.return_value = {
            "project": {"name": "example-project"},
            "resources": {"cpu_percent": 12.5},
            "services": [],
            "updated_at": "2026-08-18T00:00:00+00:00",
        }

        response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["project"]["name"], "example-project")
        build_project_dashboard.assert_called_once_with(get_client.return_value)

    @patch.object(app_module, "get_client")
    def test_service_lifecycle_endpoints(self, get_client):
        container = get_client.return_value.containers.get.return_value

        with patch.object(app_module, "get_compose_project", return_value=None):
            for action in ("start", "stop", "restart"):
                with self.subTest(action=action):
                    response = self.client.post(
                        f"/api/service/container-1/{action}",
                        headers=self.action_headers,
                    )

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), {"success": True})
                    getattr(container, action).assert_called_once_with()
                    get_client.return_value.containers.get.assert_called_with(
                        "container-1"
                    )

    @patch.object(app_module, "get_client")
    def test_lifecycle_action_rejects_container_from_another_project(
        self,
        get_client,
    ):
        container = get_client.return_value.containers.get.return_value
        container.labels = {
            app_module.COMPOSE_PROJECT_LABEL: "another-project",
        }

        with patch.object(
            app_module,
            "get_compose_project",
            return_value="overseer-project",
        ):
            response = self.client.post(
                "/api/service/container-1/stop",
                headers=self.action_headers,
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "Container not found"})
        container.stop.assert_not_called()

    @patch.object(app_module, "get_client")
    def test_missing_container_returns_json_error(self, get_client):
        get_client.return_value.containers.get.side_effect = (
            app_module.docker.errors.NotFound("missing")
        )

        response = self.client.post(
            "/api/service/missing/start",
            headers=self.action_headers,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "Container not found"})

    @patch.object(app_module, "get_client")
    def test_docker_failure_returns_json_error(self, get_client):
        get_client.return_value.containers.list.side_effect = (
            app_module.docker.errors.APIError("daemon unavailable")
        )

        with patch.object(app_module, "get_compose_project", return_value=None):
            response = self.client.get("/api/services")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "Docker operation failed"})


if __name__ == "__main__":
    unittest.main()
