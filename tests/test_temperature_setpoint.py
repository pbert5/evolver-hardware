from __future__ import annotations

import pytest

from evolver_hardware import EdgeStore
from evolver_hardware.hardware import HardwareService, HardwareUnavailableError, ProbeError
from evolver_hardware.store import LeaseValidationError, StaleGenerationError


class SetpointTransport:
    port = "/dev/pty-fake"

    def __init__(self) -> None:
        self.opened = False
        self.commands: list[str] = []
        self.temperature_reply = "HW|2|OK|TEMP|applied=1"

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def exchange(self, payload: str) -> str:
        assert self.opened
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return "MEV|2|MEV-81|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto=2,id=MEV-81"
        if payload == "HW_STATUS_!":
            return "HW|2|OK|STATUS|sleeves=2,pumps=6"
        if payload == "HW_SAFE_!":
            return "HW|2|OK|SAFE|stopped=1"
        if payload.startswith("TEMP|2|"):
            return self.temperature_reply
        raise AssertionError(payload)


def _calibrations(instrument: dict) -> list[dict[str, object]]:
    return [
        {"vial_position_id": position["id"], "artifact_id": f"cal-{index}",
         "artifact_digest": f"sha256:{index}", "calibration_type": "temperature",
         "status": "valid", "slope": 1.0, "intercept": 0.0}
        for index, position in enumerate(instrument["vial_positions"])
    ]


def test_setpoint_maps_each_vial_to_immutable_v2_raw_target_and_refreshes(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)

        result = service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)

        assert result.request_accepted is True
        assert "TEMP|2|0|32_!" in transport.commands
        assert "TEMP|2|1|32_!" in transport.commands
        service.refresh_temperature_setpoints()
        assert transport.commands.count("TEMP|2|0|32_!") == 2
        assert transport.commands.count("TEMP|2|1|32_!") == 2


def test_refresh_is_fenced_and_durably_correlated(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        service.refresh_temperature_setpoints()
        refresh_id = service.last_temperature_refresh_command_ids[0]
        record = store.inspect_command(refresh_id)
        assert record is not None
        assert record["status"] == "completed"
        assert record["operator"] == "ash"
        assert record["requested_device"] == instrument["device_identity"]
        service.refresh_temperature_setpoints()
        assert service.last_temperature_refresh_command_ids[-1] != refresh_id
        before = list(transport.commands)
        store.release_local_commissioning_lease("ash")

        with pytest.raises(LeaseValidationError, match="lease"):
            service.refresh_temperature_setpoints()
        assert transport.commands == before

def test_refresh_rejects_generation_change_before_serial_io(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        before = list(transport.commands)
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=2)

        with pytest.raises(StaleGenerationError, match="generation"):
            service.refresh_temperature_setpoints()
        assert transport.commands == before


class FailingRefreshTransport(SetpointTransport):
    def exchange(self, payload: str) -> str:
        if payload.startswith("TEMP|2|"):
            raise HardwareUnavailableError("refresh transport failed")
        return super().exchange(payload)


def test_refresh_failure_handler_records_fault_and_safe_stops(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        failing_transport = FailingRefreshTransport()
        service.transport = failing_transport

        result = service.handle_temperature_refresh_failure(HardwareUnavailableError("refresh transport failed"))

        assert result[0].request_accepted is True
        assert "HW_SAFE_!" in failing_transport.commands
        observation = store.hardware_observation()
        assert observation["component_state"] == "fault"
        assert observation["fault"]["kind"] == "temperature_refresh"
        assert store.inspect_command(result[0].command_id)["status"] == "completed"


def test_refresh_transport_failure_is_journaled_before_boundary_handling(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        service.transport = FailingRefreshTransport()

        with pytest.raises(HardwareUnavailableError, match="refresh"):
            service.refresh_temperature_setpoints()
        refresh_id = service.last_temperature_refresh_command_ids[-1]
        record = store.inspect_command(refresh_id)
        assert record is not None
        assert record["status"] == "completed"
        assert record["acknowledgement"]["request_accepted"] is False


def test_setpoint_rejects_missing_or_out_of_range_calibration_before_serial_io(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        commands_before = list(transport.commands)
        with pytest.raises(ValueError, match="exactly cover"):
            service.set_temperature(instrument["id"], {"temperature_c": 32.0, "calibrations": []},
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        assert transport.commands == commands_before
        calibration = _calibrations(instrument)
        calibration[0]["intercept"] = 32.1
        with pytest.raises(ValueError, match="out-of-range"):
            service.set_temperature(instrument["id"], {"temperature_c": 32.0, "calibrations": calibration},
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        assert transport.commands == commands_before


@pytest.mark.parametrize("raw", [1, 65535])
def test_setpoint_accepts_raw_wire_domain_boundaries(tmp_path, raw):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        calibrations = _calibrations(instrument)
        calibrations[0]["intercept"] = 32.0 - raw
        result = service.set_temperature(instrument["id"], {"temperature_c": 32.0,
            "calibrations": calibrations}, operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        assert result.request_accepted is True
        assert f"TEMP|2|0|{raw}_!" in transport.commands


def test_legacy_temperature_ack_is_rejected_fail_closed(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        transport.temperature_reply = "HW|2|OK|TEMP_V2|applied=1"
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        with pytest.raises(ProbeError, match="TEMP"):
            service.set_temperature(instrument["id"], {"temperature_c": 32.0,
                "calibrations": _calibrations(instrument)}, operator="ash", lease_owner="ash",
                lease_token=lease["token"], controller_generation=1, require_lease=True)
