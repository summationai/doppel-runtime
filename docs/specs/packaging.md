# Packaging: installing, pairing and updating a runner

Status: decided 2026-11-24. Steps 1 and 2 are built (doppel-runtime #26, Doppelwerk v79); step 3, the host command, is
built (#28); step 4's workflow is built and waits for the first tag; step 5 is not. Until the first release a runner is installed by hand: the mini's VM
follows the Linux runner guide from #19 step by step, and an update means copying a commit in from a laptop.
This spec replaces that with releases and one command, on a Mac or Linux, with Docker as the only thing to install first.

## Decided

| Decision | Choice |
|---|---|
| Prerequisite | **Docker, version 26 or later.** On a Mac: Docker Desktop, OrbStack or Colima. On Linux: Docker Engine. Nothing else: no Python, no Git, no GitHub access. |
| What a release is | A tag `vX.Y.Z` in this repo. Per tag: two images, the host command for four platforms, `install.sh`, and the Homebrew formula. |
| Images | **Public**, on GitHub's registry: `ghcr.io/summationai/doppel-runtime:X.Y.Z` (the runner) and `ghcr.io/summationai/doppel-sandbox:X.Y.Z` (the review pod). Both for `linux/amd64` and `linux/arm64`. No pull credential. |
| The host command | `doppel-runtime`, a single Go binary per platform (`darwin`/`linux` × `amd64`/`arm64`). It runs the `docker` command, so it follows whichever Docker the person uses (`docker context`). It holds no review logic: it installs, pairs, updates and removes the runner container, and passes the rest through. |
| Install | Mac: `brew install summationai/tap/doppel-runtime`. Linux (or a Mac without Homebrew): `curl -fsSL https://github.com/summationai/doppel-runtime/releases/latest/download/install.sh \| sh`. |
| gVisor | **A preference, on Linux.** Where Docker has `runsc` registered, reviews run under gVisor; otherwise under Docker's default runtime, with the hardened flags they have today. A Mac's Docker has no gVisor: standard isolation. Doppelwerk shows each runner's isolation; no workspace can require gVisor (not yet). |
| Updates | **Manual, one command:** `doppel-runtime update` does everything an update needs (below). No automatic updates. Doppelwerk says when one is available or required (the handshake, [bff-split.md §2b](bff-split.md)) and shows the command. |
| Not doing | npm, Windows, automatic updates, a GUI, running the runner outside Docker. |

## What a person does

1. Install Docker (26 or later), and start it.
2. In Doppelwerk, **Runners → Add a runner**, name it, and copy the line for their system. It installs the host
   command if it isn't there, then pairs:

   ```sh
   # Mac
   brew install summationai/tap/doppel-runtime && doppel-runtime pair dwp_… --bff https://doppelwerk.ac0.app
   # Linux
   curl -fsSL https://github.com/summationai/doppel-runtime/releases/latest/download/install.sh | sh \
     && doppel-runtime pair dwp_… --bff https://doppelwerk.ac0.app
   ```

   Within a check-in (30 seconds) the runner shows **Online**, with its version and isolation.
3. Each member who pays: **Add my seat**, and run the line it shows, `doppel-runtime seat add dws_…`. It prints a link
   and a code; they approve the Codex sign-in in their own browser.
4. When Runners shows **Update available** or **Update required**: `doppel-runtime update`.

## The host command

Everything it creates on the Docker host carries the label `dev.doppelwerk.runtime=1`, and is named:

| Thing | Name | Holds |
|---|---|---|
| Container | `doppel-runtime` | The runner (`doppel-runtime serve`), `--restart unless-stopped`: Docker brings it back after a reboot. |
| Volume | `doppel-runtime-data` | Everything that must survive an update: the pairing, each seat's Codex sign-in, the ledger, mirrors, checkouts. Owner-only inside, as today. |
| Network | `doppel-runtime` | The runner and the pods' egress forwarder: the forwarder reaches the runner's model listener as `doppel-runtime:8700`. Pods stay on their own `--internal` networks, as today. |

The version and image digests the runner was started from are labels on the container; the host command keeps no files
of its own.

| Command | Does |
|---|---|
| `doppel-runtime doctor` | Checks, and says in plain words what to fix: Docker reachable and 26+; the machine's architecture; free disk (at least 20 GB) and memory; whether `runsc` is registered (Linux: recommends it if not, with the command); outbound HTTPS to Doppelwerk, GitHub, `ghcr.io` and OpenAI. Exit status 0 only when a runner can work. |
| `doppel-runtime pair CODE --bff URL` | Runs `doctor`; pulls this release's two images (by digest); creates the volume and network; starts the container; pairs it (the code goes to the container on stdin, never in its arguments or environment); waits for its first check-in; prints "Paired: '<name>' in '<workspace>'. Online." Pairing a machine that already has a runner asks for `--replace`. |
| `doppel-runtime seat add CODE` | Passes through to the runner (`docker exec`); prints the sign-in link and code, and waits for the approval. |
| `doppel-runtime status` | The host command's version, the runner's version, protocol, isolation and image; then the runner's own status (pairing, last check-in, update available or required, seats). |
| `doppel-runtime logs [-f]` | The runner's log. |
| `doppel-runtime update [--version X.Y.Z]` | Below. |
| `doppel-runtime uninstall` | Signs every seat out and forgets the pairing (`doppel-runtime forget` in the runner), removes the container, the network and (after a confirmation, or `--yes`) the volume, and says to remove the runner in Doppelwerk's Runners: Doppelwerk has no route for a runner to remove itself (not yet). Removing the host command itself is `brew uninstall` or deleting the file. |

## `doppel-runtime update`

One command, safe to run any time, that leaves the runner on the target version or on the one it had:

1. **Pick the target.** The latest release, or `--version X.Y.Z`. Refuse a target whose protocol the runner's
   Doppelwerk no longer accepts (its `minimum`, from the last check-in), and say why.
2. **Update the host command first** if the target needs a newer one: through `brew upgrade` when Homebrew installed
   it, otherwise by downloading the release's binary, checking its SHA-256 against the release's `SHA256SUMS`, and
   replacing itself. Then run the new one with the same arguments.
3. **Run `doctor`.** A requirement the new version adds (a newer Docker, say) stops the update here, with the fix.
4. **Pull both images** by digest. Nothing has changed yet, so a failed pull leaves the runner as it was.
5. **Drain.** Tell the runner to take no new work and wait for the reviews in hand to finish, up to 10 minutes
   (`--now` skips the wait; Doppelwerk retries an interrupted review on the next runner check-in).
6. **Swap.** Stop the old container and keep it, renamed `doppel-runtime-previous`; start the new one on the same
   volume and network. The new runner upgrades what's in the volume if its layout changed (each layout carries a
   version; an upgrade is forward-only and the old layout is kept until the new runner has checked in once).
7. **Prove it.** Wait for a successful check-in (up to 2 minutes). Then remove the old container and the old images
   (only digest-pinned ones, which a release installed: never a tag someone keeps), and print the new version and
   "Online". If the new runner doesn't check in, put the old container back, say what the new
   one logged, and exit non-zero.

No re-pairing and no signing in again: the pairing and the sign-ins are in the volume.

## The runner image

- `doppel-runtime serve` with its config in the image; only `DOPPEL_*` overrides are set from outside.
- Mounts: `doppel-runtime-data` at `/var/lib/doppel-runtime`, and the Docker socket, which it needs to start pods.
  **The socket is root on the Docker host**: the docs say to give a runner a dedicated machine or VM.
- Pods' mounts: today a pod's checkout and job folder are bind mounts from the runner's data folder. From a container
  those paths would be resolved on the Docker host. Instead they become **volume mounts of a subpath of
  `doppel-runtime-data`** (`--mount type=volume,src=doppel-runtime-data,volume-subpath=pods/<pod>/…,dst=/work,readonly`),
  which is why Docker 26 is the minimum.
- Listeners: the model listener on the `doppel-runtime` network; the internal and health listeners on the container's
  loopback only (the host command reaches them with `docker exec`).
- gVisor: at start, if Docker reports `runsc`, pods use it; otherwise the default runtime. The choice is logged and
  reported (below).
- The sandbox image's digest is built into the runner, so a runner only ever starts the pod image of its own release.

## What Doppelwerk adds (protocol 2)

Per [§2b](bff-split.md), additive, BFF first:

- **Check-in:** the runtime also sends `isolation` (`"gvisor"` or `"standard"`). Runners shows it beside the version.
- **Answers** (pair and check-in) also carry `release: {latest: "X.Y.Z", notes: <url>}`, which the BFF reads from this
  repo's latest GitHub release (cached for an hour). Runners shows "Update to X.Y.Z: run `doppel-runtime update`".
- **Add a runner** shows the Mac and Linux lines above, with the code filled in. **Add my seat** shows the seat line.

## Releasing

Pushing a tag `vX.Y.Z` runs one workflow:

1. The tests, lint and the Docker integration tests.
2. Build and push both images for both architectures; record their digests.
3. Build the host command for the four platforms with the runner and sandbox digests and the version built in;
   `SHA256SUMS` over the binaries and `install.sh`.
4. Create the GitHub release with the binaries (`doppel-runtime_<os>_<arch>`), `SHA256SUMS`, `install.sh`, the notes,
   and `release.json`, which `update` reads to pick its target:

   ```json
   {"version": "0.2.0", "protocol": 2,
    "runner_image": "ghcr.io/summationai/doppel-runtime@sha256:…",
    "sandbox_image": "ghcr.io/summationai/doppel-sandbox@sha256:…"}
   ```
5. Update the formula in `summationai/homebrew-tap` to the new binaries and their sums.

The runtime's version (`__version__`, shown on Runners) is the tag's: the workflow refuses a tag that doesn't match it.
A release that raises `PROTOCOL` says so in its notes (they name the protocol).

**To release** (`.github/workflows/release.yml`, `scripts/release/`):

1. In a pull request, set `__version__` in `src/control-plane/doppel_control_plane/__init__.py` to the new X.Y.Z; merge.
2. Tag that commit and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The workflow runs everything CI runs, then publishes. The first time only: make both packages public on ghcr.io
   (GitHub makes new packages private), and create `summationai/homebrew-tap` with a `HOMEBREW_TAP_TOKEN` secret here
   that can push to it; without the secret the release still works through `install.sh` and only the formula waits.

## Moving the mini over

The mini's VM is a Linux host with Docker, so after the first release it becomes an ordinary install, keeping its
pairing and sign-ins:

1. In the VM, install the host command (`install.sh`), and `runsc` stays registered.
2. `doppel-runtime import /var/lib/doppel-runner`: stops `doppel-runner.service`, copies its data folder into the new
   volume, starts the container, waits for a check-in, then disables the service. The old folder is left in place until
   someone deletes it.

## Order of work

1. **Runner in a container.** Pods' mounts as volume subpaths; listeners as above; `isolation` detection; the runner
   and sandbox Dockerfiles; the integration tests run against the containerized runner.
2. **Protocol 2 in the BFF**, then in the runtime: `isolation` in, `release` out; Runners and the two dialogs.
3. **The host command** in Go (`cmd/doppel-runtime-host/`, built as `doppel-runtime`): `doctor`, `pair`, `seat add`,
   `status`, `logs`, `uninstall`, then `update` with its rollback; tests against a real Docker in CI. **Built:** Go
   standard library only, built and tested in Go's own image (`golang:1.24.6`), so nothing is installed to work on it;
   `tests/integration/test_host_command.py` pairs, gets a review done, updates, rolls a broken update back and
   uninstalls against real Docker. The runner gained what it needs: codes on stdin (`pair -`, `seat add -`),
   `status --json`, `drain [--off]` (check-ins offer no room) and `forget`. `import` (step 5) is not built.
4. **The release workflow**, `install.sh` and the Homebrew tap; the first release, `v0.2.0`. **Built:**
   `.github/workflows/release.yml`; `scripts/release/install.sh` (tested against a release on disk, and by shellcheck);
   `scripts/release/release.py` (the version check, release.json, the notes, the formula; `tests/test_release.py`).
   The runner image takes its sandbox image as a build argument (`SANDBOX_IMAGE`), so a release's runner names its
   sandbox by digest. Not yet: the tap repository, its token, and the first tag.
5. **The mini moved over** with `import`; this spec's steps run by someone other than its author on a clean Mac and a
   clean Linux VM.

## Overrides for tests

None of these is for a real install; each lets a test run beside one without touching it.

| Where | Variable | Does |
|---|---|---|
| Host command | `DOPPEL_HOST_PREFIX` | Names the container, volume and network `<prefix>`, `<prefix>-data`, `<prefix>-previous` |
| Host command | `DOPPEL_RUNTIME_IMAGE`, `DOPPEL_SANDBOX_IMAGE` | Local images instead of this build's release; `update --runner-image/--sandbox-image` likewise |
| Host command | `DOPPEL_HOST_EXTRA_RUN_ARGS` | A JSON array of extra `docker run` arguments for the runner (fixture repos, a fake model) |
| Host command | `DOPPEL_HOST_RELEASES` | Where releases are read from, instead of GitHub |
| Host command | `DOPPEL_HOST_PROVE_WITHIN` | How long an updated runner has to check in (default 2m) |
| Runner | `DOPPEL_CHATGPT_BASE`, `DOPPEL_REPO_URL` | A fake model endpoint; where mirrors fetch from |

## Open

- **macOS Gatekeeper.** A binary installed by Homebrew or `curl` isn't quarantined, so it runs; one downloaded by a
  browser would be. If people download it by hand, the binary needs signing and notarizing.
- **Rate limits.** Anonymous pulls from `ghcr.io` are rate limited; fine for a few runners, to revisit if many update at
  once.
- **The mini's other Docker.** If `rc`'s side of the mini ever runs Docker, the VM keeps the two apart; nothing here
  assumes one Docker per machine, since everything is named and labelled.
