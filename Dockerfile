# forge3d — container image
#
# The procedural backend needs no external binary, so this image is fully
# functional for agent-driven modelling out of the box. Blender and OpenSCAD are
# deliberately NOT installed: Blender alone is ~1 GB, and most deployments that
# need it want a specific version. Extend the image if you need them:
#
#   FROM forge3d:latest
#   USER root
#   RUN apt-get update && apt-get install -y openscad && rm -rf /var/lib/apt/lists/*
#
# Build:  docker build -t forge3d .
# Stdio:  docker run -i --rm -v "$PWD/work:/work" forge3d
# HTTP:   docker run --rm -p 8000:8000 -e FORGE3D_HTTP_TOKEN=... \
#             -v "$PWD/work:/work" forge3d --http 8000 --host 0.0.0.0
#
# A non-loopback bind refuses to start without FORGE3D_HTTP_TOKEN. Running as
# non-root is deliberate: model code executes in this container. See
# docs/security.md before exposing the port.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FORGE3D_WORKDIR=/work

# matplotlib needs no system libs in Agg mode, but trimesh's preview path wants
# these for fonts; keeping the image lean and previews functional.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[render,http]"

# Run as a non-root user: generated models are user data, not image content.
RUN useradd --create-home --uid 10001 forge3d \
    && mkdir -p /work \
    && chown -R forge3d:forge3d /work /app
USER forge3d

VOLUME ["/work"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD forge3d doctor --quiet > /dev/null || exit 1

ENTRYPOINT ["forge3d-mcp"]
CMD []
