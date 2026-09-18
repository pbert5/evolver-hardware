from __future__ import annotations

import pytest

from evolver_hardware import EdgeStore
from evolver_hardware.bundle import calibration_artifact_digest
from evolver_hardware.hardware import (HardwareService, HardwareUnavailableError, ProbeError,
                                        TemperatureSetpoint, _temperature_frame, _temperature_reply)
from evolver_hardware.hardware_ipc import HardwareIPCServer, request
from evolver_hardware.store import LeaseValidationError, StaleGenerationError


class SetpointTransport:
    port = "/dev/pty-fake"

    def __init__(self) -> None:
        self.opened = False
        self.commands: list[str] = []
        self.temperature_reply: str | None = None
        self.hw_protocol = 2

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def usb_hardware_fingerprint(self) -> dict[str, str]:
        return {"scheme": "fake-usb-v1", "usb_serial": "FAKE-81"}

    def exchange(self, payload: str) -> str:
        assert self.opened
        self.commands.append(payload)
        if payload == "WHO_ARE_YOU_!":
            return f"MEV|2|MEV-81|1|HELLO|type=minievolver,proto=2,fw=0.2,hw_proto={self.hw_protocol},id=MEV-81"
        if payload == "HW_STATUS_!":
            return "HW|2|OK|STATUS|sleeves=2,pumps=6"
        if payload == "HW_SAFE_!":
            return "HW|2|OK|SAFE|stopped=1"
        if payload.startswith("TEMP|2|"):
            if self.temperature_reply is not None:
                return self.temperature_reply
            fields = payload.removesuffix("_!").split("|")
            assert fields[:3] == ["TEMP", "2", "SET"]
            assert len(fields) == 9
            return f"TEMP|2|ACK|{fields[3]}|SET|channel={fields[4]},raw={fields[5]},ceiling=64"
        raise AssertionError(payload)


def _calibrations(store: EdgeStore, instrument: dict, *, intercept: float = 0.0) -> list[dict[str, object]]:
    result = []
    for index, position in enumerate(instrument["vial_positions"]):
        artifact = {"id": f"cal-{index}", "instrument_id": instrument["id"],
                    "vial_position_id": position["id"], "calibration_type": "temperature",
                    "method": "temperature_linear_v1", "method_version": "1",
                    "coefficients": {"slope": 1.0, "intercept": intercept},
                    "calibration_range": {"raw_min": 1, "raw_max": 65535,
                                          "reference_min": -100.0, "reference_max": 100.0},
                    "hardware_fingerprint": instrument["hardware_fingerprint"]}
        artifact["artifact_digest"] = calibration_artifact_digest(artifact)
        store.put_calibration_artifact(artifact)
        result.append({"vial_position_id": position["id"], "artifact_id": artifact["id"],
                       "artifact_digest": artifact["artifact_digest"], "calibration_type": "temperature",
                       "status": "valid",
                       "hardware_fingerprint": instrument["hardware_fingerprint"]})
    return result


def _single_parameters(instrument: dict, calibration: dict[str, object], *, target: float = 32.0,
                       raw: int | None = None, channel: int = 0) -> dict[str, object]:
    return {"vial_position_id": instrument["vial_positions"][channel]["id"], "channel": channel,
            "raw_target_adc": round((target - float(calibration.get("intercept", 0.0))) /
                                     float(calibration.get("slope", 1.0))) if raw is None else raw,
            "temperature_c": target, "calibration": calibration}


def test_setpoint_maps_each_vial_to_immutable_v2_raw_target_and_refreshes(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)

        calibrations = _calibrations(store, instrument)
        result = service.set_temperature(instrument["id"], _single_parameters(instrument, calibrations[0]), operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)

        assert result.request_accepted is True
        setpoint_frames = [command for command in transport.commands if command.startswith("TEMP|2|")]
        assert all(len(frame.removesuffix("_!").split("|")) == 9 for frame in setpoint_frames)
        assert [frame.removesuffix("_!").split("|")[4:6] for frame in setpoint_frames] == [["0", "32"]]
        assert [int(frame.removesuffix("_!").split("|")[3]) for frame in setpoint_frames] == [1]
        assert all(frame.removesuffix("_!").split("|")[6] == "ash" and frame.removesuffix("_!").split("|")[7].isdigit() and frame.removesuffix("_!").split("|")[8] == "1"
                   for frame in setpoint_frames)
        service.refresh_temperature_setpoints()
        all_frames = [command for command in transport.commands if command.startswith("TEMP|2|")]
        assert [int(frame.removesuffix("_!").split("|")[3]) for frame in all_frames] == [1, 2]
        assert all(frame.removesuffix("_!").split("|")[6] == "ash" and frame.removesuffix("_!").split("|")[7].isdigit() and frame.removesuffix("_!").split("|")[8] == "1"
                   for frame in all_frames)


