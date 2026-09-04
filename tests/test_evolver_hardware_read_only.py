from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from evolver_hardware import EdgeStore, EdgeStoreError, LeaseValidationError
from evolver_hardware.hardware import (HardwareUnavailableError,
                                                                    HardwareCommand, HardwareService, LocalSerialTransport,
                                                                    ReadOnlyHardwareService)
from evolver_hardware.hardware import (IdentityState, normalize_identity, parse_identity,
                                                                    ProbeError, ProbeOutcome, normalize_effective_device_state)
from evolver_hardware.hardware_service import build_parser, poll_once


class FakeSerial:
    """Deterministic pyserial-shaped endpoint with protocol-frame reads."""

    instances: list["FakeSerial"] = []
    responses: dict[str, bytes] = {}
    initial_input = b""

    def __init__(self, port: str, baudrate: int, timeout: float) -> None:
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.incoming = self.initial_input
        self.writes: list[bytes] = []
        self.reset_count = 0
        self.closed = False
        self.__class__.instances.append(self)

    def reset_input_buffer(self) -> None:
        self.reset_count += 1
        self.incoming = b""

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        self.incoming += self.responses.get(payload.decode(), b"")
        return len(payload)

    def flush(self) -> None:
        return None

    def read_until(self, expected: bytes) -> bytes:
        position = self.incoming.find(expected)
        if position < 0:
            result, self.incoming = self.incoming, b""
            return result
        end = position + len(expected)
        result, self.incoming = self.incoming[:end], self.incoming[end:]
        return result

    def close(self) -> None:
        self.closed = True


def serial_module(monkeypatch: pytest.MonkeyPatch, *, responses: dict[str, bytes], initial_input: bytes = b"") -> None:
    FakeSerial.instances.clear()
    FakeSerial.responses = responses
    FakeSerial.initial_input = initial_input
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=FakeSerial))


def test_local_serial_transport_sends_exact_frame_without_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    serial_module(monkeypatch, responses={"PING_!": b"PONG\n"})
    transport = LocalSerialTransport("/dev/ttyACM-fake")
    transport.open()
    assert transport.exchange("PING_!") == "PONG"
    assert FakeSerial.instances[0].writes == [b"PING_!"]


def test_local_serial_transport_reads_firmware_newline_terminated_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    serial_module(monkeypatch, responses={"PING_!": b"MEV|2|id|1|HELLO|type=minievolver\r\n"})
    transport = LocalSerialTransport("/dev/ttyACM-fake")
    transport.open()
    assert transport.exchange("PING_!") == "MEV|2|id|1|HELLO|type=minievolver"


def test_local_serial_transport_discards_stale_input_at_session_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    serial_module(monkeypatch, responses={"PING_!": b"FRESH\n"}, initial_input=b"STALE\n")
    transport = LocalSerialTransport("/dev/ttyACM-fake")
    transport.open()
    assert transport.exchange("PING_!") == "FRESH"
    assert FakeSerial.instances[0].reset_count == 1


def test_local_serial_transport_reads_sequential_frames_without_newline_assumptions(monkeypatch: pytest.MonkeyPatch) -> None:
    serial_module(monkeypatch, responses={"ONE_!": b"FIRST\nSECOND\n", "TWO_!": b"THIRD\n"})
    transport = LocalSerialTransport("/dev/ttyACM-fake")
    transport.open()
    assert transport.exchange("ONE_!") == "FIRST"
    assert transport.exchange("TWO_!") == "SECOND"
    assert transport.exchange("TWO_!") == "THIRD"


