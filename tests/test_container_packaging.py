from pathlib import Path


PACKAGE_ROOT = Path(__file__).parents[1]


def test_dockerfile_installs_dependencies_before_copying_application_source() -> None:
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")

    dependency_install = dockerfile.index("RUN python -m pip install --no-cache-dir")
    source_copy = dockerfile.index("COPY src ./src")
    package_install = dockerfile.index("RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps .")

    assert dependency_install < source_copy < package_install
    assert '"pyserial==3.5"' in dockerfile
    assert "--no-deps" in dockerfile
    assert "COPY ." not in dockerfile


def test_dockerignore_keeps_only_runtime_package_inputs_in_the_context() -> None:
    dockerignore = (PACKAGE_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert {"tests", ".pytest_cache", "**/__pycache__", ".git"} <= set(dockerignore)
    assert "src" not in dockerignore
    assert "pyproject.toml" not in dockerignore
    assert "README.md" not in dockerignore
