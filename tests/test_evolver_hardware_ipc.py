from __future__ import annotations

import json
import socket
import stat
import struct
import time

import pytest

from evolver_hardware.hardware import HardwareService
from evolver_hardware.hardware_ipc import (
    DEFAULT_IPC_TIMEOUT_SECONDS,
    HARDWARE_EXCHANGE_TIMEOUT_SECONDS,
    PROVISIONING_EXCHANGE_COUNT,
    PROVISIONING_INNER_BUDGET_SECONDS,
    PROVISIONING_IPC_TIMEOUT_SECONDS,
    HardwareIPCServer,
    request,
)
from evolver_hardware.store import EdgeStore, LeaseValidationError


class Transport:
    port = "/dev/mock"
    def __init__(self): self.opened = False; self.commands = []
    def open(self): self.opened = True
    def close(self): self.opened = False
    def exchange(self, payload):
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!": return "MEV|2|MEV-1|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id=MEV-1"
        if payload == "HW_STATUS_!": return "HW|1|OK|STATUS|sleeves=2,pumps=6"
        if payload.startswith("HW_READ_THERMISTOR,"): return "HW|1|OK|THERMISTOR|value=34416"
        if payload.startswith("HW_READ_PHOTODIODE,"): return "HW|1|OK|PHOTODIODE|value=65520"
        if payload == "HW_SAFE_!": return "HW|1|OK|SAFE|"
        if payload.startswith("HW_PULSE_STIR,"): return "HW|1|OK|PULSE_STIR|channel=0"
        raise AssertionError(payload)


class BlankProvisionTransport(Transport):
    def __init__(self):
        super().__init__(); self.device_id = "BLANK"; self.owner_id = "BLANK"

    def exchange(self, payload):
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return f"MEV|2|{self.device_id}|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id={self.device_id},owner={self.owner_id}"
        if payload.startswith("PROVISION,"):
            _, self.device_id, self.owner_id = payload.removesuffix("_!").split(",")
            return f"MEV|2|{self.device_id}|1|PROVISION_ACK|id={self.device_id},owner={self.owner_id}|00"
        raise AssertionError(payload)


def test_ipc_is_typed_and_read_only_calls_use_the_daemon(tmp_path):
    with EdgeStore(tmp_path) as store:
        transport = Transport(); service = HardwareService(store, transport, allow_physical=True)
        path = tmp_path / "hardware.sock"; server = HardwareIPCServer(store, service, path); server.start()
        try:
            found = request(path, {"operation": "discover"})
            assert found["device_identity"] == "MEV-1"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert request(path, {"operation": "protocol_test"})["verification"] == "protocol_verified"
            with pytest.raises(RuntimeError, match="unsupported typed"):
                request(path, {"operation": "raw_frame", "raw_frame": "PULSE"})
        finally:
            server.close()


def test_ipc_client_disconnect_does_not_kill_accept_loop(tmp_path):
    with EdgeStore(tmp_path) as store:
        path = tmp_path / "hardware.sock"
        server = HardwareIPCServer(store, HardwareService(store, Transport(), allow_physical=True), path)
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(path))
            # Force the server's response to encounter a reset after this
            # malformed request, matching a CLI timeout closing its socket.
            client.sendall(b"{")
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()

            deadline = time.monotonic() + 2
            while True:
                try:
                    assert request(path, {"operation": "lease_status"}, timeout=0.2) == {"status": "none"}
                    break
                except (OSError, RuntimeError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise
        finally:
            server.close()


def test_ipc_actuation_requires_local_lease_and_generation(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = Transport(); service = HardwareService(store, transport, allow_physical=True)
        path = tmp_path / "hardware.sock"; server = HardwareIPCServer(store, service, path); server.start()
        try:
            found = request(path, {"operation": "discover"})
            with pytest.raises(RuntimeError, match="lease"):
                request(path, {"operation": "set_stir", "target_identity": found["device_identity"], "physical": True,
                               "operator": "ash", "controller_generation": 1, "parameters": {"channel": 0, "duration_ms": 100, "level": 1}})
            lease = request(path, {"operation": "lease_acquire", "operator": "ash", "ttl_seconds": 60})
            result = request(path, {"operation": "set_stir", "target_identity": found["device_identity"], "physical": True,
                                    "operator": "ash", "lease_token": lease["token"], "controller_generation": 1,
                                    "parameters": {"channel": 0, "duration_ms": 100, "level": 1}})
            assert result["verification"] == "protocol_verified"
            assert transport.commands.count("HW_PULSE_STIR,0,100,1_!") == 1
        finally:
            server.close()


def test_ipc_records_layout_without_changing_stable_vial_ids(tmp_path):
    with EdgeStore(tmp_path) as store:
        path = tmp_path / "hardware.sock"; server = HardwareIPCServer(store, HardwareService(store, Transport(), allow_physical=True), path); server.start()
        try:
            found = request(path, {"operation": "discover"})
            layout = request(path, {"operation": "layout_record", "target_identity": "MEV-1", "operator": "ash",
                                    "positions": {"0": {"physical_side": "left", "method": "operator_observed"}}})
            assert layout["physical_layout"]["0"]["physical_side"] == "left"
            assert store.instrument(found["id"])["vial_positions"][0]["id"] == found["vial_positions"][0]["id"]
        finally:
            server.close()


def test_ipc_identity_provisioning_requires_physical_operator_and_returns_readback(tmp_path):
    with EdgeStore(tmp_path) as store:
        transport = BlankProvisionTransport()
        path = tmp_path / "hardware.sock"
        server = HardwareIPCServer(store, HardwareService(store, transport, allow_physical=True), path)
        server.start()
        try:
            with pytest.raises(RuntimeError, match="physical"):
                request(path, {"operation": "provision_identity", "device_id": "MEV-002",
                                "owner_id": "lab", "operator": "ash"})
            result = request(path, {"operation": "provision_identity", "device_id": "MEV-002",
                                    "owner_id": "lab", "operator": "ash", "physical": True})
            assert result["observed_evidence"]["device_id"] == "MEV-002"
            assert result["observed_evidence"]["owner_id"] == "lab"
            assert result["retryable"] is False
            assert all("CLEAR_ID" not in command for command in transport.commands)
            assert transport.commands.count("WHO_ARE_YOU_!") == PROVISIONING_EXCHANGE_COUNT - 1
        finally:
            server.close()


def test_provisioning_ipc_timeout_exceeds_inner_three_exchange_budget_without_changing_default():
    assert PROVISIONING_INNER_BUDGET_SECONDS == PROVISIONING_EXCHANGE_COUNT * HARDWARE_EXCHANGE_TIMEOUT_SECONDS
    assert PROVISIONING_IPC_TIMEOUT_SECONDS > PROVISIONING_INNER_BUDGET_SECONDS
    assert DEFAULT_IPC_TIMEOUT_SECONDS < PROVISIONING_IPC_TIMEOUT_SECONDS


def test_provisioning_ipc_timeout_rejects_budget_that_could_expire_mid_operation():
    with pytest.raises(ValueError, match="inner three-exchange budget"):
        request("/does/not/connect", {"operation": "provision_identity"}, PROVISIONING_INNER_BUDGET_SECONDS)
