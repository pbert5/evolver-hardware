FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir .
RUN mkdir -p /var/lib/evolver-hardware /run/evolver-controller
ENTRYPOINT ["evolver-hardware"]
