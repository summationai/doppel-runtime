// doppel-runtime, the host command (docs/specs/packaging.md): installs, pairs, updates and removes a Doppelwerk
// runner, which runs in Docker. It holds no review logic and keeps no files: what it made is named and labelled in
// Docker, and the runner's state is in its volume. Seats, status and logs pass through to the runner.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// Set at release (-ldflags -X): this build's version and the images it runs, by digest.
var (
	version      = "dev"
	runnerImage  = "ghcr.io/summationai/doppel-runtime:dev"
	sandboxImage = "ghcr.io/summationai/doppel-sandbox:dev"
)

const usage = `doppel-runtime %s: a Doppelwerk runner, in Docker (docker 26 or later).

  doppel-runtime pair CODE --bff URL [--replace]   install the runner if needed, then pair it with Doppelwerk
  doppel-runtime seat add CODE                     add your seat: approve the Codex sign-in it shows
  doppel-runtime status                            this command, the runner, and its seats
  doppel-runtime logs [-f]                         the runner's log
  doppel-runtime update [--version X.Y.Z] [--now]  update to the latest release (or that one)
  doppel-runtime doctor                            check what a runner needs, and say what to fix
  doppel-runtime uninstall [--yes]                 sign every seat out and remove the runner
  doppel-runtime version
`

func main() {
	os.Exit(run(cli{}, os.Args[1:], os.Stdin, os.Stdout))
}

func run(d Docker, args []string, stdin io.Reader, out io.Writer) int {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		fmt.Fprintf(out, usage, version)
		return 0
	}
	var err error
	switch args[0] {
	case "version", "--version":
		fmt.Fprintf(out, "doppel-runtime %s\n", version)
	case "doctor":
		if !doctor(d, out, "").ok() {
			return 1
		}
	case "pair":
		err = pairCmd(d, args[1:], out)
	case "seat":
		err = seatCmd(d, args[1:])
	case "status":
		err = statusCmd(d, out)
	case "logs":
		err = logsCmd(d, args[1:])
	case "update":
		err = updateCmd(d, args[1:], out, newReleases())
	case "uninstall":
		err = uninstallCmd(d, args[1:], stdin, out)
	default:
		fmt.Fprintf(os.Stderr, "doppel-runtime: no command %q\n\n", args[0])
		fmt.Fprintf(os.Stderr, usage, version)
		return 2
	}
	if err != nil {
		var u usageError
		fmt.Fprintf(os.Stderr, "doppel-runtime: %v\n", err)
		if errors.As(err, &u) {
			return 2
		}
		return 1
	}
	return 0
}

type usageError struct{ msg string }

func (u usageError) Error() string { return u.msg }

// ---- pair --------------------------------------------------------------------------------------------------------

func pairCmd(d Docker, args []string, out io.Writer) error {
	fs := flag.NewFlagSet("pair", flag.ContinueOnError)
	bff := fs.String("bff", "", "Doppelwerk's address, e.g. https://doppelwerk.ac0.app")
	replace := fs.Bool("replace", false, "pair again, replacing this runner's pairing")
	code, rest := firstArg(args)
	if err := fs.Parse(rest); err != nil {
		return usageError{err.Error()}
	}
	if code == "" || !strings.HasPrefix(code, "dwp_") {
		return usageError{"pair needs a pair code (dwp_…): Doppelwerk → Runners → Add a runner"}
	}
	if *bff == "" {
		return usageError{"say where Doppelwerk is: --bff https://<its address>"}
	}
	if !doctor(d, out, *bff).ok() {
		return errors.New("fix what doctor found, then run the same command again")
	}
	n, im := names(), builtInImages()
	if err := install(d, n, im); err != nil {
		return err
	}
	args = []string{"exec", "-i", n.Container, "doppel-runtime", "pair", "-", "--bff", *bff}
	if *replace {
		args = append(args, "--replace")
	}
	// The code goes on stdin: never in an argument a process list shows, nor in the container's environment.
	if err := d.Stream(strings.NewReader(code+"\n"), args...); err != nil {
		return errors.New("not paired (the runner said why, above)")
	}
	view, err := waitForCheckIn(d, n.Container, time.Time{}, 90*time.Second)
	if err != nil {
		return fmt.Errorf("paired, but no check-in yet: %v. Run `doppel-runtime status` in a minute, or `doppel-runtime logs`", err)
	}
	fmt.Fprintf(out, "Paired: %q in %q. Online.\nEach member who pays adds their seat: Runners → Add my seat, then `doppel-runtime seat add <code>`.\n",
		view.Runtime.Name, view.Runtime.Organization)
	return nil
}

// install makes what the runner needs and starts it, leaving whatever is already there and running.
func install(d Docker, n Names, im Images) error {
	for _, ref := range []string{im.Runner, im.Sandbox} {
		if err := ensureImage(d, ref); err != nil {
			return err
		}
	}
	if !exists(d, "volume", n.Volume) {
		if _, err := d.Out("volume", "create", "--label", label+"=1", n.Volume); err != nil {
			return err
		}
	}
	if !exists(d, "network", n.Network) {
		if _, err := d.Out("network", "create", "--label", label+"=1", n.Network); err != nil {
			return err
		}
	}
	if exists(d, "container", n.Container) {
		state, _ := d.Out("inspect", "--format", "{{.State.Running}}", n.Container)
		if state != "true" {
			if _, err := d.Out("start", n.Container); err != nil {
				return err
			}
		}
	} else if _, err := d.Out(runArgs(n, im)...); err != nil {
		return err
	}
	return waitForDaemon(d, n.Container, 60*time.Second)
}

