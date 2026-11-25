package main

// update (docs/specs/packaging.md "doppel-runtime update"): leave the runner on the target version or on the one it
// had. Pick the target; update this command first if the target is newer; doctor; pull both images (nothing has
// changed yet); drain; swap, keeping the old container; wait for a check-in; then clean up, or put the old one back.

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

// Release is a release's release.json, published beside its binaries.
type Release struct {
	Version      string `json:"version"`
	Protocol     int    `json:"protocol"`
	RunnerImage  string `json:"runner_image"`
	SandboxImage string `json:"sandbox_image"`
}

// Releases reads published releases; tests swap it.
type Releases interface {
	Get(version string) (Release, error)        // "" for the latest
	Asset(version, name string) ([]byte, error) // a file attached to that release
}

type githubReleases struct{ base string }

func newReleases() Releases {
	base := os.Getenv("DOPPEL_HOST_RELEASES") // tests and mirrors
	if base == "" {
		base = "https://github.com/summationai/doppel-runtime/releases"
	}
	return githubReleases{strings.TrimRight(base, "/")}
}

func (g githubReleases) url(version, name string) string {
	if version == "" {
		return g.base + "/latest/download/" + name
	}
	return g.base + "/download/v" + strings.TrimPrefix(version, "v") + "/" + name
}

func (g githubReleases) Asset(version, name string) ([]byte, error) {
	client := http.Client{Timeout: 2 * time.Minute}
	res, err := client.Get(g.url(version, name))
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: %s", g.url(version, name), res.Status)
	}
	return io.ReadAll(io.LimitReader(res.Body, 200<<20))
}

func (g githubReleases) Get(version string) (Release, error) {
	raw, err := g.Asset(version, "release.json")
	if err != nil {
		return Release{}, fmt.Errorf("couldn't read the release: %w", err)
	}
	var r Release
	if err := json.Unmarshal(raw, &r); err != nil || r.Version == "" || r.RunnerImage == "" || r.SandboxImage == "" {
		return Release{}, errors.New("the release's release.json isn't complete")
	}
	return r, nil
}

var (
	drainLimit  = 10 * time.Minute
	provenLimit = 2 * time.Minute
)

func init() {
	// For tests that watch a broken update roll back: how long a new runner has to check in.
	if v, err := time.ParseDuration(os.Getenv("DOPPEL_HOST_PROVE_WITHIN")); err == nil && v > 0 {
		provenLimit = v
	}
}

func updateCmd(d Docker, args []string, out io.Writer, releases Releases) error {
	fs := flag.NewFlagSet("update", flag.ContinueOnError)
	want := fs.String("version", "", "a release to update to, X.Y.Z (default: the latest)")
	now := fs.Bool("now", false, "don't wait for running reviews (Doppelwerk retries them)")
	runner := fs.String("runner-image", "", "run this runner image instead of a release's (testing)")
	sandbox := fs.String("sandbox-image", "", "with --runner-image: the sandbox image to go with it")
	if err := fs.Parse(args); err != nil {
		return usageError{err.Error()}
	}
	n := names()
	if !exists(d, "container", n.Container) {
		return errors.New("no runner here to update: pair one first (`doppel-runtime pair …`)")
	}

	// 1. The target.
	var target Images
	var protocol int
	if *runner != "" {
		if *sandbox == "" {
			return usageError{"--runner-image needs --sandbox-image"}
		}
		target = Images{Runner: *runner, Sandbox: *sandbox, Version: "dev"}
	} else {
		r, err := releases.Get(*want)
		if err != nil {
			return err
		}
		target, protocol = Images{Runner: r.RunnerImage, Sandbox: r.SandboxImage, Version: r.Version}, r.Protocol
		if view, err := runnerView(d, n.Container); err == nil && view.BffProtocol != nil && protocol < view.BffProtocol.Minimum {
			return fmt.Errorf("doppel-runtime %s speaks protocol %d, and this runner's Doppelwerk needs %d or later: choose a newer version",
				r.Version, protocol, view.BffProtocol.Minimum)
		}
		// 2. This command first, when the target is newer than it.
		if newer(r.Version, version) && os.Getenv("DOPPEL_HOST_UPDATED") == "" {
			return selfUpdate(r.Version, releases, out)
		}
	}
	current, _ := d.Out("inspect", "--format", `{{index .Config.Labels "`+label+`.runner-image"}} {{index .Config.Labels "`+label+`.sandbox-image"}}`, n.Container)
	if current == target.Runner+" "+target.Sandbox {
		fmt.Fprintf(out, "Already on %s.\n", target.Version)
		return nil
	}

	// 3. What the new version needs; 4. both images, before anything changes.
	if !doctor(d, out, "").ok() {
		return errors.New("fix what doctor found, then update again")
	}
	for _, ref := range []string{target.Runner, target.Sandbox} {
		if err := ensureImage(d, ref); err != nil {
			return err
		}
	}

	// 5. Drain: no new reviews; wait for the running ones.
	if _, err := d.Out("exec", n.Container, "doppel-runtime", "drain"); err == nil && !*now {
		deadline := time.Now().Add(drainLimit)
		for {
			view, err := runnerView(d, n.Container)
			if err != nil || view.Jobs == 0 {
				break
			}
			if time.Now().After(deadline) {
				fmt.Fprintf(out, "%d review(s) still running after %s: going ahead; Doppelwerk retries them.\n", view.Jobs, drainLimit)
				break
			}
			fmt.Fprintf(out, "Waiting for %d running review(s) to finish…\n", view.Jobs)
			time.Sleep(10 * pollEvery)
		}
	}

	// 6. Swap, keeping the old container until the new one has proven itself.
	_, _ = d.Out("rm", "-f", n.Previous)
	if _, err := d.Out("stop", "-t", "30", n.Container); err != nil {
		return undrain(d, n, err)
	}
	if _, err := d.Out("rename", n.Container, n.Previous); err != nil {
		_, _ = d.Out("start", n.Container)
		return undrain(d, n, err)
	}
	started := time.Now().UTC()
	if _, err := d.Out(runArgs(n, target)...); err != nil {
		return rollback(d, n, out, fmt.Errorf("the new runner didn't start: %w", err))
	}

	// 7. Proof: a check-in Doppelwerk accepted, from the new runner.
	view, err := waitForCheckIn(d, n.Container, started.Add(-2*time.Second), provenLimit)
	if err != nil {
		return rollback(d, n, out, fmt.Errorf("the new runner didn't check in: %w", err))
	}
	_, _ = d.Out("rm", "-f", n.Previous)
	for _, old := range strings.Fields(current) {
		// Only a release's images, pinned by digest: never a tag someone keeps for their own reasons.
		if old != target.Runner && old != target.Sandbox && strings.Contains(old, "@sha256:") {
			_, _ = d.Out("image", "rm", old)
		}
	}
	fmt.Fprintf(out, "Updated to %s: %q is online.\n", target.Version, view.Runtime.Name)
	return nil
}

