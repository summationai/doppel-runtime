# The PR sandbox image (spec section 6). One generic image per engine release: nothing about a PR,
# a repo, a user or a rule is baked in. Build from the repo root:
#   docker build -f src/sandbox-runtime/Dockerfile -t doppel-sandbox:dev .
FROM python:3.12-slim-bookworm

# Tools a reviewer reads code with. No compilers, package managers for the PR's language, gh, or
# credentials: nothing from the PR can be built or run, and nothing here talks to GitHub.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep jq ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

# The pinned Codex SDK and its codex binary, and the two packages the harness needs.
RUN pip install --no-cache-dir "openai-codex==0.167.2" "jsonschema>=4.20"
COPY src/shared/doppel_shared /opt/doppel/doppel_shared
COPY src/sandbox-runtime/doppel_sandbox /opt/doppel/doppel_sandbox
ENV PYTHONPATH=/opt/doppel PYTHONDONTWRITEBYTECODE=1

# A pool of reviewer users: each reviewer run in a pod gets its own, so one run can't read
# another's capability (review #5). The pod itself idles as `pod`, which holds nothing.
RUN useradd --system --uid 10000 --home-dir /nonexistent --shell /usr/sbin/nologin pod \
 && for i in $(seq 1 16); do \
      useradd --system --uid $((10000 + i)) --home-dir /nonexistent --shell /usr/sbin/nologin "reviewer$i"; \
    done

USER pod
WORKDIR /work
# Run with: --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges,
# /work and /job mounted read-only, on a network that reaches only the gateway.
CMD ["sleep", "infinity"]