def test_setpoint_consumes_one_canonical_vial_target_and_preserves_other_channel(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        artifacts = _calibrations(store, instrument)
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)

        result = service.set_temperature(instrument["id"], {
            "vial_position_id": instrument["vial_positions"][0]["id"], "channel": 0,
            "raw_target_adc": 32, "temperature_c": 32.0, "calibration": artifacts[0]},
            operator="ash", lease_owner="ash", lease_token=lease["token"],
            controller_generation=1, require_lease=True)

        assert result.request_accepted is True
        assert [frame.removesuffix("_!").split("|")[4:6]
                for frame in transport.commands if frame.startswith("TEMP|2|")] == [["0", "32"]]
        assert list(service._frozen_temperature_setpoints) == [(instrument["id"], 0)]

        service.set_temperature(instrument["id"], _single_parameters(instrument, artifacts[1], target=40.0, raw=40, channel=1),
                                operator="ash", lease_owner="ash", lease_token=lease["token"],
                                controller_generation=1, require_lease=True)
        assert set(service._frozen_temperature_setpoints) == {(instrument["id"], 0), (instrument["id"], 1)}
        service.refresh_temperature_setpoints()
        refreshed = [frame.removesuffix("_!").split("|")[4:6]
                     for frame in transport.commands if frame.startswith("TEMP|2|")]
        assert refreshed[-2:] == [["0", "32"], ["1", "40"]]


