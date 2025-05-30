import enum
import os
from typing import Dict  # noqa:F401
from typing import Iterable  # noqa:F401
from typing import Set  # noqa:F401

from ddtrace import config as ddconfig
from ddtrace.internal import agent
from ddtrace.internal import periodic
from ddtrace.internal.logger import get_logger
from ddtrace.internal.remoteconfig._pubsub import PubSub  # noqa:F401
from ddtrace.internal.remoteconfig.client import RemoteConfigClient
from ddtrace.internal.remoteconfig.client import config as rc_config
from ddtrace.internal.remoteconfig.constants import REMOTE_CONFIG_AGENT_ENDPOINT
from ddtrace.internal.service import ServiceStatus
from ddtrace.internal.utils.time import StopWatch


log = get_logger(__name__)


class RemoteConfigPoller(periodic.PeriodicService):
    """Remote configuration worker.

    This implements a finite-state machine that allows checking the agent for
    the expected endpoint, which could be enabled after the client is started.
    """

    _enable = True

    def __init__(self):
        super(RemoteConfigPoller, self).__init__(interval=ddconfig._remote_config_poll_interval)
        self._client = RemoteConfigClient()
        self._state = self._agent_check
        self._parent_id = os.getpid()
        self._products_to_restart_on_fork = set()
        self._capabilities_map: Dict[enum.IntFlag, str] = dict()
        log.debug("RemoteConfigWorker created with polling interval %d", ddconfig._remote_config_poll_interval)

    def _agent_check(self) -> None:
        try:
            info = agent.info()
        except Exception:
            info = None

        if info:
            endpoints = info.get("endpoints", [])
            if endpoints and (
                REMOTE_CONFIG_AGENT_ENDPOINT in endpoints or ("/" + REMOTE_CONFIG_AGENT_ENDPOINT) in endpoints
            ):
                self._state = self._online
                return
        log.debug(
            "Agent is down or Remote Config is not enabled in the Agent\n"
            "Check your Agent version, you need an Agent running on 7.39.1 version or above.\n"
            "Check Your Remote Config environment variables on your Agent:\n"
            "DD_REMOTE_CONFIGURATION_ENABLED=true\n"
            "See: https://docs.datadoghq.com/agent/guide/how_remote_config_works/",
        )

    def _online(self) -> None:
        with StopWatch() as sw:
            if not self._client.request():
                # An error occurred, so we transition back to the agent check
                self._state = self._agent_check
                return

        elapsed = sw.elapsed()
        log.debug("request config in %.5fs to %s", elapsed, self._client.agent_url)

    def periodic(self) -> None:
        return self._state()

    def enable(self) -> bool:
        # TODO: this is only temporary. DD_REMOTE_CONFIGURATION_ENABLED variable will be deprecated
        rc_env_enabled = ddconfig._remote_config_enabled
        if rc_env_enabled and self._enable:
            if self.status == ServiceStatus.RUNNING:
                return True

            self.start()

            return True
        return False

    def reset_at_fork(self) -> None:
        """Client Id needs to be refreshed when application forks"""
        self._enable = False
        log.debug("[%d][P: %d] Remote Config Poller fork. Refreshing state", os.getpid(), os.getppid())
        self._client.renew_id()
        self.start_subscribers_by_product(self._products_to_restart_on_fork)
        log.debug(
            "[%d][P: %d] Remote Config Poller restarted services: %s",
            os.getpid(),
            os.getppid(),
            str(self._products_to_restart_on_fork),
        )

    def start_subscribers_by_product(self, products: Set[str]) -> None:
        self._client.start_products(products)

    def _poll_data(self):
        """Force subscribers to poll new data. This function is only used in tests"""
        for pubsub in self._client.get_pubsubs():
            pubsub._poll_data()

    def stop_subscribers(self, join: bool = False) -> None:
        """
        Disable the remote config service and drop, remote config can be re-enabled
        by calling ``enable`` again.
        """
        log.debug(
            "[%s][P: %s] Remote Config Poller fork. Stopping  Pubsub services",
            os.getpid(),
            self._parent_id,
        )
        for pubsub in self._client.get_pubsubs():
            pubsub.stop(join=join)

    def disable(self, join: bool = False) -> None:
        self.stop_subscribers(join=join)
        self._client.reset_products()

        if self.status == ServiceStatus.STOPPED:
            return

        self.stop(join=join)

    def _stop_service(self, *args, **kwargs) -> None:
        self.stop_subscribers()

        if self.status == ServiceStatus.STOPPED or self._worker is None:
            return

        super(RemoteConfigPoller, self)._stop_service(*args, **kwargs)

    def update_product_callback(self, product, callback):
        """Some Products fork and restart their instances when application creates new process. In that case,
        we need to update the callback instance to ensure the instance of the child process receives correctly the
        Remote Configuration payloads.
        """
        return self._client.update_product_callback(product, callback)

    def register(
        self,
        product: str,
        pubsub_instance: PubSub,
        skip_enabled: bool = False,
        restart_on_fork: bool = False,
        capabilities: Iterable[enum.IntFlag] = [],
    ) -> None:
        try:
            # By enabling on registration we ensure we start the RCM client only
            # if there is at least one registered product.
            if not skip_enabled:
                self.enable()

            self._client.register_product(product, pubsub_instance)

            # Check for potential conflicts in capabilities
            for capability in capabilities:
                if self._capabilities_map.get(capability, product) != product:
                    log.error(
                        "Capability %s already registered for product %s, skipping registration",
                        capability,
                        self._capabilities_map[capability],
                    )
                    continue
                self._capabilities_map[capability] = product

            self._client.add_capabilities(capabilities)

            if not self._client.is_subscriber_running(pubsub_instance):
                pubsub_instance.start_subscriber()

            if restart_on_fork:
                self._products_to_restart_on_fork.add(product)
        except Exception:
            log.debug("error starting the RCM client", exc_info=True)

    def unregister(self, product):
        if rc_config.skip_shutdown:
            # If we are asked to skip shutdown, then we likely don't want to
            # unregister any of the products, because this is generally done
            # when the application is shutting down.
            return

        try:
            self._client.unregister_product(product)
        except Exception:
            log.debug("error starting the RCM client", exc_info=True)

    def get_registered(self, product):
        return self._client._products.get(product)

    def __enter__(self) -> "RemoteConfigPoller":
        self.enable()
        return self

    def __exit__(self, *args) -> None:
        self.disable(join=True)


remoteconfig_poller = RemoteConfigPoller()
