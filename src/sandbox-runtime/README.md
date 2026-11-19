# sandbox-runtime

What runs inside a PR's pod: the image (Debian slim, non-root, read-only root, a pinned Codex SDK) and the harness that reads `/job`, runs one Codex process per reviewer run, validates each verdict and writes `/out/<reviewer>.json`. Built in checkpoint 5; see [docs/specs/doppel-service.md](../../docs/specs/doppel-service.md) section 6.