def test_controller_shaped_ipc_request_reaches_one_secure_temp_frame(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        calibration = _calibrations(store, instrument)[0]
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        path = tmp_path / "hardware.sock"
        server = HardwareIPCServer(store, service, path)
        server.start()
        try:
            result = request(path, {"operation": "set_temperature", "physical": True,
                "target_identity": instrument["device_identity"], "operator": "ash",
                "lease_token": lease["token"], "controller_generation": 1,
                "parameters": {"vial_position_id": instrument["vial_positions"][0]["id"],
                    "channel": 0, "raw_target_adc": 32, "temperature_c": 32.0,
                    "calibration": calibration}})
            assert result["request_accepted"] is True
            frames = [frame for frame in transport.commands if frame.startswith("TEMP|2|")]
            assert len(frames) == 1
            fields = frames[0].removesuffix("_!").split("|")
            assert fields[:7] == ["TEMP", "2", "SET", "1", "0", "32", "ash"]
            assert fields[7].isdigit() and fields[8] == "1"
        finally:
            server.close()


def test_refresh_failure_safe_stops_each_target_once_when_channels_are_active(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        calibrations = _calibrations(store, instrument)
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        for channel in (0, 1):
            service.set_temperature(instrument["id"], _single_parameters(instrument, calibrations[channel],
                target=32.0 + channel, raw=32 + channel, channel=channel), operator="ash", lease_owner="ash",
                lease_token=lease["token"], controller_generation=1, require_lease=True)

        service.handle_temperature_refresh_failure(HardwareUnavailableError("refresh failed"))
        assert transport.commands.count("HW_SAFE_!") == 1


def test_capability_sink_separates_raw_wire_domain_from_firmware_pid_ceiling(tmp_path):
    with EdgeStore(tmp_path) as store:
        service = HardwareService(store, SetpointTransport(), allow_physical=True)
        capability = service.discover()["capabilities"]["temperature_setpoint"]

        assert capability["raw_pid_target"] == {"minimum": 1, "maximum": 65535}
        assert capability["firmware_pid_ceiling"] == 64


def test_refresh_is_fenced_and_durably_correlated(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        calibration = _calibrations(store, instrument)[0]
        service.set_temperature(instrument["id"], _single_parameters(instrument, calibration), operator="ash", lease_owner="ash",
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
        calibration = _calibrations(store, instrument)[0]
        service.set_temperature(instrument["id"], _single_parameters(instrument, calibration), operator="ash", lease_owner="ash",
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
        calibration = _calibrations(store, instrument)[0]
        service.set_temperature(instrument["id"], _single_parameters(instrument, calibration), operator="ash", lease_owner="ash",
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
        service.set_temperature(instrument["id"], _single_parameters(instrument, _calibrations(store, instrument)[0]), operator="ash", lease_owner="ash",
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
        with pytest.raises(ValueError, match="vial_position_id"):
            service.set_temperature(instrument["id"], {"temperature_c": 32.0},
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        assert transport.commands == commands_before
        calibration = _calibrations(store, instrument)[0]
        calibration["artifact_digest"] = "sha256:wrong"
        with pytest.raises(ValueError, match="immutable authority"):
            service.set_temperature(instrument["id"], _single_parameters(instrument, calibration),
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
        calibrations = _calibrations(store, instrument, intercept=32.0 - raw)
        result = service.set_temperature(instrument["id"], _single_parameters(instrument, calibrations[0], raw=raw), operator="ash", lease_owner="ash",
            lease_token=lease["token"], controller_generation=1, require_lease=True)
        assert result.request_accepted is True
        assert any(frame.removesuffix("_!").split("|")[4:6] == ["0", str(raw)] for frame in transport.commands if frame.startswith("TEMP|2|"))


def test_legacy_temperature_ack_is_rejected_fail_closed(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        transport.temperature_reply = "HW|2|OK|TEMP_V2|applied=1"
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        with pytest.raises(ProbeError, match="TEMP"):
            service.set_temperature(instrument["id"], _single_parameters(instrument, _calibrations(store, instrument)[0]), operator="ash", lease_owner="ash",
                lease_token=lease["token"], controller_generation=1, require_lease=True)


def test_v1_temperature_device_is_rejected_before_any_temp_frame(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        transport.hw_protocol = 1
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        lease = store.acquire_local_commissioning_lease("ash", ttl_seconds=60, controller_generation=1)
        with pytest.raises(HardwareUnavailableError, match="protocol v2"):
            service.set_temperature(instrument["id"], _single_parameters(instrument, _calibrations(store, instrument)[0]), operator="ash", lease_owner="ash",
                lease_token=lease["token"], controller_generation=1, require_lease=True)
        assert not any(command.startswith("TEMP|") for command in transport.commands)


@pytest.mark.parametrize("correlation", [0, -1, 0x100000000])
def test_temperature_correlation_rejects_values_outside_uint32_domain(correlation):
    with pytest.raises(ProbeError, match="correlation"):
        _temperature_reply("TEMP|2|ACK|1|SET|channel=0,raw=32", correlation=correlation,
                           operation="SET", channel=0, raw=32)


def test_temperature_frame_rejects_unsupported_channel():
    item = TemperatureSetpoint(2, "vial-2", 32.0, 32, "cal", "digest")
    with pytest.raises(ValueError, match="channel"):
        _temperature_frame(correlation=1, item=item, owner="ash", lease=1, generation=1)


def test_temperature_rejects_missing_or_mismatched_calibration_binding_before_serial_io(tmp_path):
    with EdgeStore(tmp_path) as store:
        store.bind(webui_controller_id="central", server_url="https://central", credential="secret", generation=1)
        transport = SetpointTransport()
        service = HardwareService(store, transport, allow_physical=True)
        instrument = service.discover()
        calibrations = _calibrations(store, instrument)
        before = list(transport.commands)
        missing = dict(calibrations[0], hardware_fingerprint=None)
        with pytest.raises(ValueError, match="fingerprint binding"):
            service.set_temperature(instrument["id"], _single_parameters(instrument, missing),
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        mismatched = dict(calibrations[0], hardware_fingerprint={"scheme": "other"})
        with pytest.raises(ValueError, match="fingerprint binding"):
            service.set_temperature(instrument["id"], _single_parameters(instrument, mismatched),
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        assert transport.commands == before


@pytest.mark.parametrize("reply", [
    "TEMP|2|ERR|7|TEMP_ERR_REPLAY|reason=replay",
    "TEMP|2|ERR|6|TEMP_ERR_AUTHORITY|reason=stale_generation",
])
def test_temperature_firmware_replay_and_stale_replies_are_rejected(reply):
    with pytest.raises(ProbeError, match="temperature"):
        _temperature_reply(reply, correlation=7, operation="SET", channel=0, raw=32)
