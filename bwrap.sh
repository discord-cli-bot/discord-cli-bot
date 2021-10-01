#!/usr/bin/env bash
set -euo pipefail

stty cols 80 rows 24
exec env -i TERM=linux COLUMNS=80 LINES=24 HOME="$HOME" \
  bwrap --ro-bind / / \
    --ro-bind /tmp/socket /tmp/socket \
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
    ~/osai-bot/bash/bash -l
