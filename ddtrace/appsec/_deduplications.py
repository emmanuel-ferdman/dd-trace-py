from collections import OrderedDict
from time import monotonic

from ddtrace.settings.asm import config as asm_config


M_INF = float("-inf")


class deduplication:
    _time_lapse = 3600  # 1 hour
    _max_cache_size = 256

    def __init__(self, func) -> None:
        self.func = func
        self.reported_logs: OrderedDict[int, float] = OrderedDict()

    def _extract(self, args):
        return args

    def get_last_time_reported(self, raw_log_hash: int) -> float:
        return self.reported_logs.get(raw_log_hash, 0.0)

    def _reset_cache(self):
        """
        Reset the cache of reported logs
        For testing purposes only
        """
        self.reported_logs.clear()

    def _check_deduplication(self):
        return asm_config._asm_deduplication_enabled

    def __call__(self, *args, **kwargs):
        result = False
        if self._check_deduplication():
            raw_log_hash = hash("".join([str(arg) for arg in self._extract(args)]))
            last_reported_timestamp = self.reported_logs.get(raw_log_hash, M_INF) + self._time_lapse
            current = monotonic()
            if current > last_reported_timestamp:
                self.reported_logs[raw_log_hash] = current
                self.reported_logs.move_to_end(raw_log_hash)
                if len(self.reported_logs) >= self._max_cache_size:
                    self.reported_logs.popitem(last=False)
                result = self.func(*args, **kwargs)
        else:
            result = self.func(*args, **kwargs)
        return result