// rollback puts the previous container back after a failed swap, and says what the new one logged.
func rollback(d Docker, n Names, out io.Writer, why error) error {
	logs, _ := d.Out("logs", "--tail", "20", n.Container)
	_, _ = d.Out("rm", "-f", n.Container)
	if _, err := d.Out("rename", n.Previous, n.Container); err != nil {
		return fmt.Errorf("%v; and putting the old runner back failed too (%v): it is container %s", why, err, n.Previous)
	}
	if _, err := d.Out("start", n.Container); err != nil {
		return fmt.Errorf("%v; the old runner is back but didn't start: %v", why, err)
	}
	if logs != "" {
		fmt.Fprintf(out, "The new runner's last lines:\n%s\n", logs)
	}
	return fmt.Errorf("%v. The previous version is running again", why)
}

func undrain(d Docker, n Names, why error) error {
	_, _ = d.Out("exec", n.Container, "doppel-runtime", "drain", "--off")
	return why
}

// ---- this command's own update ----------------------------------------------------------------------------------

func selfUpdate(target string, releases Releases, out io.Writer) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	resolved, err := filepath.EvalSymlinks(exe)
	if err != nil {
		resolved = exe
	}
	if strings.Contains(resolved, "/Cellar/") { // installed by Homebrew: its own upgrade
		fmt.Fprintf(out, "Updating this command to %s with Homebrew…\n", target)
		brew := exec.Command("brew", "upgrade", "summationai/tap/doppel-runtime")
		brew.Stdout, brew.Stderr = os.Stdout, os.Stderr
		if err := brew.Run(); err != nil {
			return fmt.Errorf("brew upgrade failed: %w", err)
		}
	} else {
		fmt.Fprintf(out, "Updating this command to %s…\n", target)
		asset := fmt.Sprintf("doppel-runtime_%s_%s", runtime.GOOS, runtime.GOARCH)
		bin, err := releases.Asset(target, asset)
		if err != nil {
			return err
		}
		sums, err := releases.Asset(target, "SHA256SUMS")
		if err != nil {
			return err
		}
		if err := verify(bin, sums, asset); err != nil {
			return err
		}
		tmp := resolved + ".new"
		if err := os.WriteFile(tmp, bin, 0o755); err != nil {
			return fmt.Errorf("couldn't write %s (run with the rights to replace it): %w", tmp, err)
		}
		if err := os.Rename(tmp, resolved); err != nil {
			os.Remove(tmp)
			return err
		}
	}
	// Run the new command with the same arguments; it updates the runner.
	return syscall.Exec(exe, os.Args, append(os.Environ(), "DOPPEL_HOST_UPDATED=1"))
}

// verify checks a downloaded file against the release's SHA256SUMS ("<hex>  <name>" lines).
func verify(data, sums []byte, name string) error {
	got := sha256.Sum256(data)
	scan := bufio.NewScanner(bytes.NewReader(sums))
	for scan.Scan() {
		fields := strings.Fields(scan.Text())
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name {
			if fields[0] == hex.EncodeToString(got[:]) {
				return nil
			}
			return fmt.Errorf("%s doesn't match the release's SHA256SUMS: not installed", name)
		}
	}
	return fmt.Errorf("%s isn't in the release's SHA256SUMS: not installed", name)
}

// newer: whether version a is newer than b, both X.Y.Z; a dev build is never older (nothing to compare).
func newer(a, b string) bool {
	pa, pb := parseVersion(a), parseVersion(b)
	if pa == nil || pb == nil {
		return false
	}
	for i := range 3 {
		if pa[i] != pb[i] {
			return pa[i] > pb[i]
		}
	}
	return false
}

func parseVersion(v string) []int {
	parts := strings.Split(strings.TrimPrefix(v, "v"), ".")
	if len(parts) != 3 {
		return nil
	}
	out := make([]int, 3)
	for i, p := range parts {
		n := 0
		if p == "" {
			return nil
		}
		for _, c := range p {
			if c < '0' || c > '9' {
				return nil
			}
			n = n*10 + int(c-'0')
		}
		out[i] = n
	}
	return out
}
