"""Apply limits in a disposable process, then replace it with FFmpeg.

No shell, no preexec_fn (which is unsafe with worker/server threads). Linux
additionally limits address space; macOS lacks a reliable RLIMIT_AS, so its
memory protection is FFmpeg's per-allocation cap, single threads and bounded
input/output, not a claimed hard aggregate RAM quota.
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command or not os.path.isabs(command[0]):
        raise SystemExit("An absolute decoder executable is required.")
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (args.file_bytes, args.file_bytes))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        descriptors = 64 if hard == resource.RLIM_INFINITY else min(64, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (descriptors, descriptors))
        if sys.platform.startswith("linux"):
            limit = args.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
