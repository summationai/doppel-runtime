package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"
)

// fakeDocker answers docker commands from a handler and records them.
type fakeDocker struct {
	calls  [][]string
	stdin  []string
	handle func(args []string) (string, error)
}

func (f *fakeDocker) Out(args ...string) (string, error) {
	f.calls = append(f.calls, args)
	return f.handle(args)
}

func (f *fakeDocker) Stream(in io.Reader, args ...string) error {
	f.calls = append(f.calls, args)
	if in != nil {
		b, _ := io.ReadAll(in)
		f.stdin = append(f.stdin, string(b))
	}
	_, err := f.handle(args)
	return err
}

func (f *fakeDocker) ran(prefix ...string) bool {
	for _, c := range f.calls {
		if len(c) >= len(prefix) && strings.Join(c[:len(prefix)], " ") == strings.Join(prefix, " ") {
			return true
		}
	}
	return false
}

func (f *fakeDocker) all() string {
	var b strings.Builder
	for _, c := range f.calls {
		b.WriteString(strings.Join(c, " ") + "\n")
	}
	return b.String()
}

const info28 = `{"ServerVersion":"28.3.3","OSType":"linux","Architecture":"aarch64","MemTotal":8589934592,"Runtimes":{"runc":{}}}`

func init() {
	pollEvery = time.Millisecond
	probe = func(string) error { return nil }
	diskFree = func(string) uint64 { return 0 }
}

func statusJSON(checkIn time.Time, extra string) string {
	return fmt.Sprintf(`{"daemon":true,"paired":true,"runtime":{"name":"Sum MacMini","organization":"Summation","url":"https://x"},"last_check_in":%q,"last_error":null,"jobs":0%s}`,
		checkIn.Format(time.RFC3339Nano), extra)
}

func TestPairInstallsStartsAndPairsWithTheCodeOnStdinOnly(t *testing.T) {
	made := map[string]bool{}
	f := &fakeDocker{handle: func(a []string) (string, error) {
		switch {
		case a[0] == "info":
			return info28, nil
		case a[0] == "image" && a[1] == "inspect":
			return "", errors.New("no such image") // pulled
		case a[0] == "pull":
			return "", nil
		case (a[0] == "volume" || a[0] == "network" || a[0] == "container") && a[1] == "inspect":
			if made[a[0]] {
				return "{}", nil
			}
			return "", errors.New("no such object")
		case a[1] == "create":
			made[a[0]] = true
			return "", nil
		case a[0] == "run":
			made["container"] = true
			return "abc", nil
		case a[0] == "exec" && strings.Join(a[2:], " ") == "doppel-runtime status --json":
			return statusJSON(time.Now(), ""), nil
		}
		return "", nil
	}}
	var out bytes.Buffer
	code := "dwp_" + strings.Repeat("A", 43)
	if rc := run(f, []string{"pair", code, "--bff", "https://doppelwerk.example"}, nil, &out); rc != 0 {
		t.Fatalf("pair exited %d:\n%s\n%s", rc, out.String(), f.all())
	}
	if strings.Contains(f.all(), code) {
		t.Fatalf("the pair code appeared in a docker command's arguments:\n%s", f.all())
	}
	if len(f.stdin) != 1 || strings.TrimSpace(f.stdin[0]) != code {
		t.Fatalf("the pair code should go on stdin once, got %q", f.stdin)
	}
	for _, want := range [][]string{{"pull", runnerImage}, {"pull", sandboxImage}, {"volume", "create"}, {"network", "create"},
		{"exec", "-i", "doppel-runtime", "doppel-runtime", "pair", "-", "--bff", "https://doppelwerk.example"}} {
		if !f.ran(want...) {
			t.Errorf("expected docker %v:\n%s", want, f.all())
		}
	}
	runLine := ""
	for _, c := range f.calls {
		if c[0] == "run" {
			runLine = strings.Join(c, " ")
		}
	}
	for _, want := range []string{"--restart unless-stopped", "-v doppel-runtime-data:/var/lib/doppel-runtime",
		"-v /var/run/docker.sock:/var/run/docker.sock", "DOPPEL_SANDBOX_DATA_VOLUME=doppel-runtime-data",
		"DOPPEL_SANDBOX_NETWORK=doppel-runtime", "DOPPEL_SANDBOX_GATEWAY_TARGET=doppel-runtime:8700",
		"--label dev.doppelwerk.runtime=1", "DOPPEL_SANDBOX_IMAGE=" + sandboxImage} {
		if !strings.Contains(runLine, want) {
			t.Errorf("runner container is missing %q: %s", want, runLine)
		}
	}
	if !strings.Contains(out.String(), `Paired: "Sum MacMini" in "Summation". Online.`) {
		t.Errorf("unexpected output:\n%s", out.String())
	}
}