func waitForDaemon(d Docker, container string, limit time.Duration) error {
	deadline := time.Now().Add(limit)
	for {
		if _, err := runnerView(d, container); err == nil {
			return nil
		} else if time.Now().After(deadline) {
			return fmt.Errorf("the runner didn't start: %v", err)
		}
		time.Sleep(pollEvery)
	}
}

var pollEvery = time.Second

// waitForCheckIn waits for a check-in at or after `since` that Doppelwerk accepted.
func waitForCheckIn(d Docker, container string, since time.Time, limit time.Duration) (RunnerView, error) {
	deadline := time.Now().Add(limit)
	var last error
	for time.Now().Before(deadline) {
		view, err := runnerView(d, container)
		switch {
		case err != nil:
			last = err
		case !view.Paired:
			last = errors.New("not paired")
		case view.LastError != nil:
			last = errors.New(*view.LastError)
		case view.LastCheckIn != nil && checkedInSince(*view.LastCheckIn, since):
			return view, nil
		default:
			last = errors.New("waiting for the first check-in")
		}
		time.Sleep(pollEvery)
	}
	return RunnerView{}, last
}

func checkedInSince(at string, since time.Time) bool {
	if since.IsZero() {
		return true
	}
	t, err := time.Parse(time.RFC3339Nano, at)
	return err == nil && !t.Before(since)
}

// ---- seats, status, logs ------------------------------------------------------------------------------------------

func seatCmd(d Docker, args []string) error {
	if len(args) < 2 || args[0] != "add" || !strings.HasPrefix(args[1], "dws_") {
		return usageError{"seat add needs your seat code (dws_…): Doppelwerk → Runners → Add my seat"}
	}
	n := names()
	if !exists(d, "container", n.Container) {
		return errors.New("no runner here yet: pair one first (`doppel-runtime pair …`)")
	}
	// The runner prints the sign-in link and code, and waits for the approval.
	return d.Stream(strings.NewReader(args[1]+"\n"), "exec", "-i", n.Container, "doppel-runtime", "seat", "add", "-")
}

func statusCmd(d Docker, out io.Writer) error {
	n := names()
	fmt.Fprintf(out, "doppel-runtime %s (this command)\n", version)
	if !exists(d, "container", n.Container) {
		fmt.Fprintln(out, "No runner here. Pair one: doppel-runtime pair <code> --bff <Doppelwerk's address>")
		return nil
	}
	labels, _ := d.Out("inspect", "--format",
		`{{index .Config.Labels "`+label+`.version"}} {{.Config.Image}} {{.State.Status}}`, n.Container)
	fmt.Fprintf(out, "runner container %s: %s\n", n.Container, labels)
	return d.Stream(nil, "exec", n.Container, "doppel-runtime", "status")
}

func logsCmd(d Docker, args []string) error {
	n := names()
	tail := []string{"logs", "--tail", "200"}
	for _, a := range args {
		if a == "-f" || a == "--follow" {
			tail = append(tail, "-f")
		}
	}
	return d.Stream(nil, append(tail, n.Container)...)
}

// ---- uninstall ---------------------------------------------------------------------------------------------------

func uninstallCmd(d Docker, args []string, stdin io.Reader, out io.Writer) error {
	yes := len(args) > 0 && (args[0] == "--yes" || args[0] == "-y")
	n := names()
	if exists(d, "container", n.Container) {
		if view, err := runnerView(d, n.Container); err == nil && view.Paired {
			if _, err := d.Out("exec", n.Container, "doppel-runtime", "forget"); err != nil {
				return fmt.Errorf("couldn't sign the seats out (%v); nothing was removed", err)
			}
			fmt.Fprintf(out, "Signed every seat out. Remove %q in Doppelwerk too: Runners → Remove.\n", view.Runtime.Name)
		}
	}
	for _, c := range []string{n.Container, n.Previous} {
		_, _ = d.Out("rm", "-f", c)
	}
	_, _ = d.Out("network", "rm", n.Network)
	if exists(d, "volume", n.Volume) {
		if !yes {
			fmt.Fprintf(out, "Delete the runner's data too (volume %s: its mirrors and ledger; the sign-ins are already gone)? [y/N] ", n.Volume)
			var answer string
			_, _ = fmt.Fscanln(stdin, &answer)
			yes = strings.EqualFold(strings.TrimSpace(answer), "y")
		}
		if yes {
			if _, err := d.Out("volume", "rm", n.Volume); err != nil {
				return err
			}
		} else {
			fmt.Fprintf(out, "Kept volume %s.\n", n.Volume)
		}
	}
	fmt.Fprintln(out, "Removed the runner.")
	return nil
}

// firstArg takes the one positional argument out of "CODE --bff URL", in either order (Go's flag package stops at the
// first non-flag).
func firstArg(args []string) (string, []string) {
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--bff" || a == "-bff" {
			i++ // its value
			continue
		}
		if !strings.HasPrefix(a, "-") {
			return a, append(append([]string{}, args[:i]...), args[i+1:]...)
		}
	}
	return "", args
}
