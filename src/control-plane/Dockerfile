# The runner image (docs/specs/packaging.md): `doppel-runtime serve`, with Docker's CLI to start the review pods through
# the host's Docker socket, and git for the repository mirrors. Build from the repo root:
#   docker build -f src/control-plane/Dockerfile -t doppel-runtime:dev .
# Run it as the host command does (step 3): the data volume, the socket, and the runner's own network.
#   docker network create doppel-runtime && docker volume create doppel-runtime-data
#   docker run -d --name doppel-runtime --network doppel-runtime --restart unless-stopped \
#     -v doppel-runtime-data:/var/lib/doppel-runtime -v /var/run/docker.sock:/var/run/docker.sock doppel-runtime:dev
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Docker's CLI only (the daemon is the host's), pinned, for either architecture.
ARG DOCKER_CLI_VERSION=28.3.3
ARG TARGETARCH
RUN arch="$([ "$TARGETARCH" = arm64 ] && echo aarch64 || echo x86_64)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
  | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && docker --version

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
WORKDIR /opt/doppel-runtime
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --python /usr/local/bin/python3 \
 && rm -rf /root/.cache

# The review pod image this runner starts: a release builds the sandbox first and passes its digest here, so a runner
# only ever starts the pod image of its own release.
ARG SANDBOX_IMAGE=doppel-sandbox:dev
# The packaging defaults; the host command sets nothing else. The model listener listens on the runner's network, where
# only the pods' egress forwarder reaches it; the internal and health listeners stay on the container's loopback.
ENV PATH=/opt/doppel-runtime/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    DOPPEL_DATA_DIR=/var/lib/doppel-runtime \
    DOPPEL_MODEL_HOST=0.0.0.0 \
    DOPPEL_SANDBOX_RUNTIME=auto \
    DOPPEL_SANDBOX_DATA_VOLUME=doppel-runtime-data \
    DOPPEL_SANDBOX_NETWORK=doppel-runtime \
    DOPPEL_SANDBOX_GATEWAY_TARGET=doppel-runtime:8700 \
    DOPPEL_SANDBOX_IMAGE=${SANDBOX_IMAGE}
VOLUME /var/lib/doppel-runtime
LABEL dev.doppelwerk.runtime="1"
CMD ["doppel-runtime", "serve"]
