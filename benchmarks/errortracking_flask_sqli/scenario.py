import bm
from bm.flask_utils import FlaskScenarioMixin


class ErrorTrackingFlaskSQLi(bm.Scenario, FlaskScenarioMixin):
    # DEV: These should better go in FlaskScenarioMixin
    # but then the logic to get them wouldn't work
    tracer_enabled: bool
    profiler_enabled: bool
    debugger_enabled: bool
    appsec_enabled: bool
    iast_enabled: bool
    post_request: bool
    telemetry_metrics_enabled: bool
    errortracking_enabled: str

    def run(self):
        app = self.create_app()

        # Setup the request function
        headers = {
            "SERVER_PORT": "8000",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:8000",
            "HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
            "HTTP_SEC_FETCH_DEST": "document",
            "HTTP_ACCEPT_ENCODING": "gzip, deflate, br",
            "HTTP_ACCEPT_LANGUAGE": "en-US,en;q=0.9",
            "User-Agent": "dd-test-scanner-log",
        }

        def make_request(app):
            client = app.test_client()
            return client.post(
                "/sqli_with_errortracking",
                headers=headers,
                data={"username": "shaquille_oatmeal", "password": "123456"},
            )

        # Scenario loop function
        def _(loops):
            for _ in range(loops):
                res = make_request(app)
                assert res.status_code == 200
                res.close()

        yield _
