from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest
import subprocess
import sys

from evolver_hardware import firmware
from evolver_hardware.firmware import main


def test_firmware_toolchain_closure_requires_board_selected_bossac(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_evolver_firmware", "tools/build_evolver_firmware.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    validate_upload_closure = module.validate_upload_closure

    core = tmp_path / "packages/SparkFun/hardware/samd/1.8.13"
    core.mkdir(parents=True)
    (core / "boards.txt").write_text(
        "samd21_mini.upload.tool=bossac\n"
        "samd21_mini.upload.use_1200bps_touch=true\n", encoding="utf-8")
    (core / "platform.txt").write_text(
        "tools.bossac.path={runtime.tools.bossac-1.7.0-arduino3.path}\n", encoding="utf-8")
    toolchain = {"board_core": "SparkFun:samd@1.8.13", "dependencies": [
        {"id": "arduino:bossac", "version": "1.7.0-arduino3"},
        {"id": "arduino:bossac", "version": "1.8.0-48-gb176eee"},
    ]}
    with pytest.raises(SystemExit, match="missing pinned upload tools"):
        validate_upload_closure(tmp_path, toolchain)
    tool_dir = tmp_path / "packages/arduino/tools/bossac"
    selected = tool_dir / "1.7.0-arduino3"
    (selected / "bin").mkdir(parents=True)
    (selected / "bin/bossac").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (selected / "bin/bossac").chmod(0o755)
    (selected / "provenance.json").write_text(json.dumps({
        "source_archive_sha256": "a" * 64,
        "executable_sha256": hashlib.sha256((selected / "bin/bossac").read_bytes()).hexdigest(),
    }), encoding="utf-8")
    (tool_dir / "1.8.0-48-gb176eee").mkdir(parents=True)
    toolchain["board_uploader"] = {
        "version": "1.7.0-arduino3",
        "linux_x86_64_archive_sha256": "a" * 64,
    }
    validate_upload_closure(tmp_path, toolchain)


def test_firmware_toolchain_closure_rejects_tampered_selected_bossac(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_evolver_firmware", "tools/build_evolver_firmware.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

    core = tmp_path / "packages/SparkFun/hardware/samd/1.8.13"
    core.mkdir(parents=True)
    (core / "boards.txt").write_text("samd21_mini.upload.tool=bossac\n"
                                     "samd21_mini.upload.use_1200bps_touch=true\n", encoding="utf-8")
    (core / "platform.txt").write_text(
        "tools.bossac.path={runtime.tools.bossac-1.7.0-arduino3.path}\n", encoding="utf-8")
    selected = tmp_path / "packages/arduino/tools/bossac/1.7.0-arduino3/bin/bossac"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"pinned bossac")
    selected.chmod(0o755)
    (selected.parent.parent / "provenance.json").write_text(json.dumps({
        "source_archive_sha256": "b" * 64,
        "executable_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    (tmp_path / "packages/arduino/tools/bossac/1.8.0-48-gb176eee").mkdir(parents=True)
    selected.write_bytes(b"tampered bossac")

    with pytest.raises(SystemExit, match="executable provenance mismatch"):
        module.validate_upload_closure(tmp_path, {
            "board_core": "SparkFun:samd@1.8.13",
            "dependencies": [
                {"id": "arduino:bossac", "version": "1.7.0-arduino3"},
                {"id": "arduino:bossac", "version": "1.8.0-48-gb176eee"},
            ],
            "board_uploader": {
                "version": "1.7.0-arduino3",
                "linux_x86_64_archive_sha256": "b" * 64,
            },
        })


def test_release_owns_exact_nixos_uploader_provenance() -> None:
    source = Path("nix/evolver-firmware-uploader.nix").read_text(encoding="utf-8")
    assert "bossac-${version}-linux64.tar.gz" in source
    assert "autoPatchelfHook" in source
    assert "1ae54999c1f97234a5c603eb99ad39313b11746a4ca517269a9285afa05f9100" in source
    assert "nix_transformation" in source


def test_firmware_config_records_board_uploader_provenance() -> None:
    config = json.loads(Path("applications/evolver/firmware/build-config.json").read_text(encoding="utf-8"))
    uploader = config["toolchain"]["board_uploader"]
    assert uploader["version"] == "1.7.0-arduino3"
    assert uploader["nixos_packaging"] == "nix/evolver-firmware-uploader.nix"


def test_release_builder_rejects_false_firmware_source_provenance(tmp_path: Path) -> None:
    x86 = tmp_path / "x86.tar.gz"; firmware = tmp_path / "firmware.bin"; bad = tmp_path / "firmware-manifest.json"
    x86.write_bytes(b"x86"); firmware.write_bytes(b"firmware")
    bad.write_text(json.dumps({"source_commit": "952a6fd6f9b07e18a70d1c76bc8ecbfc3538ea6c"}), encoding="utf-8")
    result = subprocess.run([sys.executable, "tools/build_evolver_release.py", "--output", str(tmp_path / "published"),
                             "--version", "bad", "--git-revision", "a" * 40, "--artifact", f"linux-x86_64={x86}",
                             "--firmware", str(firmware), "--firmware-manifest", str(bad)], text=True, capture_output=True)
    assert result.returncode != 0
    assert "authoritative repaired revision" in result.stderr


def test_firmware_verify_is_local_and_deterministic(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "firmware.bin"
    artifact.write_bytes(b"offline firmware")
    digest = hashlib.sha256(b"offline firmware").hexdigest()
    assert main(["verify", "--artifact", str(artifact), "--sha256", digest]) == 0
    assert capsys.readouterr().out.strip() == digest


def test_firmware_verify_requires_and_uses_release_digest_file(tmp_path: Path, monkeypatch, capsys) -> None:
    artifact = tmp_path / "firmware.bin"
    artifact.write_bytes(b"offline firmware")
    digest_file = tmp_path / "sha256"
    digest_file.write_text(hashlib.sha256(artifact.read_bytes()).hexdigest() + "\n", encoding="utf-8")
    monkeypatch.setenv("EVOLVER_FIRMWARE_SHA256_FILE", str(digest_file))
    assert main(["verify", "--artifact", str(artifact)]) == 0
    assert capsys.readouterr().out.strip() == digest_file.read_text().strip()
    digest_file.write_text("0" * 64, encoding="utf-8")
    assert main(["verify", "--artifact", str(artifact)]) == 2
    assert "SHA-256 mismatch" in capsys.readouterr().out


def test_physical_upload_requires_explicit_operator_gate(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/arduino-cli")
    with pytest.raises(SystemExit, match="--physical"):
        main(["upload", "--port", "/dev/ttyACM0"])


def _runtime_toolchain(root: Path) -> tuple[Path, Path, Path]:
    toolchain = root / "firmware-toolchain"
    cli = toolchain / "bin/arduino-cli"
    data = toolchain / "arduino-data"
    config = toolchain / "arduino-cli.yaml"
    cli.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    config.write_text("board_manager:\n  additional_urls: []\n", encoding="utf-8")
    (toolchain / "PROVENANCE.json").write_text(json.dumps({
        "format": 1, "offline": True, "variant": "linux-x86_64-glibc",
        "arduino_cli": {"version": "1.1.1", "path": "firmware-toolchain/bin/arduino-cli"},
    }), encoding="utf-8")
    return cli, data, config


def test_firmware_upload_uses_release_owned_toolchain_and_provenance(tmp_path: Path, monkeypatch, capsys) -> None:
    cli, data, config = _runtime_toolchain(tmp_path)
    artifact = tmp_path / "firmware.bin"
    artifact.write_bytes(b"offline firmware")
    monkeypatch.setenv("EVOLVER_FIRMWARE_TOOLCHAIN_ROOT", str(tmp_path / "firmware-toolchain"))
    monkeypatch.setenv("EVOLVER_FIRMWARE_SHA256", hashlib.sha256(b"offline firmware").hexdigest())
    monkeypatch.setenv("PATH", str(tmp_path / "does-not-exist"))
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **kwargs: calls.append((command, kwargs)))

    assert main(["upload", "--physical", "--operator", "test", "--port", "/dev/fake",
                 "--artifact", str(artifact), "--state-root", str(tmp_path / "state")]) == 0
    command, kwargs = calls[0]
    assert command == [str(cli), "upload", "--fqbn", "SparkFun:samd:samd21_mini",
                       "--input-file", str(artifact), "--port", "/dev/fake"]
    assert kwargs["env"]["ARDUINO_DIRECTORIES_DATA"] == str(data)
    assert kwargs["env"]["ARDUINO_DIRECTORIES_USER"] == str(data.parent / "arduino-libraries")
    assert kwargs["env"]["ARDUINO_CONFIG_FILE"] == str(config)
    assert "firmware upload complete" in capsys.readouterr().out


def test_firmware_upload_accepts_explicit_platform_paths(tmp_path: Path, monkeypatch) -> None:
    cli, data, config = _runtime_toolchain(tmp_path)
    artifact = tmp_path / "firmware.bin"
    artifact.write_bytes(b"firmware")
    for name, value in (("EVOLVER_ARDUINO_CLI", cli), ("EVOLVER_ARDUINO_DATA_DIR", data),
                        ("EVOLVER_ARDUINO_CONFIG_FILE", config)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("EVOLVER_FIRMWARE_SHA256", hashlib.sha256(b"firmware").hexdigest())
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: None)
    assert main(["upload", "--physical", "--operator", "test", "--port", "/dev/fake",
                 "--artifact", str(artifact), "--state-root", str(tmp_path / "state")]) == 0


def test_firmware_upload_fails_closed_without_immutable_toolchain(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "firmware.bin"
    artifact.write_bytes(b"firmware")
    monkeypatch.delenv("EVOLVER_FIRMWARE_TOOLCHAIN_ROOT", raising=False)
    for name in ("EVOLVER_ARDUINO_CLI", "EVOLVER_ARDUINO_DATA_DIR", "EVOLVER_ARDUINO_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="immutable Arduino toolchain"):
        main(["upload", "--physical", "--operator", "test", "--port", "/dev/fake",
              "--artifact", str(artifact), "--state-root", str(tmp_path / "state")])


def _preflight_toolchain(root: Path, *, path: str | None = None) -> tuple[Path, Path, dict]:
    toolchain = root / "firmware-toolchain"
    cli = toolchain / "bin/arduino-cli"
    data = toolchain / "arduino-data"
    config = toolchain / "arduino-cli.yaml"
    cli.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    config.write_text("directories:\n  data: arduino-data\n", encoding="utf-8")
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)

    store_root = root / "nix-store-bossac"
    store_root.mkdir()
    store_executable = store_root / "bin/bossac"
    store_executable.parent.mkdir()
    store_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    store_executable.chmod(0o755)
    closure = toolchain / "nix-closures/bossac.closure"
    closure.parent.mkdir()
    closure.write_bytes(b"nix closure")
    gc_root = toolchain / "nix-roots/bossac"
    gc_root.parent.mkdir()
    gc_root.symlink_to(store_root)

    expected = data / firmware.CANONICAL_BOSSA_RELATIVE_PATH
    expected.parent.mkdir(parents=True)
    wrapper = f"#!/bin/sh\nexec {store_executable} \"$@\"\n"
    expected.write_text(wrapper, encoding="utf-8")
    expected.chmod(0o755)
    platform = data / "packages/SparkFun/hardware/samd/1.8.13/platform.txt"
    platform.parent.mkdir(parents=True)
    platform.write_text('tools.bossac.upload.pattern="{path}/{cmd}"\n', encoding="utf-8")

    provenance = {
        "format": 1,
        "offline": True,
        "variant": "linux-x86_64-nixos",
        "arduino_cli": {"version": "1.1.1"},
        "bossac": {
            "delivery": "nix-store-export-v1",
            "store_root": str(store_root),
            "store_executable": str(store_executable),
            "store_executable_sha256": hashlib.sha256(store_executable.read_bytes()).hexdigest(),
            "closure_paths": [str(store_root)],
            "closure_artifact": "toolchain/nix-closures/bossac.closure",
            "closure_sha256": hashlib.sha256(closure.read_bytes()).hexdigest(),
            "path": path or firmware.CANONICAL_BOSSA_PROVENANCE_PATH,
            "wrapper_sha256": hashlib.sha256(wrapper.encode()).hexdigest(),
        },
    }
    (toolchain / "PROVENANCE.json").write_text(json.dumps(provenance), encoding="utf-8")
    return cli, expected, provenance


def test_firmware_preflight_uses_official_arduino_environment_and_is_read_only(tmp_path: Path, monkeypatch, capsys) -> None:
    cli, expected, provenance = _preflight_toolchain(tmp_path)
    monkeypatch.setenv("EVOLVER_FIRMWARE_TOOLCHAIN_ROOT", str(tmp_path / "firmware-toolchain"))
    properties = f"tools.bossac.path={expected.parent}\ntools.bossac.cmd=bossac\n"
    calls = []

    def read_only(command, *, env=None):
        calls.append((command, env))
        if command[:3] == ["nix-store", "--query", "--requisites"]:
            stdout = f"{provenance['bossac']['store_root']}\n"
        elif command[-3:] == ["config", "dump", "--verbose"]:
            toolchain = tmp_path / "firmware-toolchain"
            stdout = (f"directories:\n  data: {toolchain / 'arduino-data'}\n"
                      f"  user: {toolchain / 'arduino-libraries'}\n"
                      f"  downloads: {toolchain / 'arduino-data/staging'}\n")
        elif command[:2] == [str(cli), "--config-file"]:
            stdout = properties
        elif command[-1:] == ["--help"]:
            stdout = "Basic Open Source SAM-BA Application (BOSSA) Version 1.7.0\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(firmware, "_run_read_only", read_only)
    assert main(["preflight"]) == 0
    output = capsys.readouterr().out
    assert f"ARDUINO_BOSSAC_EXECUTABLE={expected}" in output
    cli_call, env = next((call for call in calls if call[0][0] == str(cli) and "board" in call[0]), (None, None))
    assert cli_call == [str(cli), "--config-file", str(tmp_path / "firmware-toolchain/arduino-cli.yaml"),
                        "board", "details", "--fqbn", firmware.FQBN, "--show-properties=expanded"]
    assert env["ARDUINO_DIRECTORIES_DATA"] == str(tmp_path / "firmware-toolchain/arduino-data")
    assert env["ARDUINO_DIRECTORIES_USER"] == str(tmp_path / "firmware-toolchain/arduino-libraries")
    assert env["ARDUINO_CONFIG_FILE"] == str(tmp_path / "firmware-toolchain/arduino-cli.yaml")
    assert all("upload" not in command and "update-index" not in command for command, _ in calls)


def test_firmware_preflight_rejects_noncanonical_bossa_path(tmp_path: Path, monkeypatch) -> None:
    _preflight_toolchain(tmp_path, path=f"toolchain/arduino-data/packages/arduino/tools/bossac/{firmware.CANONICAL_BOSSA_VERSION}/bin/bossac")
    monkeypatch.setenv("EVOLVER_FIRMWARE_TOOLCHAIN_ROOT", str(tmp_path / "firmware-toolchain"))
    monkeypatch.setattr(
        firmware,
        "_run_read_only",
        lambda command, *, env=None: subprocess.CompletedProcess(
            command,
            0,
            stdout=(str(tmp_path / "nix-store-bossac") + "\n")
            if command[:3] == ["nix-store", "--query", "--requisites"]
            else "tools.bossac.path=unused\ntools.bossac.cmd=bossac\n",
            stderr="",
        ),
    )
    with pytest.raises(SystemExit, match="canonical Arduino root path"):
        main(["preflight"])


def test_release_self_contained_checker_requires_firmware(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    installer = tmp_path / "installer.sh"
    installer.write_text("server-hosted installer", encoding="utf-8")
    manifest.write_text(json.dumps({"artifacts": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, "tools/check_evolver_release_self_contained.py",
                             "--installer", str(installer), "--manifest", str(manifest)],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "firmware artifact" in result.stderr


def test_release_builder_records_firmware_size_and_manifest_timestamp(tmp_path: Path) -> None:
    x86, arm, firmware = (tmp_path / name for name in ("x86.tar.gz", "arm.tar.gz", "firmware.bin"))
    x86.write_bytes(b"x86")
    arm.write_bytes(b"arm")
    firmware.write_bytes(b"firmware")
    subprocess.run([sys.executable, "tools/build_evolver_release.py", "--output", str(tmp_path / "published"),
                    "--version", "acceptance", "--git-revision", "d" * 40, "--x86_64", str(x86),
                    "--aarch64", str(arm), "--firmware", str(firmware), "--build-timestamp",
                    "1970-01-01T00:00:00Z"], check=True)
    release = tmp_path / "published" / "acceptance"
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["build_timestamp"] == "1970-01-01T00:00:00Z"
    assert manifest["firmware"]["size"] == firmware.stat().st_size
    assert "lifecycle-plan --current-state" in manifest["required_cli_capabilities"]
    subprocess.run([sys.executable, "tools/validate_evolver_release.py", str(release)], check=True)


def test_release_builder_can_truthfully_publish_x86_64_only(tmp_path: Path) -> None:
    artifact = tmp_path / "x86.tar.gz"
    firmware = tmp_path / "firmware.bin"
    artifact.write_bytes(b"x86 native wheelhouse")
    firmware.write_bytes(b"SAMD21 image")
    subprocess.run([sys.executable, "tools/build_evolver_release.py", "--output", str(tmp_path / "published"),
                    "--version", "x86-only", "--git-revision", "e" * 40,
                    "--artifact", f"linux-x86_64={artifact}", "--firmware", str(firmware)], check=True)
    manifest = json.loads((tmp_path / "published/x86-only/manifest.json").read_text())
    assert manifest["architectures"] == ["linux-x86_64"]
    assert "linux-aarch64" not in manifest["artifacts"]


def test_self_contained_checker_rejects_public_source_in_production_payload(tmp_path: Path) -> None:
    installer = tmp_path / "installer.sh"
    installer.write_text("curl https://github.com/example/repo", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"firmware": {"variant": "samd21-minievolver", "size": 1}}), encoding="utf-8")
    result = subprocess.run([sys.executable, "tools/check_evolver_release_self_contained.py", "--installer", str(installer),
                             "--manifest", str(manifest)], text=True, capture_output=True)
    assert result.returncode != 0
    assert "github.com" in result.stderr


def test_native_release_defaults_to_edge_python_version() -> None:
    source = Path("tools/build_evolver_native_artifact.py").read_text(encoding="utf-8")
    assert '"--python-version", default="3.12"' in source
    assert '"--abi", f"cp{python_digits}"' in source


def test_nixos_bossac_provenance_separates_root_wrapper_from_store_executable() -> None:
    source = Path("tools/build_evolver_native_artifact.py").read_text(encoding="utf-8")
    assert 'canonical_bossac = toolchain / "arduino-data/packages/arduino/tools/bossac"' in source
    assert '"path": f"toolchain/arduino-data/packages/arduino/tools/bossac/{TOOLCHAIN_BOSSAC_VERSION}/bossac"' in source
    assert '"store_executable": str(supplied_bossac)' in source
    assert '"closure_artifact": "toolchain/nix-closures/bossac.closure"' in source
    assert '"#!/bin/sh\\nexec " + str(supplied_bossac)' in source


def test_nixos_validation_requires_sparkfun_root_wrapper_and_separate_store_path() -> None:
    source = Path("tools/validate_evolver_release.py").read_text(encoding="utf-8")
    assert "must name the Arduino root wrapper" in source
    assert "store_executable_sha256" in source
    assert "wrapper digest does not match its declared path" in source
    assert 'exec {store_executable} \\"$@\\"' in source
    assert "masks executable failures" in source


def test_nixos_builder_hashes_the_declared_root_wrapper_path() -> None:
    source = Path("tools/build_evolver_native_artifact.py").read_text(encoding="utf-8")
    assert 'bossac_digest = _sha256(canonical_bossac if toolchain_variant == "linux-x86_64-nixos" else bossac)' in source
    assert '"wrapper_sha256": _sha256(canonical_bossac)' in source


def test_cli_exposes_read_only_firmware_preflight() -> None:
    source = Path("applications/evolver/backend/src/meta_webui_application_backend/evolver_edge/cli.py").read_text(encoding="utf-8")
    assert 'choices=("build", "upload", "verify", "preflight")' in source


def test_production_builder_gates_publication_on_packaged_lifecycle_contract() -> None:
    source = Path("tools/build_evolver_production_release.py").read_text(encoding="utf-8")
    assert "check_evolver_native_artifact.py" in source


def test_nixos_production_builder_requires_explicit_offline_inputs() -> None:
    source = Path("tools/build_evolver_production_release.py").read_text(encoding="utf-8")
    assert "--nixos-arduino-cli" in source
    assert "--nixos-arduino-data" in source
    assert "refusing PATH/download fallback" in source
    assert "if not offline_nixos:" in source


def test_nixos_production_builder_requires_explicit_offline_toolchain() -> None:
    source = Path("tools/build_evolver_production_release.py").read_text(encoding="utf-8")
    assert "NixOS production builds require --nixos-arduino-cli, --nixos-arduino-data, and --nixos-arduino-libraries" in source
    assert "if not offline_nixos:" in source
    assert "shutil.which(\"arduino-cli\")" in source
    assert "refusing PATH/download fallback" in source


def test_nixos_service_exposes_release_firmware_contract() -> None:
    source = Path("nix/evolver-controller.nix").read_text(encoding="utf-8")
    assert "firmwareArtifact" in source
    assert "firmwareSha256File" in source
    assert "EVOLVER_FIRMWARE_ARTIFACT=${cfg.firmwareArtifact}" in source
    assert "EVOLVER_FIRMWARE_SHA256_FILE=${cfg.firmwareSha256File}" in source


def _toolchain_artifact(path: Path, *, bossac_version: str = "1.7.0-arduino3") -> None:
    provenance = {
        "format": 1, "offline": True, "variant": "linux-x86_64-glibc",
        "arduino_cli": {"version": "1.1.1", "path": "toolchain/arduino-cli", "sha256": hashlib.sha256(b"cli").hexdigest()},
        "arduino_data": {"path": "toolchain/arduino-data"},
        "arduino_libraries": {"path": "toolchain/arduino-libraries"}, "board_core": "SparkFun:samd@1.8.13",
        "bossac": {"version": bossac_version, "path": f"toolchain/arduino-data/packages/arduino/tools/bossac/{bossac_version}/bin/bossac",
                   "sha256": hashlib.sha256(b"bossac").hexdigest(),
                   "source_archive_sha256": "1ae54999c1f97234c5a603eb99ad39313b11746a4ca517269a9285afa05f9100"},
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (("toolchain/PROVENANCE.json", json.dumps(provenance).encode()),
                           ("toolchain/arduino-cli", b"cli"),
                           ("toolchain/arduino-libraries/FlashStorage_SAMD/library.properties", b"name=FlashStorage_SAMD\n"),
                           ("toolchain/arduino-data/packages/arduino/tools/bossac/1.7.0-arduino3/bin/bossac", b"bossac")):
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o755
            archive.addfile(info, __import__("io").BytesIO(data))


def test_release_manifest_embeds_offline_toolchain_contract(tmp_path: Path) -> None:
    artifact, firmware = tmp_path / "x86.tar.gz", tmp_path / "firmware.bin"
    _toolchain_artifact(artifact); firmware.write_bytes(b"firmware")
    subprocess.run([sys.executable, "tools/build_evolver_release.py", "--output", str(tmp_path / "published"),
                    "--version", "bundle", "--git-revision", "a" * 40, "--artifact", f"linux-x86_64={artifact}",
                    "--firmware", str(firmware)], check=True)
    manifest = json.loads((tmp_path / "published/bundle/manifest.json").read_text())
    assert manifest["artifacts"]["linux-x86_64"]["firmware_toolchain"]["offline"] is True
    subprocess.run([sys.executable, "tools/validate_evolver_release.py", str(tmp_path / "published/bundle")], check=True)


def test_release_manifest_rejects_bossa_mismatch(tmp_path: Path) -> None:
    artifact, firmware = tmp_path / "x86.tar.gz", tmp_path / "firmware.bin"
    _toolchain_artifact(artifact, bossac_version="1.9.1"); firmware.write_bytes(b"firmware")
    result = subprocess.run([sys.executable, "tools/build_evolver_release.py", "--output", str(tmp_path / "published"),
                             "--version", "bad-bossa", "--git-revision", "a" * 40, "--artifact", f"linux-x86_64={artifact}",
                             "--firmware", str(firmware)], text=True, capture_output=True)
    assert result.returncode != 0
    assert "BOSSA" in result.stderr
