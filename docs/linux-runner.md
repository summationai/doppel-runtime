# Set up a Doppelwerk runner on Linux

One Ubuntu host or VM can run reviews for a workspace. The host keeps each member's Codex sign-in on its own disk. Members approve their own device sign-in from a browser. Give seats only to members who trust the host administrator.

## Install the runner

Use Ubuntu 24.04 on amd64 or arm64 with Python 3.12, Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and local [Docker Engine](https://docs.docker.com/engine/install/ubuntu/). For gVisor isolation, [install and register `runsc`](https://gvisor.dev/docs/user_guide/quick_start/docker/) with Docker. Run these checks from the Linux host:

```sh
docker info
docker run --rm --runtime=runsc hello-world
```

Use a dedicated runner host. The service account needs access to the Docker socket, which gives it administrative access to the host. The host needs outbound HTTPS access to Doppelwerk, GitHub, and the Codex services. Bootstrap also needs access to the source and dependency registries.

With access to the runtime repository, check out the revision approved for your workspace. These commands install it at the path used by the service below.

```sh
sudo install -d -m 0755 -o "$(id -un)" /opt/doppel-runtime
git clone https://github.com/summationai/doppel-runtime.git /opt/doppel-runtime
cd /opt/doppel-runtime
# Check out your approved release or commit before installing.
uv sync --locked --no-dev --python /usr/bin/python3
docker build -f src/sandbox-runtime/Dockerfile -t doppel-sandbox:local .
```

Create an account and a state directory for the runner. Run the daemon and its pairing and seat commands as this same account.

```sh
sudo useradd --system --user-group --create-home --home-dir /var/lib/doppel-runner --shell /usr/sbin/nologin doppel-runner
sudo chmod 0700 /var/lib/doppel-runner
sudo install -d -m 0755 /etc/doppel-runner
```

Save this configuration as `/etc/doppel-runner/runner.toml`.

```toml
data_dir = "/var/lib/doppel-runner"

[sandbox]
image = "doppel-sandbox:local"
runtime = "runsc"
```

Save this service as `/etc/systemd/system/doppel-runner.service`.

```ini
[Unit]
Description=Doppelwerk review runner
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
User=doppel-runner
Group=doppel-runner
SupplementaryGroups=docker
WorkingDirectory=/opt/doppel-runtime
ExecStart=/opt/doppel-runtime/.venv/bin/doppel-runtime serve --config /etc/doppel-runner/runner.toml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Pair the host and add seats

1. In Doppelwerk, open **Settings → Runners → Add a runner**. Name the host and get the one-time pair command. On the Linux host, use the executable and config from the service:

   ```sh
   sudo -u doppel-runner /opt/doppel-runtime/.venv/bin/doppel-runtime pair '<pair code>' --bff 'https://doppelwerk.ac0.app' --config /etc/doppel-runner/runner.toml
   sudo systemctl daemon-reload
   sudo systemctl enable --now doppel-runner
   ```

   Use the Doppelwerk address shown in the pair command. Pair codes expire after 30 minutes.

2. When the runner is **Online**, each member who pays for reviews chooses **Add my seat**. Run their one-time seat command on the host:

   ```sh
   sudo -u doppel-runner /opt/doppel-runtime/.venv/bin/doppel-runtime seat add '<seat code>' --config /etc/doppel-runner/runner.toml
   ```

   The member follows the displayed link and enters the device code in their own browser. The seat becomes **Ready** after the runner checks in.

3. Set a payer for any app doppel you want the runner to serve. Open a test PR in an enrolled repository. Its **Reviews** card shows the runner and the verdict.

## Check the connection

```sh
sudo -u doppel-runner /opt/doppel-runtime/.venv/bin/doppel-runtime status --config /etc/doppel-runner/runner.toml
sudo journalctl -u doppel-runner -n 50
```

On Linux the default model listener binds to loopback and the local Docker bridge. The broker and health listeners bind to loopback. The runner starts the review pod on an isolated network and maps its `gateway` hostname to the forwarder on that network. Use the standard local Docker IPv4 bridge for this setup. For a custom Docker network, set `[listeners.model].host` to a host address reachable from the forwarder and set `[sandbox].gateway_target` to that address and the model port. Keep the internal and health listeners on loopback.

Keep `/var/lib/doppel-runner` across upgrades. To upgrade, check out the new runtime revision, rerun `uv sync` and the image build, then restart the service.
