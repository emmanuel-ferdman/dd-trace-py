import contextlib
import http.client as httplib
import socket
import sys
from urllib import parse
from urllib.request import Request
from urllib.request import build_opener
from urllib.request import urlopen

import pytest
import wrapt

from ddtrace import config
from ddtrace.contrib.internal.httplib.patch import patch
from ddtrace.contrib.internal.httplib.patch import should_skip_request
from ddtrace.contrib.internal.httplib.patch import unpatch
from ddtrace.ext import http
from ddtrace.internal.constants import _HTTPLIB_NO_TRACE_REQUEST
from ddtrace.internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ddtrace.trace import Pin
from tests.opentracer.utils import init_tracer
from tests.utils import TracerTestCase
from tests.utils import assert_span_http_status_code
from tests.utils import override_global_tracer


# socket name comes from https://english.stackexchange.com/a/44048
SOCKET = "localhost:8001"
URL_200 = "http://{}/status/200".format(SOCKET)
URL_500 = "http://{}/status/500".format(SOCKET)
URL_404 = "http://{}/status/404".format(SOCKET)


class HTTPLibBaseMixin(object):
    SPAN_NAME = "http.client.request"

    def to_str(self, value):
        return value.decode("utf-8")

    def setUp(self):
        super(HTTPLibBaseMixin, self).setUp()

        patch()
        Pin._override(httplib, tracer=self.tracer)

    def tearDown(self):
        unpatch()

        super(HTTPLibBaseMixin, self).tearDown()


