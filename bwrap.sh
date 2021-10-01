#!/usr/bin/env bash
set -euo pipefail

ulimit -Sc 0
ulimit -v 4194304
ulimit -t 600
ulimit -f 8129
ulimit -n 128
ulimit -u 8192
ulimit -Ss 8192
ulimit -l 64
ulimit -r 0
ulimit -q 1024

stty cols 80 rows 24
exec env -i TERM=linux COLUMNS=80 LINES=24 HOME="$HOME" \
  bwrap --ro-bind / / \
    --dir /tmp \
    --proc /proc \
    --dev /dev \
    --chdir / \
    --unshare-ipc \
    --unshare-pid \
    --unshare-net \
    --unshare-uts \
    --unshare-cgroup-try \
    --hostname osaibot \
    --die-with-parent \
    --as-pid-1 \
    bash -l
