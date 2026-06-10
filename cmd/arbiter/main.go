package main

import (
	"fmt"
	"io"
	"os"
)

const version = "dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) != 1 {
		printUsage(stderr)
		return 2
	}

	switch args[0] {
	case "version":
		fmt.Fprintln(stdout, versionString())
		return 0
	default:
		printUsage(stderr)
		return 2
	}
}

func versionString() string {
	return "arbiter " + version
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "usage: arbiter version")
}
