#!/usr/bin/env bash
set -euo pipefail

stty cols 80 rows 24

LD_PRELOAD=/home/user/nsjail-close-fds.so exec /home/user/nsjail \
  --config /home/user/nsjail.cfg \
  --pass_fd "$SOCK_FD" \
  --pass_fd "$EXE_FD" \
  -- /proc/self/fd/"$EXE_FD"
