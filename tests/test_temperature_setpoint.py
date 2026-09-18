from __future__ import annotations

import pytest

from evolver_hardware import EdgeStore
from evolver_hardware.hardware import HardwareService


class SetpointTransport:
    port = "/dev/pty-fake"

    def __init__(self) -> None:
        self.opened = False
        self.commands: list[str] = []

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
        if payload.startswith("HW_TEMP_V2,"):
            return "HW|2|OK|TEMP_V2|applied=1"
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
        assert "HW_TEMP_V2,0,32_!" in transport.commands
        assert "HW_TEMP_V2,1,32_!" in transport.commands
        service.refresh_temperature_setpoints()
        assert transport.commands.count("HW_TEMP_V2,0,32_!") == 2
        assert transport.commands.count("HW_TEMP_V2,1,32_!") == 2


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
        calibration[0]["slope"] = 0.1
        with pytest.raises(ValueError, match="out-of-range"):
            service.set_temperature(instrument["id"], {"temperature_c": 32.0, "calibrations": calibration},
                                    operator="ash", lease_owner="ash", lease_token="unused",
                                    controller_generation=1, require_lease=True)
        assert transport.commands == commands_before
