package main

// Everything this command does to the machine, it does by running `docker`, so it follows the person's own Docker
// (Docker Desktop, OrbStack, Colima, Docker Engine) and whichever `docker context` they use.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

// Docker runs the docker CLI. Tests swap it for a fake.
type Docker interface {
	// Out runs docker and returns its trimmed stdout; a non-zero exit is an error carrying stderr.
	Out(args ...string) (string, error)
	// Stream runs docker with this process's terminal (stdin from `in` when given), for commands a person watches.
	Stream(in io.Reader, args ...string) error
}

type cli struct{}

func (cli) Out(args ...string) (string, error) {
	var out, errb bytes.Buffer
	cmd := exec.Command("docker", args...)
	cmd.Stdout, cmd.Stderr = &out, &errb
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(errb.String())
		if msg == "" {
			msg = err.Error()
		}
		return "", fmt.Errorf("docker %s: %s", args[0], msg)
	}
	return strings.TrimSpace(out.String()), nil
}

func (cli) Stream(in io.Reader, args ...string) error {
	cmd := exec.Command("docker", args...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = in, os.Stdout, os.Stderr
	if in == nil {
		cmd.Stdin = os.Stdin
	}
	return cmd.Run()
}

// Names of what this command creates, all labelled dev.doppelwerk.runtime=1. DOPPEL_HOST_PREFIX changes them (tests
// run beside a real install without touching it).
type Names struct {
	Container, Previous, Volume, Network string
}

func names() Names {
	p := os.Getenv("DOPPEL_HOST_PREFIX")
	if p == "" {
		p = "doppel-runtime"
	}
	return Names{Container: p, Previous: p + "-previous", Volume: p + "-data", Network: p}
}

const (
	label   = "dev.doppelwerk.runtime"
	dataDir = "/var/lib/doppel-runtime"
)

// Images to run: this build's release (built in), or DOPPEL_RUNTIME_IMAGE / DOPPEL_SANDBOX_IMAGE for local ones.
type Images struct{ Runner, Sandbox, Version string }

func builtInImages() Images {
	im := Images{Runner: runnerImage, Sandbox: sandboxImage, Version: version}
	if v := os.Getenv("DOPPEL_RUNTIME_IMAGE"); v != "" {
		im.Runner = v
	}
	if v := os.Getenv("DOPPEL_SANDBOX_IMAGE"); v != "" {
		im.Sandbox = v
	}
	return im
}

// runArgs: the runner container as the spec draws it (docs/specs/packaging.md "The runner image").
func runArgs(n Names, im Images) []string {
	args := []string{
		"run", "-d", "--name", n.Container, "--network", n.Network, "--restart", "unless-stopped",
		"-v", n.Volume + ":" + dataDir, "-v", "/var/run/docker.sock:/var/run/docker.sock",
		"-e", "DOPPEL_SANDBOX_DATA_VOLUME=" + n.Volume, "-e", "DOPPEL_SANDBOX_NETWORK=" + n.Network,
		"-e", "DOPPEL_SANDBOX_GATEWAY_TARGET=" + n.Container + ":8700", "-e", "DOPPEL_SANDBOX_IMAGE=" + im.Sandbox,
		"--label", label + "=1", "--label", label + ".version=" + im.Version,
		"--label", label + ".runner-image=" + im.Runner, "--label", label + ".sandbox-image=" + im.Sandbox,
	}
	// For tests only: what a fixture needs (a fake model's address, fixture repositories). A JSON array of arguments.
	if extra := os.Getenv("DOPPEL_HOST_EXTRA_RUN_ARGS"); extra != "" {
		var more []string
		if json.Unmarshal([]byte(extra), &more) == nil {
			args = append(args, more...)
		}
	}
	return append(args, im.Runner)
}

// ensureImage pulls an image unless it's here already (a release names images by digest, so here means exact).
func ensureImage(d Docker, ref string) error {
	if _, err := d.Out("image", "inspect", "--format", "{{.Id}}", ref); err == nil {
		return nil
	}
	fmt.Printf("Pulling %s…\n", ref)
	if _, err := d.Out("pull", ref); err != nil {
		return fmt.Errorf("couldn't pull %s: %w", ref, err)
	}
	return nil
}

// exists says whether a container, volume or network of this name exists.
func exists(d Docker, kind, name string) bool {
	_, err := d.Out(kind, "inspect", name)
	return err == nil
}

// RunnerView is the runner's own status (`doppel-runtime status --json` in its container).
type RunnerView struct {
	Daemon  bool `json:"daemon"` // the daemon answered
	Paired  bool `json:"paired"`
	Runtime *struct {
		Name         string `json:"name"`
		Organization string `json:"organization"`
		URL          string `json:"url"`
	} `json:"runtime"`
	LastCheckIn *string `json:"last_check_in"`
	LastError   *string `json:"last_error"`
	Update      string  `json:"update"`
	Isolation   *string `json:"isolation"`
	Version     string  `json:"version"`
	Draining    bool    `json:"draining"`
	Jobs        int     `json:"jobs"` // reviews in hand
	BffProtocol *struct {
		Current int `json:"current"`
		Minimum int `json:"minimum"`
	} `json:"bff_protocol"`
	Release *struct {
		Latest string `json:"latest"`
	} `json:"release"`
	Seats []json.RawMessage `json:"seats"`
}

// runnerView asks the runner in the container for its status. An error: the container or its daemon isn't up.
func runnerView(d Docker, container string) (RunnerView, error) {
	out, err := d.Out("exec", container, "doppel-runtime", "status", "--json")
	var v RunnerView
	if jerr := json.Unmarshal([]byte(out), &v); jerr != nil {
		if err != nil {
			return v, err
		}
		return v, fmt.Errorf("the runner's status isn't JSON: %w", jerr)
	}
	if !v.Daemon {
		return v, fmt.Errorf("the runner's daemon isn't answering")
	}
	return v, nil
}
