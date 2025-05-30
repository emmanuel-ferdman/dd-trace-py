"""Public API for User events"""

from ddtrace.appsec._trace_utils import block_request  # noqa: F401
from ddtrace.appsec._trace_utils import block_request_if_user_blocked  # noqa: F401
from ddtrace.appsec._trace_utils import should_block_user  # noqa: F401
from ddtrace.appsec._trace_utils import track_custom_event  # noqa: F401
from ddtrace.appsec._trace_utils import track_user_login_failure_event  # noqa: F401
from ddtrace.appsec._trace_utils import track_user_login_success_event  # noqa: F401
from ddtrace.appsec._trace_utils import track_user_signup_event  # noqa: F401
import ddtrace.internal.core


ddtrace.internal.core.on("set_user_for_asm", block_request_if_user_blocked, "block_user")
