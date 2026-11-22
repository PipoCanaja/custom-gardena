"""Connection observation for debugging (custom-gardena, not for upstream).

ObservedConnection behaves exactly like gardena_bluetooth.client.CachedConnection:
same establish_connection call (services cache on, same number of attempts),
same disconnect delay. It only records what happens.

For every connection attempt, successful connection, disconnection and
unexpected disconnection it writes one JSON line to
/config/gardena_observe.jsonl (rotated at 5 MB, one old file kept) and one INFO
line to the Home Assistant log (logger custom_components.gardena_bluetooth.observe).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import logging
import os
import sys
import threading
import time
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from gardena_bluetooth.client import CachedConnection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

FILE_NAME = "gardena_observe.jsonl"
MAX_FILE_SIZE = 5 * 1024 * 1024
GARDENA_SERVICE_PREFIX = "98bd"

_FILE_LOCK = threading.Lock()


def _write_line(path: str, line: str) -> None:
    with _FILE_LOCK:
        try:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")


def _trigger() -> str:
    """Tell whether the current call comes from the periodic poll or an action."""
    frame = sys._getframe(1)  # noqa: SLF001
    names = set()
    while frame is not None:
        names.add(frame.f_code.co_name)
        frame = frame.f_back
    if "_async_update_data" in names:
        return "poll"
    if "async_config_entry_first_refresh" in names:
        return "setup"
    if names & {"write_char", "write_char_raw"}:
        return "action"
    return "other"


def _proxy_name(client: BleakClient) -> str | None:
    backend = getattr(client, "_backend", None)
    for attr in ("_source_name", "_source"):
        if value := getattr(backend, attr, None):
            return str(value)
    return getattr(client, "_backend_id", None)


class ObservedConnection(CachedConnection):
    """CachedConnection that records every connection event."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        disconnect_delay: float,
        device_lookup: Callable[[], BLEDevice],
    ) -> None:
        """Initialize the connection."""
        super().__init__(disconnect_delay, device_lookup)
        self._hass = hass
        self._address = address
        self._path = hass.config.path(FILE_NAME)
        self._started = time.monotonic()
        self._connected_at: float | None = None
        self._last_disconnect: float | None = None
        self._expected_disconnect = False
        self._counters = {
            "attempts": 0,
            "ok": 0,
            "fail": 0,
            "consecutive_fail": 0,
            "unexpected_disconnects": 0,
        }

    # Recording ---------------------------------------------------------------

    def _record(self, event: str, **fields: Any) -> None:
        now = time.monotonic()
        entry: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "address": self._address,
            "ha_uptime_s": round(now - self._started, 1),
            **fields,
            "counters": dict(self._counters),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        LOGGER.info("%s", line)
        self._hass.async_add_executor_job(_write_line, self._path, line)

    def _radio_snapshot(self) -> dict[str, Any]:
        """Describe what Home Assistant knows about the device before connecting."""
        now = time.monotonic()
        snapshot: dict[str, Any] = {}
        info = bluetooth.async_last_service_info(
            self._hass, self._address, connectable=True
        )
        if info is not None:
            snapshot["last_adv"] = {
                "source": info.source,
                "rssi": info.rssi,
                "age_s": round(now - info.time, 1),
                "manufacturer_data": {
                    str(key): value.hex(" ")
                    for key, value in info.manufacturer_data.items()
                },
            }
        candidates = []
        try:
            devices = bluetooth.async_scanner_devices_by_address(
                self._hass, self._address, connectable=True
            )
        except Exception as err:  # noqa: BLE001 - observation only
            snapshot["candidates_error"] = repr(err)
            devices = []
        for device in devices:
            scanner = device.scanner
            failures = None
            if hasattr(scanner, "connection_failures"):
                failures = scanner.connection_failures(self._address)
            elif hasattr(scanner, "_connect_failures"):
                failures = scanner._connect_failures.get(self._address, 0)  # noqa: SLF001
            candidates.append(
                {
                    "proxy": getattr(scanner, "name", None),
                    "source": getattr(scanner, "source", None),
                    "rssi": device.advertisement.rssi,
                    "failures": failures,
                }
            )
        snapshot["candidates"] = candidates
        return snapshot

    # Connection (same behaviour as CachedConnection) ------------------------

    async def _connect(self) -> BleakClient:
        device = self._lookup()
        trigger = _trigger()
        since_disconnect = (
            None
            if self._last_disconnect is None
            else round(time.monotonic() - self._last_disconnect, 1)
        )
        snapshot = self._radio_snapshot()
        self._counters["attempts"] += 1
        self._record(
            "connect_start",
            trigger=trigger,
            since_last_disconnect_s=since_disconnect,
            **snapshot,
        )

        start = time.monotonic()
        try:
            client = await establish_connection(
                BleakClient,
                device,
                "Gardena Bluetooth",
                use_services_cache=True,
                max_attempts=self._max_attempts,
                disconnected_callback=self._on_disconnected,
            )
        except BaseException as err:
            self._counters["fail"] += 1
            self._counters["consecutive_fail"] += 1
            self._record(
                "connect_fail",
                trigger=trigger,
                duration_s=round(time.monotonic() - start, 2),
                error_type=type(err).__name__,
                error=str(err),
                cause=repr(err.__cause__) if err.__cause__ else None,
            )
            raise

        self._client = client
        self._connected_at = time.monotonic()
        self._expected_disconnect = False
        self._counters["ok"] += 1
        self._counters["consecutive_fail"] = 0
        services = [service.uuid for service in client.services]
        self._record(
            "connect_ok",
            trigger=trigger,
            duration_s=round(self._connected_at - start, 2),
            proxy=_proxy_name(client),
            mtu=getattr(client, "mtu_size", None),
            services=len(services),
            gardena_services=sum(
                1 for uuid in services if uuid.startswith(GARDENA_SERVICE_PREFIX)
            ),
        )
        return client

    async def _disconnect(self) -> None:
        if self._client is not None:
            self._expected_disconnect = True
        await super()._disconnect()

    def _on_disconnected(self, client: BleakClient) -> None:
        now = time.monotonic()
        session = (
            None if self._connected_at is None else round(now - self._connected_at, 2)
        )
        self._connected_at = None
        self._last_disconnect = now
        if self._expected_disconnect:
            self._record("disconnect", session_s=session, proxy=_proxy_name(client))
        else:
            self._counters["unexpected_disconnects"] += 1
            self._record(
                "unexpected_disconnect", session_s=session, proxy=_proxy_name(client)
            )
        self._expected_disconnect = False

