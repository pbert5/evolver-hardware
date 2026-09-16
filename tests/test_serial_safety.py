from __future__ import annotations

import threading
import time

from evolver_hardware.hardware import ReadOnlyHardwareService
from evolver_hardware.store import EdgeStore


class BlockingTransport:
    port = "/dev/mock"

    def __init__(self):
        self.entered = threading.Event(); self.release = threading.Event()
        self.active = 0; self.max_active = 0

    def open(self):
        self.active += 1; self.max_active = max(self.max_active, self.active)
        self.entered.set()

    def close(self):
        self.active -= 1

def test_same_daemon_sessions_are_serialized(tmp_path):
    with EdgeStore(tmp_path) as store:
        transport = BlockingTransport()
        service = ReadOnlyHardwareService(store, transport, startup_attempts=1)
        first = threading.Thread(target=lambda: _hold(service, transport))
        first.start()
        assert transport.entered.wait(1)
        second_error = []
        second = threading.Thread(target=lambda: _hold(service, transport, second_error))
        second.start()
        time.sleep(0.1)
        assert not second_error
        transport.release.set()
        first.join(2); second.join(2)
        assert not second_error
        assert transport.max_active == 1


def _capture(service, errors):
    try:
        service.discover()
    except Exception as error:
        errors.append(error)


def _hold(service, transport, errors=None):
    try:
        with service._session():
            transport.release.wait(2)
    except Exception as error:
        if errors is not None:
            errors.append(error)
