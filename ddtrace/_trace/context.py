import base64
import re
import threading
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Text
from typing import Tuple

from ddtrace._trace._span_link import SpanLink
from ddtrace._trace.types import _MetaDictType
from ddtrace._trace.types import _MetricDictType
from ddtrace.constants import _ORIGIN_KEY
from ddtrace.constants import _SAMPLING_PRIORITY_KEY
from ddtrace.constants import _USER_ID_KEY
from ddtrace.internal.compat import NumericType
from ddtrace.internal.constants import MAX_UINT_64BITS as _MAX_UINT_64BITS
from ddtrace.internal.constants import W3C_TRACEPARENT_KEY
from ddtrace.internal.constants import W3C_TRACESTATE_KEY
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.http import w3c_get_dd_list_member as _w3c_get_dd_list_member


_ContextState = Tuple[
    Optional[int],  # trace_id
    Optional[int],  # span_id
    _MetaDictType,  # _meta
    _MetricDictType,  # _metrics
    List[SpanLink],  #  span_links
    Dict[str, Any],  # baggage
    bool,  # is_remote
    bool,  # _reactivate
]


_DD_ORIGIN_INVALID_CHARS_REGEX = re.compile(r"[^\x20-\x7E]+")

log = get_logger(__name__)


class Context(object):
    """Represents the state required to propagate a trace across execution
    boundaries.
    """

    __slots__ = [
        "trace_id",
        "span_id",
        "_lock",
        "_meta",
        "_metrics",
        "_span_links",
        "_baggage",
        "_is_remote",
        "_reactivate",
        "__weakref__",
    ]

    def __init__(
        self,
        trace_id: Optional[int] = None,
        span_id: Optional[int] = None,
        dd_origin: Optional[str] = None,
        sampling_priority: Optional[float] = None,
        meta: Optional[_MetaDictType] = None,
        metrics: Optional[_MetricDictType] = None,
        lock: Optional[threading.RLock] = None,
        span_links: Optional[List[SpanLink]] = None,
        baggage: Optional[Dict[str, Any]] = None,
        is_remote: bool = True,
    ):
        self._meta: _MetaDictType = meta if meta is not None else {}
        self._metrics: _MetricDictType = metrics if metrics is not None else {}
        self._baggage: Dict[str, Any] = baggage if baggage is not None else {}

        self.trace_id: Optional[int] = trace_id
        self.span_id: Optional[int] = span_id
        self._is_remote: bool = is_remote
        self._reactivate: bool = False

        if dd_origin is not None and _DD_ORIGIN_INVALID_CHARS_REGEX.search(dd_origin) is None:
            self._meta[_ORIGIN_KEY] = dd_origin
        if sampling_priority is not None:
            self._metrics[_SAMPLING_PRIORITY_KEY] = sampling_priority
        if span_links is not None:
            self._span_links = span_links
        else:
            self._span_links = []

        if lock is not None:
            self._lock = lock
        else:
            # DEV: A `forksafe.RLock` is not necessary here since Contexts
            # are recreated by the tracer after fork
            # https://github.com/DataDog/dd-trace-py/blob/a1932e8ddb704d259ea8a3188d30bf542f59fd8d/ddtrace/tracer.py#L489-L508
            self._lock = threading.RLock()

    def __getstate__(self) -> _ContextState:
        return (
            self.trace_id,
            self.span_id,
            self._meta,
            self._metrics,
            self._span_links,
            self._baggage,
            self._is_remote,
            self._reactivate,
            # Note: self._lock is not serializable
        )

    def __setstate__(self, state: _ContextState) -> None:
        (
            self.trace_id,
            self.span_id,
            self._meta,
            self._metrics,
            self._span_links,
            self._baggage,
            self._is_remote,
            self._reactivate,
        ) = state
        # We cannot serialize and lock, so we must recreate it unless we already have one
        self._lock = threading.RLock()

    def __enter__(self) -> "Context":
        self._lock.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self._lock.release()

    @property
    def sampling_priority(self) -> Optional[NumericType]:
        """Return the context sampling priority for the trace."""
        return self._metrics.get(_SAMPLING_PRIORITY_KEY)

    @sampling_priority.setter
    def sampling_priority(self, value: Optional[NumericType]) -> None:
        with self._lock:
            if value is None:
                if _SAMPLING_PRIORITY_KEY in self._metrics:
                    del self._metrics[_SAMPLING_PRIORITY_KEY]
                return
            self._metrics[_SAMPLING_PRIORITY_KEY] = value

    @property
    def _traceparent(self) -> str:
        tp = self._meta.get(W3C_TRACEPARENT_KEY)
        if self.span_id is None or self.trace_id is None:
            # if we only have a traceparent then we'll forward it
            # if we don't have a span id or trace id value we can't build a valid traceparent
            return tp or ""

        # determine the trace_id value
        if tp:
            # grab the original traceparent trace id, not the converted value
            trace_id = tp.split("-")[1]
        else:
            trace_id = "{:032x}".format(self.trace_id)

        return "00-{}-{:016x}-{}".format(trace_id, self.span_id, self._traceflags)

    @property
    def _traceflags(self) -> str:
        return "01" if self.sampling_priority and self.sampling_priority > 0 else "00"

    @property
    def _tracestate(self) -> str:
        dd_list_member = _w3c_get_dd_list_member(self)

        # if there's a preexisting tracestate we need to update it to preserve other vendor data
        ts = self._meta.get(W3C_TRACESTATE_KEY, "")
        if ts and dd_list_member:
            # cut out the original dd list member from tracestate so we can replace it with the new one we created
            ts_w_out_dd = re.sub("dd=(.+?)(?:,|$)", "", ts)
            if ts_w_out_dd:
                ts = "dd={},{}".format(dd_list_member, ts_w_out_dd)
            else:
                ts = "dd={}".format(dd_list_member)
        # if there is no original tracestate value then tracestate is just the dd list member we created
        elif dd_list_member:
            ts = "dd={}".format(dd_list_member)
        return ts

    @property
    def dd_origin(self) -> Optional[Text]:
        """Get the origin of the trace."""
        return self._meta.get(_ORIGIN_KEY)

    @dd_origin.setter
    def dd_origin(self, value: Optional[Text]) -> None:
        """Set the origin of the trace."""
        with self._lock:
            if value is None:
                if _ORIGIN_KEY in self._meta:
                    del self._meta[_ORIGIN_KEY]
                return
            self._meta[_ORIGIN_KEY] = value

    @property
    def dd_user_id(self) -> Optional[Text]:
        """Get the user ID of the trace."""
        user_id = self._meta.get(_USER_ID_KEY)
        if user_id:
            return str(base64.b64decode(user_id), encoding="utf-8")
        return None

    @dd_user_id.setter
    def dd_user_id(self, value: Optional[Text]) -> None:
        """Set the user ID of the trace."""
        with self._lock:
            if value is None:
                if _USER_ID_KEY in self._meta:
                    del self._meta[_USER_ID_KEY]
                return
            self._meta[_USER_ID_KEY] = str(base64.b64encode(bytes(value, encoding="utf-8")), encoding="utf-8")

    @property
    def _trace_id_64bits(self):
        """Return the trace ID as a 64-bit value."""
        if self.trace_id is None:
            return None
        else:
            return _MAX_UINT_64BITS & self.trace_id

    def set_baggage_item(self, key: str, value: Any) -> None:
        """Sets a baggage item in this span context.
        Note that this operation mutates the baggage of this span context
        """
        self._baggage[key] = value

    def copy(self, trace_id: int, span_id: int) -> "Context":
        """Return a shallow copy of the context with the given correlation IDs."""
        return self.__class__(
            trace_id=trace_id,
            span_id=span_id,
            meta=self._meta,
            metrics=self._metrics,
            lock=self._lock,
            baggage=self._baggage,
            is_remote=False,
        )

    def _with_baggage_item(self, key: str, value: Any) -> "Context":
        """Returns a copy of this span with a new baggage item.
        Useful for instantiating new child span contexts.
        """
        new_baggage = dict(self._baggage)
        new_baggage[key] = value

        ctx = self.__class__(trace_id=self.trace_id, span_id=self.span_id)
        ctx._meta = self._meta
        ctx._metrics = self._metrics
        ctx._baggage = new_baggage
        return ctx

    def get_baggage_item(self, key: str) -> Optional[Any]:
        """Gets a baggage item in this span context."""
        return self._baggage.get(key, None)

    def get_all_baggage_items(self) -> Dict[str, Any]:
        """Returns all baggage items in this span context."""
        return self._baggage

    def remove_baggage_item(self, key: str) -> None:
        """Remove a baggage item from this span context."""
        if key in self._baggage:
            del self._baggage[key]

    def remove_all_baggage_items(self) -> None:
        """Removes all baggage items from this span context."""
        self._baggage.clear()

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Context):
            with self._lock:
                return (
                    self.trace_id == other.trace_id
                    and self._meta == other._meta
                    and self._metrics == other._metrics
                    and self._span_links == other._span_links
                    and self._baggage == other._baggage
                    and self._is_remote == other._is_remote
                )
        return False

    def __repr__(self) -> str:
        return "Context(trace_id=%s, span_id=%s, _meta=%s, _metrics=%s, _span_links=%s, _baggage=%s, _is_remote=%s)" % (
            self.trace_id,
            self.span_id,
            self._meta,
            self._metrics,
            self._span_links,
            self._baggage,
            self._is_remote,
        )

    def __hash__(self) -> int:
        return hash(self.trace_id)

    __str__ = __repr__