class FakeTransport:
    port = "/dev/ttyACM-fake"

    def __init__(self, device_id: str = "MEV-001") -> None:
        self.device_id, self.opened, self.commands = device_id, False, []

    def open(self) -> None: self.opened = True
    def close(self) -> None: self.opened = False
    def exchange(self, payload: str) -> str:
        assert self.opened
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return f"MEV|2|{self.device_id}|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id={self.device_id}"
        if payload == "HW_STATUS_!": return "HW|1|OK|STATUS|sleeves=2,pumps=6,hw_proto=1"
        if payload.startswith("HW_READ_THERMISTOR,"):
            return f"HW|1|OK|THERMISTOR|channel={payload.split(',')[1][0]},value=32000"
        if payload.startswith("HW_READ_PHOTODIODE,"):
            return f"HW|1|OK|PHOTODIODE|channel={payload.split(',')[1][0]},value=20000"
        raise AssertionError(f"unsafe or unknown command: {payload}")


def test_read_only_service_registers_provisioned_inventory_and_spools_raw_sensor_data(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = FakeTransport()
        service = ReadOnlyHardwareService(store, transport)
        instrument = service.discover()
        assert instrument["source"] == "physical"
        assert instrument["identity_state"] == "provisioned"
        assert len(instrument["vial_positions"]) == 2
        assert instrument["capabilities"]["pump_control"]["enabled"] is False
        record = service.capture_telemetry(instrument["id"])
        assert record["payload"]["thermistor_adc_0"] == 32000
        assert record["payload"]["calibration"]["od"] == "not_calibrated"
        assert all("PULSE" not in command and "SET_OD_LED" not in command for command in transport.commands)
        assert transport.opened is False


def test_unprovisioned_hardware_is_visible_but_never_assigned_a_tty_derived_identity(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        discovered = ReadOnlyHardwareService(store, FakeTransport("")).discover()
        assert discovered["identity_state"] == "unprovisioned"
        assert "id" not in discovered
        assert store.list_instruments() == []


class OpensThenTimesOutTransport(FakeTransport):
    """Deterministic equivalent of an open CDC ACM device with no handshake reply."""

    def exchange(self, payload: str) -> str:
        self.commands.append(payload)
        assert payload == "WHO_ARE_YOU_!"
        raise ProbeError(ProbeOutcome.TIMEOUT, "timeout waiting for WHO_ARE_YOU_!",
                         evidence={"operation": "exchange", "command": payload,
                                   "detail": "r" * 1000})


def test_open_device_handshake_timeout_has_typed_bounded_evidence_and_closes(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = OpensThenTimesOutTransport()
        with pytest.raises(ProbeError) as raised:
            ReadOnlyHardwareService(store, transport, startup_attempts=1).discover()

        error = raised.value
        assert error.outcome is ProbeOutcome.TIMEOUT
        assert error.evidence["operation"] == "exchange"
        assert error.evidence["command"] == "WHO_ARE_YOU_!"
        assert error.evidence["attempt"] == 1
        assert error.evidence["max_attempts"] == 1
        assert len(error.evidence["detail"]) == 256
        assert transport.commands == ["WHO_ARE_YOU_!"]
        assert transport.opened is False
        assert store.list_instruments() == []


def test_startup_retry_does_not_retry_non_timeout_probe_failure(tmp_path) -> None:
    class MalformedTransport(FakeTransport):
        def exchange(self, payload: str) -> str:
            self.commands.append(payload)
            raise ProbeError(ProbeOutcome.MALFORMED, "malformed identity reply",
                             evidence={"operation": "identity", "reply": "garbage"})

    with EdgeStore(tmp_path) as store:
        transport = MalformedTransport()
        with pytest.raises(ProbeError) as raised:
            ReadOnlyHardwareService(store, transport, startup_attempts=5).discover()

        assert raised.value.outcome is ProbeOutcome.MALFORMED
        assert transport.commands == ["WHO_ARE_YOU_!"]
        assert transport.opened is False
        assert store.list_instruments() == []


def test_repeated_discovery_is_idempotent_and_sends_only_read_commands(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = FakeTransport()
        service = ReadOnlyHardwareService(store, transport)

        first = service.discover()
        second = service.discover()

        assert second["id"] == first["id"]
        assert len(store.list_instruments()) == 1
        assert transport.commands == ["WHO_ARE_YOU_!", "HW_STATUS_!"] * 2
        assert all(not command.startswith(("HW_PULSE_", "HW_SET_", "HW_SAFE_"))
                   for command in transport.commands)


def test_blank_identity_is_visible_during_poll_without_registration(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        def transport_factory(port: str) -> FakeTransport:
            transport = FakeTransport("")
            transport.port = port
            return transport

        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"],
                  transport_factory=transport_factory)
        observation = store.hardware_observation()
        assert observation["identity_state"] == "unprovisioned"
        assert observation["connection_state"] == "connected"
        assert observation["transport"]["path"] == "/dev/ttyACM0"
        assert store.list_instruments() == []


@pytest.mark.parametrize("value", [None, "", "BLANK"])
def test_identity_normalizer_accepts_only_firmware_unprovisioned_forms(value) -> None:
    assert normalize_identity(value) == (IdentityState.UNPROVISIONED, None)


def test_identity_normalizer_does_not_hide_malformed_or_placeholder_like_ids() -> None:
    assert normalize_identity("BLANK-01")[0] is IdentityState.VALID
    assert normalize_identity("bad|frame")[0] is IdentityState.INVALID
    assert normalize_identity(b"BLANK")[0] is IdentityState.INVALID


@pytest.mark.parametrize(("reply", "outcome"), [
    ("garbage", ProbeOutcome.MALFORMED),
    ("MEV|2|id|1|HELLO|type=other,hw_proto=1", ProbeOutcome.IDENTITY),
    ("MEV|9|id|1|HELLO|type=minievolver,hw_proto=1", ProbeOutcome.PROTOCOL),
])
def test_identity_probe_failures_are_typed(reply, outcome) -> None:
    with pytest.raises(ProbeError) as raised:
        parse_identity(reply)
    assert raised.value.outcome is outcome
    assert len(str(raised.value.evidence["reply"])) <= 256 if "reply" in raised.value.evidence else True


class ResettingTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.handshakes = 0

    def exchange(self, payload: str) -> str:
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            self.handshakes += 1
            if self.handshakes == 1:
                raise ProbeError(ProbeOutcome.TIMEOUT, "USB reset still in progress")
        return super().exchange(payload)


def test_discovery_retries_only_bounded_startup_timeout_without_sleep(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = ResettingTransport()
        discovered = ReadOnlyHardwareService(store, transport, startup_attempts=2).discover()
        assert discovered["identity_state"] == "provisioned"
        assert transport.handshakes == 2


def test_discovery_exhausts_startup_timeout_retries_without_status_probe(tmp_path) -> None:
    class NeverReadyTransport(FakeTransport):
        def exchange(self, payload: str) -> str:
            self.commands.append(payload)
            if payload == "WHO_ARE_YOU_!":
                raise ProbeError(ProbeOutcome.TIMEOUT, "no response")
            raise AssertionError(f"status must not be probed after timeout: {payload}")

    with EdgeStore(tmp_path) as store:
        transport = NeverReadyTransport()
        with pytest.raises(ProbeError) as raised:
            ReadOnlyHardwareService(store, transport, startup_attempts=3).discover()
        assert raised.value.outcome is ProbeOutcome.TIMEOUT
        assert raised.value.evidence["attempt"] == 3
        assert transport.commands == ["WHO_ARE_YOU_!"] * 3


def test_discovery_retries_empty_identity_reads_after_transport_opens(tmp_path) -> None:
    class EmptyThenReadyTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.handshakes = 0

        def exchange(self, payload: str) -> str:
            self.commands.append(payload)
            if payload == "WHO_ARE_YOU_!":
                self.handshakes += 1
                if self.handshakes < 3:
                    return ""
            return super().exchange(payload)

    with EdgeStore(tmp_path) as store:
        transport = EmptyThenReadyTransport()
        discovered = ReadOnlyHardwareService(store, transport, startup_attempts=3).discover()
        assert discovered["identity_state"] == "provisioned"
        assert transport.handshakes == 3
        assert transport.commands[:3] == ["WHO_ARE_YOU_!"] * 3
        assert all("PULSE" not in command and "SET_OD_LED" not in command
                   for command in transport.commands)


@pytest.mark.parametrize(("failure", "outcome", "state"), [
    (ProbeError(ProbeOutcome.PERMISSION, "permission denied"), ProbeOutcome.PERMISSION, "degraded"),
    (ProbeError(ProbeOutcome.BUSY, "resource busy"), ProbeOutcome.BUSY, "degraded"),
    (ProbeError(ProbeOutcome.TIMEOUT, "no response"), ProbeOutcome.TIMEOUT, "disconnected"),
    (ProbeError(ProbeOutcome.MALFORMED, "malformed reply"), ProbeOutcome.MALFORMED, "ambiguous"),
    (ProbeError(ProbeOutcome.PROTOCOL, "unsupported protocol"), ProbeOutcome.PROTOCOL, "ambiguous"),
    (ProbeError(ProbeOutcome.STATUS, "status failure"), ProbeOutcome.STATUS, "ambiguous"),
])
def test_poll_projects_typed_probe_failures_without_registration(tmp_path, failure, outcome, state) -> None:
    class FailingTransport(FakeTransport):
        def exchange(self, payload: str) -> str:
            self.commands.append(payload)
            raise failure

    with EdgeStore(tmp_path) as store:
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"],
                  transport_factory=lambda port: FailingTransport())
        observation = store.hardware_observation()
        assert observation["probe_outcome"] == outcome.value
        assert observation["connection_state"] == state
        assert store.list_instruments() == []


def test_poll_projects_status_failure_from_a_malformed_status_reply(tmp_path) -> None:
    class StatusFailureTransport(FakeTransport):
        def exchange(self, payload: str) -> str:
            self.commands.append(payload)
            if payload == "WHO_ARE_YOU_!":
                return "MEV|2|MEV-001|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id=MEV-001"
            if payload == "HW_STATUS_!":
                return "HW|1|ERROR|STATUS|reason=fault"
            raise AssertionError(payload)

    with EdgeStore(tmp_path) as store:
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"],
                  transport_factory=lambda port: StatusFailureTransport())
        observation = store.hardware_observation()
        assert observation["probe_outcome"] == ProbeOutcome.MALFORMED.value
        assert observation["connection_state"] == "ambiguous"
        assert store.list_instruments() == []


def test_probe_error_evidence_is_bounded() -> None:
    error = ProbeError(ProbeOutcome.TIMEOUT, "timeout", evidence={"detail": "x" * 1000})
    assert error.outcome is ProbeOutcome.TIMEOUT
    assert len(error.evidence["detail"]) == 256


def test_parse_identity_preserves_invalid_identity_as_ambiguous() -> None:
    parsed = parse_identity("MEV|2|bad|frame|1|type=minievolver,fw=0.2,hw_proto=1,id=12345678901234567890123456789012")
    assert parsed.identity_state == IdentityState.INVALID
    assert not parsed.provisioned


def test_effective_device_state_distinguishes_reported_temperature_from_unreported_outputs() -> None:
    state = normalize_effective_device_state({"temp_control": "on", "pump_0": "off", "stir_state": "active"})
    assert state["temperature"] == {"effective_state": "active", "evidence": "protocol_verified"}
    assert state["pump"]["effective_state"] == "inactive"
    assert state["pump"]["channels"] == {"pump_0": "inactive"}
    assert state["pump"]["evidence"] == "protocol_verified"
    assert state["stir"]["effective_state"] == "active"
    assert state["stir"]["evidence"] == "protocol_verified"


class ProvisionTransport(FakeTransport):
    def __init__(self, device_id: str = "BLANK", owner_id: str = "BLANK", readback_id: str | None = None):
        super().__init__(device_id)
        self.owner_id, self.readback_id = owner_id, readback_id
        self.written = False

    def exchange(self, payload: str) -> str:
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            device_id = self.readback_id if self.written and self.readback_id is not None else self.device_id
            return f"MEV|2|{device_id}|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id={device_id},owner={self.owner_id}"
        if payload.startswith("PROVISION,"):
            _, device_id, owner = payload.removesuffix("_!").split(",")
            self.device_id, self.owner_id = device_id, owner
            self.written = True
            return f"MEV|2|{device_id}|1|PROVISION_ACK|id={device_id},owner={owner}|00"
        raise AssertionError(f"unexpected command: {payload}")


def test_provisioning_allows_blank_and_verifies_exact_readback(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = ProvisionTransport()
        service = HardwareService(store, transport, allow_physical=True, operator="alice")
        result = service.provision_identity(device_id="MEV-002", owner_id="lab", operator="alice", command_id="prov-1")
        assert result.verification == "protocol_verified"
        assert transport.commands.count("PROVISION,MEV-002,lab_!") == 1
        duplicate = service.provision_identity(device_id="MEV-002", owner_id="lab", operator="alice", command_id="prov-1")
        assert duplicate.as_json() == result.as_json()
        assert transport.commands.count("PROVISION,MEV-002,lab_!") == 1


@pytest.mark.parametrize("device_id", ["MEV-002", "12345678901234567890123456789012"])
def test_provisioning_refuses_valid_or_invalid_current_identity(tmp_path, device_id) -> None:
    with EdgeStore(tmp_path) as store:
        service = HardwareService(store, ProvisionTransport(device_id), allow_physical=True, operator="alice")
        with pytest.raises(HardwareUnavailableError):
            service.provision_identity(device_id="MEV-003", owner_id="lab", operator="alice")


def test_provisioning_readback_mismatch_is_not_success(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        service = HardwareService(store, ProvisionTransport(readback_id="MEV-WRONG"), allow_physical=True, operator="alice")
        with pytest.raises(HardwareUnavailableError, match="ambiguous|mismatch"):
            service.provision_identity(device_id="MEV-003", owner_id="lab", operator="alice")


def test_provisioning_timeout_reconciles_readback_without_resending(tmp_path) -> None:
    class TimeoutAfterWriteTransport(ProvisionTransport):
        def exchange(self, payload: str) -> str:
            if payload.startswith("PROVISION,"):
                self.commands.append(payload)
                _, device_id, owner = payload.removesuffix("_!").split(",")
                self.device_id, self.owner_id, self.written = device_id, owner, True
                raise ProbeError(ProbeOutcome.TIMEOUT, "provision ACK timed out")
            return super().exchange(payload)

    with EdgeStore(tmp_path) as store:
        transport = TimeoutAfterWriteTransport()
        result = HardwareService(store, transport, allow_physical=True, operator="alice").provision_identity(
            device_id="MEV-002", owner_id="lab", operator="alice", command_id="prov-timeout")
        assert result.verification == "protocol_verified"
        assert transport.commands.count("PROVISION,MEV-002,lab_!") == 1
        assert transport.commands == ["WHO_ARE_YOU_!", "PROVISION,MEV-002,lab_!", "WHO_ARE_YOU_!"]


def test_service_rejects_a_different_device_before_reading_sensors(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        service = ReadOnlyHardwareService(store, FakeTransport("MEV-001"))
        instrument = service.discover()
        service.transport = FakeTransport("MEV-002")
        with pytest.raises(HardwareUnavailableError, match="does not match"):
            service.capture_telemetry(instrument["id"])


def test_hardware_service_requires_an_explicit_safe_poll_interval() -> None:
    assert build_parser().parse_args(["--port", "/dev/ttyACM0"]).interval == 10.0


def test_hardware_polling_tolerates_missing_or_ambiguous_usb_candidates(tmp_path) -> None:
    """Unplug/replug is retried in-process instead of requiring a restart."""
    with EdgeStore(tmp_path) as store:
        poll_once(store, requested_port=None, discover=lambda _port: [])
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0", "/dev/ttyACM1"])
        assert store.list_instruments() == []
        assert store.hardware_observation()["connection_state"] == "ambiguous"
        assert store.hardware_observation()["identity_ambiguous"] is True


def test_hardware_poll_persists_disconnect_evidence_for_known_instrument(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        def transport(port: str) -> FakeTransport:
            result = FakeTransport("MEV-001")
            result.port = port
            return result
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"], transport_factory=transport)
        instrument_id = store.list_instruments()[0]["id"]
        poll_once(store, requested_port=None, discover=lambda _port: [], transport_factory=transport)
        observed = store.instrument(instrument_id)
        assert observed["connection_state"] == "disconnected"
        assert observed["transport_evidence"]["reason"] == "no_usb_candidates"
        assert store.hardware_observation()["transport"]["candidates"] == []


def test_hardware_poll_reconnects_a_provisioned_device_at_a_new_tty_path(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        def transport(port: str) -> FakeTransport:
            result = FakeTransport("MEV-001")
            result.port = port
            return result
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"], transport_factory=transport)
        first = store.list_instruments()[0]
        poll_once(store, requested_port=None, discover=lambda _port: [], transport_factory=transport)
        poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM1"], transport_factory=transport)
        second = store.list_instruments()[0]
        assert second["id"] == first["id"]
        assert second["transport"]["path"] == "/dev/ttyACM1"
        assert len(store.telemetry_after(f"instrument:{first['id']}:read_only_sensors")) == 2


def test_repeated_rescan_is_idempotent_for_registration_and_uses_read_only_wire_messages(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transports: list[FakeTransport] = []

        def transport(port: str) -> FakeTransport:
            result = FakeTransport("MEV-001")
            result.port = port
            transports.append(result)
            return result

        for _ in range(2):
            poll_once(store, requested_port=None, discover=lambda _port: ["/dev/ttyACM0"],
                      transport_factory=transport)

        assert len(store.list_instruments()) == 1
        instrument_id = store.list_instruments()[0]["id"]
        assert len(store.telemetry_after(f"instrument:{instrument_id}:read_only_sensors")) == 2
        assert transports[0].commands == [
            "WHO_ARE_YOU_!", "HW_STATUS_!", "WHO_ARE_YOU_!",
            "HW_READ_THERMISTOR,0_!", "HW_READ_PHOTODIODE,0_!",
            "HW_READ_THERMISTOR,1_!", "HW_READ_PHOTODIODE,1_!",
        ]
        assert transports[1].commands == transports[0].commands
        assert all(not command.startswith(("HW_PULSE_", "HW_SET_", "HW_SAFE_"))
                   for transport_instance in transports for command in transport_instance.commands)


class ActuatorTransport(FakeTransport):
    def exchange(self, payload: str) -> str:
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return "MEV|2|MEV-001|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id=MEV-001"
        if payload == "HW_SET_OD_LED,0,255_!": return "HW|1|OK|SET_OD_LED|channel=0,level=255"
        if payload == "HW_PULSE_PUMP,0,100_!": return "HW|1|OK|PULSE_PUMP|channel=0,duration_ms=100"
        if payload == "HW_SAFE_!": return "HW|1|OK|SAFE|outputs=off"
        raise AssertionError(f"unexpected command: {payload}")


def test_bound_read_only_command_resolves_current_binding_generation(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="sentinel", generation=1)
        result = HardwareService(store, FakeTransport()).command("get_status", "MEV-001", {})
        assert result.request_accepted is True


def test_unbound_physical_actuation_requires_positive_generation(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        with pytest.raises(EdgeStoreError, match="active positive controller generation"):
            HardwareService(store, ActuatorTransport(), allow_physical=True, operator="alice").command(
                "pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100})


def test_actuation_is_gated_and_bounds_are_checked_before_serial(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        transport = ActuatorTransport()
        service = HardwareService(store, transport)
        with pytest.raises(PermissionError):
            service.command("set_output", "MEV-001", {"output": "od_led", "channel": 0, "level": 1})
        store.bind(webui_controller_id="central", server_url="https://central", credential="sentinel", generation=1)
        with pytest.raises(ValueError):
            HardwareService(store, transport, allow_physical=True, operator="op").command(
                "pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 1001})
        assert transport.commands == []


def test_unbound_physical_actuation_requires_positive_generation(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        with pytest.raises(EdgeStoreError, match="positive controller generation"):
            HardwareService(store, ActuatorTransport(), allow_physical=True, operator="alice").command(
                "pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100})


def test_out_of_range_pump_is_rejected_before_serial(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = ActuatorTransport()
        with pytest.raises(ValueError, match="between 0 and 5"):
            HardwareService(store, transport, allow_physical=True, operator="alice").command(
                "pulse_pump", "MEV-001", {"channel": 99, "duration_ms": 100})
        assert transport.commands == []


def test_actuator_result_distinguishes_protocol_ack_and_is_idempotent(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="sentinel", generation=1)
        transport = ActuatorTransport()
        service = HardwareService(store, transport, allow_physical=True, operator="alice")
        first = service.command("set_output", "MEV-001", {"output": "od_led", "channel": 0, "level": 255}, command_id="led-1")
        second = service.command("set_output", "MEV-001", {"output": "od_led", "channel": 0, "level": 255}, command_id="led-1")
        assert first.verification == "protocol_verified"
        assert first.request_accepted is True
        assert second.as_json() == first.as_json()
        assert transport.commands.count("HW_SET_OD_LED,0,255_!") == 1


def test_pump_is_explicitly_non_retryable(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="sentinel", generation=1)
        result = HardwareService(store, ActuatorTransport(), allow_physical=True, operator="alice").command(
            "pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100}, command_id="pump-1")
        assert result.retryable is False


def test_actuator_lease_token_owner_and_generation_are_checked_at_command_boundary(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=4)
        store.set_control_lease(lease_token="token-a", owner="alice", generation=4,
                                expires_at="2099-01-01T00:00:00+00:00")
        service = HardwareService(store, ActuatorTransport(), allow_physical=True, operator="alice")
        with pytest.raises(LeaseValidationError):
            service.command("pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100}, command_id="lease-missing")
        with pytest.raises(LeaseValidationError):
            service.command("pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100}, command_id="lease-wrong", lease_token="wrong", lease_owner="alice")
        result = service.command("pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100}, command_id="lease-ok", lease_token="token-a", lease_owner="alice")
        assert result.request_accepted is True


class FailingActuatorTransport(ActuatorTransport):
    def exchange(self, payload: str) -> str:
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return "MEV|2|MEV-001|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=1,id=MEV-001"
        if payload == "HW_PULSE_PUMP,0,100_!":
            raise HardwareUnavailableError("fake transport disconnected")
        raise AssertionError(f"unexpected command: {payload}")


def test_actuator_transport_failure_is_terminal_and_records_component_fault(tmp_path) -> None:
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="sentinel", generation=1)
        result = HardwareService(store, FailingActuatorTransport(), allow_physical=True, operator="alice").command(
            "pulse_pump", "MEV-001", {"channel": 0, "duration_ms": 100}, command_id="pump-failed")
        assert result.request_accepted is False
        assert result.verification == "protocol_failed"
        assert result.retryable is False
        observation = store.hardware_observation()
        assert observation["component"] == "pump"
        assert observation["component_state"] == "fault"


def test_pump_state_maps_fault_and_unavailable_distinctly() -> None:
    assert normalize_effective_device_state({"pump_state": "fault"})["pump"]["effective_state"] == "fault"
    assert normalize_effective_device_state({"pump_state": "unavailable"})["pump"]["effective_state"] == "unavailable"
