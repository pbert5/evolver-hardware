FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Keep dependency installation independent of application source changes.
# The runtime dependency is intentionally pinned to the package metadata.
RUN python -m pip install --no-cache-dir "setuptools>=68" "pyserial==3.5"

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps .

RUN mkdir -p /var/lib/evolver-hardware /run/evolver-controller
ENTRYPOINT ["evolver-hardware"]
