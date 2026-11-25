package main

// doctor: what a runner needs, checked, and what to fix said plainly. Docker (26+, Linux containers) is required;
// memory, disk, gVisor and the network are reported but don't stop anything, since the runner itself says more.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const minDocker = 26

type check struct {
	name     string
	ok       bool
	required bool
	detail   string // what was found, or what to do
}

type report []check

func (r report) ok() bool {
	for _, c := range r {
		if c.required && !c.ok {
			return false
		}
	}
	return true
}

// dockerInfo is the part of `docker info` doctor reads.
type dockerInfo struct {
	ServerVersion string         `json:"ServerVersion"`
	OSType        string         `json:"OSType"`
	Architecture  string         `json:"Architecture"`
	MemTotal      int64          `json:"MemTotal"`
	DockerRootDir string         `json:"DockerRootDir"`
	Runtimes      map[string]any `json:"Runtimes"`
}

// probe answers whether an HTTPS address is reachable; tests swap it.
var probe = func(url string) error {
	client := http.Client{Timeout: 5 * time.Second}
	res, err := client.Head(url)
	if err != nil {
		return err
	}
	res.Body.Close()
	return nil
}

// diskFree is the free space where Docker keeps its data, when that's on this machine (Linux); 0 when unknown.
var diskFree = func(path string) uint64 {
	var st syscall.Statfs_t
	if path == "" || syscall.Statfs(path, &st) != nil {
		return 0
	}
	return st.Bavail * uint64(st.Bsize)
}

func doctor(d Docker, out io.Writer, bff string) report {
	var r report
	add := func(c check) { r = append(r, c) }

	if _, err := exec.LookPath("docker"); err != nil && isRealDocker(d) {
		add(check{"Docker", false, true, "the docker command isn't installed. Install Docker Desktop, OrbStack or Colima on a Mac, Docker Engine on Linux"})
		return r.print(out)
	}
	raw, err := d.Out("info", "--format", "{{json .}}")
	var info dockerInfo
	if err != nil || json.Unmarshal([]byte(raw), &info) != nil || info.ServerVersion == "" {
		add(check{"Docker", false, true, "Docker isn't running, or this user can't reach it. Start Docker (on Linux: add yourself to the docker group, then sign in again)"})
		return r.print(out)
	}
	major, _ := strconv.Atoi(strings.SplitN(info.ServerVersion, ".", 2)[0])
	add(check{"Docker " + info.ServerVersion, major >= minDocker, true,
		fmt.Sprintf("a runner needs Docker %d or later: update Docker", minDocker)})
	add(check{"Linux containers", info.OSType == "linux", true, "switch Docker to Linux containers"})
	add(check{"Architecture " + info.Architecture, info.Architecture == "x86_64" || info.Architecture == "aarch64", true,
		"runner images are built for x86_64 and arm64"})

	const gib = 1 << 30
	add(check{fmt.Sprintf("Memory %.0f GB for Docker", float64(info.MemTotal)/gib), info.MemTotal >= 4*gib, false,
		"give Docker at least 4 GB (Docker Desktop: Settings → Resources) for reviews to run side by side"})
	if free := diskFree(info.DockerRootDir); free > 0 {
		add(check{fmt.Sprintf("Disk %.0f GB free for Docker", float64(free)/gib), free >= 20*gib, false,
			"free at least 20 GB where Docker keeps its data: repository mirrors and images need room"})
	}
	if _, ok := info.Runtimes["runsc"]; ok {
		add(check{"gVisor: reviews will run under gVisor", true, false, ""})
	} else {
		add(check{"gVisor: not installed; reviews run with Docker's standard isolation", false, false,
			"on Linux, install gVisor and register it with Docker for stronger isolation: https://gvisor.dev/docs/user_guide/install/ (a Mac's Docker has no gVisor)"})
	}

	sites := []string{"https://github.com", "https://ghcr.io/v2/", "https://chatgpt.com"}
	if bff != "" {
		sites = append([]string{bff}, sites...)
	}
	for _, site := range sites {
		err := probe(site)
		detail := ""
		if err != nil {
			detail = fmt.Sprintf("can't reach it from here (%v); the runner needs outbound HTTPS to it", shortErr(err))
		}
		add(check{"Reaches " + site, err == nil, false, detail})
	}
	return r.print(out)
}

func (r report) print(out io.Writer) report {
	for _, c := range r {
		mark := "✓"
		if !c.ok {
			mark = "!"
			if c.required {
				mark = "✗"
			}
		}
		line := mark + " " + c.name
		if !c.ok && c.detail != "" {
			line += ": " + c.detail
		}
		fmt.Fprintln(out, line)
	}
	return r
}

func shortErr(err error) string {
	s := err.Error()
	if i := strings.LastIndex(s, ": "); i >= 0 {
		return s[i+2:]
	}
	return s
}

// isRealDocker: the docker command matters only when this is the real CLI (tests use a fake).
func isRealDocker(d Docker) bool { _, ok := d.(cli); return ok }