# Main test cases for httplib/http.client and urllib2/urllib.request
class HTTPLibTestCase(HTTPLibBaseMixin, TracerTestCase):
    SPAN_NAME = "http.client.request"

    def to_str(self, value):
        """Helper method to decode a string or byte object to a string"""
        return value.decode("utf-8")

    def get_http_connection(self, *args, **kwargs):
        conn = httplib.HTTPConnection(*args, **kwargs)
        Pin._override(conn, tracer=self.tracer)
        return conn

    def get_https_connection(self, *args, **kwargs):
        conn = httplib.HTTPSConnection(*args, **kwargs)
        Pin._override(conn, tracer=self.tracer)
        return conn

    def test_patch(self):
        """
        When patching httplib
            we patch the correct module/methods
        """
        self.assertIsInstance(httplib.HTTPConnection.__init__, wrapt.BoundFunctionWrapper)
        self.assertIsInstance(httplib.HTTPConnection.request, wrapt.BoundFunctionWrapper)
        self.assertIsInstance(httplib.HTTPConnection.getresponse, wrapt.BoundFunctionWrapper)

    def test_unpatch(self):
        """
        When unpatching httplib
            we restore the correct module/methods
        """
        original_init = httplib.HTTPConnection.__init__.__wrapped__
        original_request = httplib.HTTPConnection.request.__wrapped__
        original_getresponse = httplib.HTTPConnection.getresponse.__wrapped__
        unpatch()

        self.assertEqual(httplib.HTTPConnection.__init__, original_init)
        self.assertEqual(httplib.HTTPConnection.request, original_request)
        self.assertEqual(httplib.HTTPConnection.getresponse, original_getresponse)

    def test_should_skip_request(self):
        """
        When calling should_skip_request
            with an enabled Pin and non-internal request
                returns False
            with a disabled Pin and non-internal request
                returns True
            with an enabled Pin and internal request
                returns True
            with a disabled Pin and internal request
                returns True
        """
        # Enabled Pin and non-internal request
        self.tracer.enabled = True
        request = self.get_http_connection(SOCKET)
        pin = Pin.get_from(request)
        self.assertFalse(should_skip_request(pin, request))

        # Disabled Pin and non-internal request
        self.tracer.enabled = False
        request = self.get_http_connection(SOCKET)
        pin = Pin.get_from(request)
        self.assertTrue(should_skip_request(pin, request))

        # Enabled Pin and internal request
        self.tracer.enabled = True
        parsed = parse.urlparse(self.tracer.agent_url)
        request = self.get_http_connection(parsed.hostname, parsed.port)
        pin = Pin.get_from(request)
        self.assertTrue(should_skip_request(pin, request))

        # Disabled Pin and internal request
        self.tracer.enabled = False
        request = self.get_http_connection(parsed.hostname, parsed.port)
        pin = Pin.get_from(request)
        self.assertTrue(should_skip_request(pin, request))

    def test_httplib_request_get_request_no_ddtrace(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            while setting _dd_no_trace attr to True
                we do not capture any spans
        """
        self.tracer.enabled = True
        conn = self.get_http_connection(SOCKET)
        setattr(conn, _HTTPLIB_NO_TRACE_REQUEST, True)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 0)

    def test_httplib_request_get_request(self, query_string=""):
        """
        When making a GET request via httplib.HTTPConnection.request
            we return the original response
            we capture a span for the request
        """
        if query_string:
            fqs = "?" + query_string
        else:
            fqs = ""
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200" + fqs)
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        assert span.get_tag("http.method") == "GET"
        assert span.get_tag("component") == "httplib"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("out.host") == "localhost"
        assert span.get_tag("http.url") == URL_200 + fqs
        assert_span_http_status_code(span, 200)
        if config.httplib.trace_query_string:
            assert span.get_tag(http.QUERY_STRING) == query_string
        else:
            assert http.QUERY_STRING not in span.get_tags()

    def test_httplib_request_get_request_qs(self):
        with self.override_http_config("httplib", dict(trace_query_string=True)):
            return self.test_httplib_request_get_request("foo=bar")

    def test_httplib_request_get_request_multiqs(self):
        with self.override_http_config("httplib", dict(trace_query_string=True)):
            return self.test_httplib_request_get_request("foo=bar&foo=baz&x=y")

    def test_httplib_request_get_request_https(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            when making an HTTPS connection
                we return the original response
                we capture a span for the request
        """
        # TODO: figure out how to use https in our local httpbin container
        conn = self.get_https_connection("icanhazdadjoke.com")
        with contextlib.closing(conn):
            conn.request(
                "GET",
                "/j/R7UfaahVfFd",
                headers={"Accept": "text/plain", "User-Agent": "ddtrace-test"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(
                self.to_str(resp.read()),
                "My dog used to chase people on a bike a lot. It got so bad I had to take his bike away.",
            )

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        assert span.get_tag("http.method") == "GET"
        assert span.get_tag("component") == "httplib"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("out.host") == "icanhazdadjoke.com"
        assert_span_http_status_code(span, 200)
        assert span.get_tag("http.url") == "https://icanhazdadjoke.com/j/R7UfaahVfFd"

    def test_httplib_request_post_request(self):
        """
        When making a POST request via httplib.HTTPConnection.request
            we return the original response
            we capture a span for the request
        """
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("POST", "/status/200", body="key=value")
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        assert span.get_tag("http.method") == "POST"
        assert span.get_tag("component") == "httplib"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("out.host") == "localhost"
        assert_span_http_status_code(span, 200)
        assert span.get_tag("http.url") == URL_200

    def test_httplib_request_get_request_query_string(self):
        """
        When making a GET request with a query string via httplib.HTTPConnection.request
            we capture the all of the url in the span except for the query string
        """
        qs = "?key=value&key2=value2"
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200" + qs)
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        assert span.get_tag("http.method") == "GET"
        assert span.get_tag("component") == "httplib"
        assert span.get_tag("span.kind") == "client"
        assert span.get_tag("out.host") == "localhost"
        assert_span_http_status_code(span, 200)
        assert span.get_tag("http.url") == URL_200 + qs

    def test_httplib_request_500_request(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            when the response is a 500
                we raise the original exception
                we mark the span as an error
                we capture the correct span tags
        """
        try:
            conn = self.get_http_connection(SOCKET)
            with contextlib.closing(conn):
                conn.request("GET", "/status/500")
                conn.getresponse()
        except httplib.HTTPException:
            resp = sys.exc_info()[1]
            self.assertEqual(self.to_str(resp.read()), "500 Internal Server Error")
            self.assertEqual(resp.status, 500)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 1)
        self.assertEqual(span.get_tag("http.method"), "GET")
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "localhost")
        assert_span_http_status_code(span, 500)
        self.assertEqual(span.get_tag("http.url"), URL_500)

    def test_httplib_request_non_200_request(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            when the response is a non-200
                we raise the original exception
                we mark the span as an error
                we capture the correct span tags
        """
        try:
            conn = self.get_http_connection(SOCKET)
            with contextlib.closing(conn):
                conn.request("GET", "/status/404")
                conn.getresponse()
        except httplib.HTTPException:
            resp = sys.exc_info()[1]
            self.assertEqual(self.to_str(resp.read()), "404 Not Found")
            self.assertEqual(resp.status, 404)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        self.assertEqual(span.get_tag("http.method"), "GET")
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "localhost")
        assert_span_http_status_code(span, 404)
        self.assertEqual(span.get_tag("http.url"), URL_404)

    def test_httplib_request_get_request_disabled(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            when the tracer is disabled
                we do not capture any spans
        """
        self.tracer.enabled = False
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 0)

    def test_httplib_request_get_request_disabled_and_enabled(self):
        """
        When making a GET request via httplib.HTTPConnection.request
            when the tracer is disabled
                we do not capture any spans
        """
        self.tracer.enabled = False
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            self.tracer.enabled = True
            resp = conn.getresponse()
            self.assertEqual(self.to_str(resp.read()), "")
            self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 0)

    def test_httplib_request_and_response_headers(self):
        # Disabled when not configured
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200", headers={"my-header": "my_value"})
            conn.getresponse()
            spans = self.pop_spans()
            s = spans[0]
            self.assertEqual(s.get_tag("http.request.headers.my_header"), None)
            self.assertEqual(s.get_tag("http.response.headers.access_control_allow_origin"), None)

        # Enabled when configured
        with self.override_config("httplib", {}):
            from ddtrace.settings import IntegrationConfig  # noqa:F401

            integration_config = config.httplib  # type: IntegrationConfig
            integration_config.http.trace_headers(["my-header", "access-control-allow-origin"])
            conn = self.get_http_connection(SOCKET)
            with contextlib.closing(conn):
                conn.request("GET", "/status/200", headers={"my-header": "my_value"})
                conn.getresponse()
                spans = self.pop_spans()
        s = spans[0]
        self.assertEqual(s.get_tag("http.request.headers.my-header"), "my_value")
        self.assertEqual(s.get_tag("http.response.headers.access-control-allow-origin"), "*")

    def test_urllib_request(self):
        """
        When making a request via urllib.request.urlopen
           we return the original response
           we capture a span for the request
        """
        with override_global_tracer(self.tracer):
            resp = urlopen(URL_200)

        self.assertEqual(self.to_str(resp.read()), "")
        self.assertEqual(resp.getcode(), 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        self.assertEqual(span.get_tag("http.method"), "GET")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.get_tag("http.url"), URL_200)
        self.assertEqual(span.get_tag("component"), "httplib")
        assert span.get_tag("span.kind") == "client"

    def test_urllib_request_https(self):
        """
        When making a request via urllib.request.urlopen
           when making an HTTPS connection
               we return the original response
               we capture a span for the request
        """
        # TODO: figure out how to use https in our local httpbin container
        url = "https://icanhazdadjoke.com/j/R7UfaahVfFd"
        req = Request(
            url,
            headers={"Accept": "text/plain", "User-Agent": "ddtrace-test"},
        )
        with override_global_tracer(self.tracer):
            resp = urlopen(req)

        self.assertEqual(resp.getcode(), 200)
        self.assertEqual(
            self.to_str(resp.read()),
            "My dog used to chase people on a bike a lot. It got so bad I had to take his bike away.",
        )

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        self.assertEqual(span.get_tag("http.method"), "GET")
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "icanhazdadjoke.com")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.get_tag("http.url"), url)

    def test_urllib_request_object(self):
        """
        When making a request via urllib.request.urlopen
           with a urllib.request.Request object
               we return the original response
               we capture a span for the request
        """
        req = Request(URL_200)
        with override_global_tracer(self.tracer):
            resp = urlopen(req)

        self.assertEqual(self.to_str(resp.read()), "")
        self.assertEqual(resp.getcode(), 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        self.assertEqual(span.get_tag("http.method"), "GET")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.get_tag("http.url"), URL_200)
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "localhost")

    def test_urllib_request_opener(self):
        """
        When making a request via urllib.request.OpenerDirector
           we return the original response
           we capture a span for the request
        """
        opener = build_opener()
        with override_global_tracer(self.tracer):
            resp = opener.open(URL_200)

        self.assertEqual(self.to_str(resp.read()), "")
        self.assertEqual(resp.getcode(), 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 0)
        self.assertEqual(span.get_tag("http.method"), "GET")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.get_tag("http.url"), URL_200)
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "localhost")

    def test_httplib_request_get_request_ot(self):
        """OpenTracing version of test with same name."""
        ot_tracer = init_tracer("my_svc", self.tracer)

        with ot_tracer.start_active_span("ot_span"):
            conn = self.get_http_connection(SOCKET)
            with contextlib.closing(conn):
                conn.request("GET", "/status/200")
                resp = conn.getresponse()
                self.assertEqual(self.to_str(resp.read()), "")
                self.assertEqual(resp.status, 200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 2)
        ot_span, dd_span = spans

        # confirm the parenting
        self.assertEqual(ot_span.parent_id, None)
        self.assertEqual(dd_span.parent_id, ot_span.span_id)

        self.assertEqual(ot_span.service, "my_svc")
        self.assertEqual(ot_span.name, "ot_span")

        self.assert_is_not_measured(dd_span)
        self.assertEqual(dd_span.span_type, "http")
        self.assertEqual(dd_span.name, self.SPAN_NAME)
        self.assertEqual(dd_span.error, 0)
        assert dd_span.get_tag("http.method") == "GET"
        assert_span_http_status_code(dd_span, 200)
        assert dd_span.get_tag("http.url") == URL_200

    def test_httplib_bad_url(self):
        conn = self.get_http_connection("DNE", "80")
        with contextlib.closing(conn):
            with pytest.raises(socket.gaierror):
                conn.request("GET", "/status/500")

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assert_is_not_measured(span)
        self.assertEqual(span.span_type, "http")
        self.assertEqual(span.service, "tests.contrib.httplib")
        self.assertEqual(span.name, self.SPAN_NAME)
        self.assertEqual(span.error, 1)
        self.assertEqual(span.get_tag("http.method"), "GET")
        self.assertEqual(span.get_tag("http.url"), "http://DNE:80/status/500")
        self.assertEqual(span.get_tag("component"), "httplib")
        self.assertEqual(span.get_tag("span.kind"), "client")
        self.assertEqual(span.get_tag("out.host"), "DNE")

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc"))
    def test_schematization_httplib_service_name_default(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc"))
    def test_schematization_urlib_service_name_default(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_httplib_service_name_v0(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_urllib_service_name_v0(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_httplib_service_name_v1(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_urllib_service_name_v1(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, "mysvc", msg=span.service)

    @TracerTestCase.run_in_subprocess()
    def test_schematization_httplib_unspecified_service_name_default(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsNone(span.service, msg=span.service)

    @TracerTestCase.run_in_subprocess()
    def test_schematization_urllib_unspecified_service_name_default(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsNone(span.service, msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_httplib_unspecified_service_name_v0(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsNone(span.service, msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_urllib_unspecified_service_name_v0(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsNone(span.service, msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_httplib_unspecified_service_name_v1(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, DEFAULT_SPAN_SERVICE_NAME, msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_urllib_unspecified_service_name_v1(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.service, DEFAULT_SPAN_SERVICE_NAME, msg=span.service)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_httplib_unspecified_operation_name_v0(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.name, self.SPAN_NAME)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_urllib_unspecified_operation_name_v0(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.name, self.SPAN_NAME)

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_httplib_unspecified_operation_name_v1(self):
        conn = self.get_http_connection(SOCKET)
        with contextlib.closing(conn):
            conn.request("GET", "/status/200")
            conn.getresponse()

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.name, "http.client.request")

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_urllib_unspecified_operation_name_v1(self):
        with override_global_tracer(self.tracer):
            urlopen(URL_200)

        spans = self.pop_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.name, "http.client.request")