func TestPairRefusesWithoutACodeOrAnAddress(t *testing.T) {
	f := &fakeDocker{handle: func([]string) (string, error) { return "", nil }}
	if rc := run(f, []string{"pair", "--bff", "https://x"}, nil, io.Discard); rc != 2 {
		t.Errorf("no code: exit %d", rc)
	}
	if rc := run(f, []string{"pair", "dwp_x"}, nil, io.Discard); rc != 2 {
		t.Errorf("no --bff: exit %d", rc)
	}
	if len(f.calls) != 0 {
		t.Errorf("nothing should run: %s", f.all())
	}
}

func TestDoctorRequiresDocker26AndReportsGVisor(t *testing.T) {
	old := &fakeDocker{handle: func(a []string) (string, error) {
		return `{"ServerVersion":"25.0.5","OSType":"linux","Architecture":"x86_64","MemTotal":8589934592}`, nil
	}}
	var out bytes.Buffer
	if doctor(old, &out, "").ok() {
		t.Fatalf("Docker 25 should fail:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "✗ Docker 25.0.5: a runner needs Docker 26 or later") {
		t.Errorf("unexpected:\n%s", out.String())
	}
	gv := &fakeDocker{handle: func(a []string) (string, error) {
		return `{"ServerVersion":"28.3.3","OSType":"linux","Architecture":"x86_64","MemTotal":8589934592,"Runtimes":{"runsc":{}}}`, nil
	}}
	out.Reset()
	if !doctor(gv, &out, "https://doppelwerk.example").ok() || !strings.Contains(out.String(), "✓ gVisor: reviews will run under gVisor") {
		t.Errorf("unexpected:\n%s", out.String())
	}
	down := &fakeDocker{handle: func(a []string) (string, error) { return "", errors.New("Cannot connect") }}
	out.Reset()
	if doctor(down, &out, "").ok() || !strings.Contains(out.String(), "Docker isn't running") {
		t.Errorf("unexpected:\n%s", out.String())
	}
}

type fakeReleases struct {
	r      Release
	assets map[string][]byte
}

func (f fakeReleases) Get(string) (Release, error) { return f.r, nil }
func (f fakeReleases) Asset(_, name string) ([]byte, error) {
	if b, ok := f.assets[name]; ok {
		return b, nil
	}
	return nil, errors.New("no such asset")
}

// updateWorld: a runner on "old" images whose Doppelwerk needs protocol 1 or later; the new runner checks in when
// healthy, or reports the BFF's refusal and never checks in.
func updateWorld(healthy bool) *fakeDocker {
	started := false
	return &fakeDocker{handle: func(a []string) (string, error) {
		switch {
		case a[0] == "info":
			return info28, nil
		case a[0] == "container" && a[1] == "inspect":
			return "{}", nil
		case a[0] == "inspect":
			return "ghcr.io/x/runner@sha256:old ghcr.io/x/sandbox@sha256:old", nil
		case a[0] == "image" && a[1] == "inspect":
			return "sha", nil
		case a[0] == "run":
			started = true
			return "new", nil
		case a[0] == "rename" && a[1] == "doppel-runtime-previous":
			started = false // the old one is back
			return "", nil
		case a[0] == "exec" && strings.Join(a[2:], " ") == "doppel-runtime status --json":
			if started && !healthy {
				return `{"daemon":true,"paired":true,"last_check_in":null,"last_error":"the BFF answered 400","runtime":{"name":"n","organization":"o"}}`, nil
			}
			return statusJSON(time.Now().Add(time.Second), `,"bff_protocol":{"current":2,"minimum":1}`), nil
		}
		return "", nil
	}}
}

func TestUpdateDrainsSwapsProvesAndCleansUp(t *testing.T) {
	f := updateWorld(true)
	rel := fakeReleases{r: Release{Version: "0.3.0", Protocol: 2, RunnerImage: "ghcr.io/x/runner@new", SandboxImage: "ghcr.io/x/sandbox@new"}}
	var out bytes.Buffer
	if err := updateCmd(f, nil, &out, rel); err != nil {
		t.Fatalf("update: %v\n%s\n%s", err, out.String(), f.all())
	}
	order := []string{"exec doppel-runtime doppel-runtime drain", "stop -t 30 doppel-runtime", "rename doppel-runtime doppel-runtime-previous", "run -d --name doppel-runtime", "rm -f doppel-runtime-previous", "image rm ghcr.io/x/runner@sha256:old"}
	log := f.all()
	at := 0
	for _, step := range order {
		i := strings.Index(log[at:], step)
		if i < 0 {
			t.Fatalf("expected %q after position %d in:\n%s", step, at, log)
		}
		at += i
	}
	if !strings.Contains(out.String(), "Updated to 0.3.0") {
		t.Errorf("unexpected:\n%s", out.String())
	}
}

func TestUpdatePutsTheOldRunnerBackWhenTheNewOneDoesntCheckIn(t *testing.T) {
	provenLimit = 50 * time.Millisecond
	defer func() { provenLimit = 2 * time.Minute }()
	f := updateWorld(false)
	rel := fakeReleases{r: Release{Version: "0.3.0", Protocol: 2, RunnerImage: "ghcr.io/x/runner@new", SandboxImage: "ghcr.io/x/sandbox@new"}}
	var out bytes.Buffer
	err := updateCmd(f, nil, &out, rel)
	if err == nil || !strings.Contains(err.Error(), "The previous version is running again") {
		t.Fatalf("expected a rollback, got %v\n%s", err, f.all())
	}
	for _, want := range [][]string{{"rm", "-f", "doppel-runtime"}, {"rename", "doppel-runtime-previous", "doppel-runtime"}, {"start", "doppel-runtime"}} {
		if !f.ran(want...) {
			t.Errorf("expected docker %v:\n%s", want, f.all())
		}
	}
	if f.ran("image", "rm") {
		t.Errorf("a rollback keeps the images:\n%s", f.all())
	}
}

func TestUpdateRefusesAVersionTheBFFNoLongerAccepts(t *testing.T) {
	f := updateWorld(true)
	rel := fakeReleases{r: Release{Version: "0.1.0", Protocol: 0, RunnerImage: "r@x", SandboxImage: "s@x"}}
	err := updateCmd(f, []string{"--version", "0.1.0"}, io.Discard, rel)
	if err == nil || !strings.Contains(err.Error(), "needs 1 or later") {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if f.ran("stop") {
		t.Errorf("nothing should change:\n%s", f.all())
	}
}

func TestUninstallSignsOutThenRemovesEverything(t *testing.T) {
	f := &fakeDocker{handle: func(a []string) (string, error) {
		if a[0] == "exec" && strings.Join(a[2:], " ") == "doppel-runtime status --json" {
			return statusJSON(time.Now(), ""), nil
		}
		return "{}", nil
	}}
	var out bytes.Buffer
	if rc := run(f, []string{"uninstall", "--yes"}, nil, &out); rc != 0 {
		t.Fatalf("exit %d:\n%s", rc, out.String())
	}
	for _, want := range [][]string{{"exec", "doppel-runtime", "doppel-runtime", "forget"}, {"rm", "-f", "doppel-runtime"},
		{"network", "rm", "doppel-runtime"}, {"volume", "rm", "doppel-runtime-data"}} {
		if !f.ran(want...) {
			t.Errorf("expected docker %v:\n%s", want, f.all())
		}
	}
	if !strings.Contains(out.String(), `Remove "Sum MacMini" in Doppelwerk too`) {
		t.Errorf("unexpected:\n%s", out.String())
	}
}

func TestFirstArgTakesTheCodeInEitherOrder(t *testing.T) {
	for _, args := range [][]string{{"dwp_a", "--bff", "https://x"}, {"--bff", "https://x", "dwp_a"}, {"--bff=https://x", "dwp_a"}} {
		code, rest := firstArg(args)
		if code != "dwp_a" || strings.Contains(strings.Join(rest, " "), "dwp_a") {
			t.Errorf("%v: got %q %v", args, code, rest)
		}
	}
}

func TestNewerAndVerify(t *testing.T) {
	if !newer("0.10.0", "0.9.0") || newer("1.0.0", "1.0.0") || newer("1.0.0", "dev") || newer("dev", "0.1.0") {
		t.Error("newer")
	}
	bin := []byte("binary")
	sum := sha256.Sum256(bin)
	sums := []byte(hex.EncodeToString(sum[:]) + "  doppel-runtime_linux_arm64\n")
	if err := verify(bin, sums, "doppel-runtime_linux_arm64"); err != nil {
		t.Error(err)
	}
	if verify([]byte("tampered"), sums, "doppel-runtime_linux_arm64") == nil {
		t.Error("a changed binary must fail")
	}
	if verify(bin, sums, "doppel-runtime_darwin_arm64") == nil {
		t.Error("a binary missing from SHA256SUMS must fail")
	}
}
