import ipaddress
import logging
import re
from urllib import parse

from ddtrace.contrib.internal.grpc import constants
from ddtrace.ext import net


log = logging.getLogger(__name__)


def parse_method_path(method_path):
    """Returns (package_service, package, service, method) tuple from parsing method path"""
    # unpack method path based on "/{package}.{service}/{method}"
    # first remove leading "/" as unnecessary
    package_service, method_name = method_path.lstrip("/").rsplit("/", 1)

    package_service_split = package_service.rsplit(".", 1)
    # {package} is optional
    if len(package_service_split) == 2:
        return package_service, package_service_split[0], package_service_split[1], method_name

    return package_service, None, package_service_split[0], method_name


def set_grpc_method_meta(span, method, method_kind):
    method_path = method
    method_package_service, method_package, method_service, method_name = parse_method_path(method_path)
    if method_package_service is not None:
        span.set_tag_str(constants.GRPC_METHOD_PACKAGE_SERVICE_KEY, method_package_service)
    if method_path is not None:
        span.set_tag_str(constants.GRPC_METHOD_PATH_KEY, method_path)
    if method_package is not None:
        span.set_tag_str(constants.GRPC_METHOD_PACKAGE_KEY, method_package)
    if method_service is not None:
        span.set_tag_str(constants.GRPC_METHOD_SERVICE_KEY, method_service)
    if method_name is not None:
        span.set_tag_str(constants.GRPC_METHOD_NAME_KEY, method_name)
    if method_kind is not None:
        span.set_tag_str(constants.GRPC_METHOD_KIND_KEY, method_kind)


def set_grpc_client_meta(span, host, port):
    if host:
        span.set_tag_str(constants.GRPC_HOST_KEY, host)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            span.set_tag_str(net.PEER_HOSTNAME, host)
        else:
            span.set_tag_str(net.TARGET_IP, host)
    if port:
        span.set_tag_str(net.TARGET_PORT, str(port))
    span.set_tag_str(constants.GRPC_SPAN_KIND_KEY, constants.GRPC_SPAN_KIND_VALUE_CLIENT)


def _parse_target_from_args(args, kwargs):
    if "target" in kwargs:
        target = kwargs["target"]
    else:
        target = args[0]

    try:
        if target is None:
            return

        # ensure URI follows RFC 3986 and is preceded by double slash
        # https://tools.ietf.org/html/rfc3986#section-3.2
        parsed = parse.urlsplit("//" + target if not target.startswith("//") else target)
        port = None
        try:
            port = parsed.port
        except ValueError:
            log.warning("Non-integer port in target '%s'", target)

        # an empty hostname in Python 2.7 will be an empty string rather than
        # None
        hostname = parsed.hostname if parsed.hostname is not None and len(parsed.hostname) > 0 else None

        return hostname, port
    except ValueError:
        log.warning("Malformed target '%s'.", target)


def _parse_rpc_repr_string(rpc_string, module):
    # Define the regular expression patterns to extract status and details
    status_pattern = r"status\s*=\s*StatusCode\.(\w+)"
    details_pattern = r'details\s*=\s*"([^"]*)"'

    # Search for the status and details in the input string
    status_match = re.search(status_pattern, rpc_string)
    details_match = re.search(details_pattern, rpc_string)

    if not status_match or not details_match:
        raise ValueError("Unable to parse grpc status or details repr string")

    # Extract the status and details from the matches
    status_str = status_match.group(1)
    details = details_match.group(1)

    # Convert the status string to a grpc.StatusCode object
    try:
        code = module.StatusCode[status_str]
    except KeyError:
        code = None
        raise ValueError(f"Invalid grpc status code: {status_str}")

    # Return the status code and details
    return code, details
