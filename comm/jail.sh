#!/usr/bin/env bash
set -euo pipefail

stty cols 80 rows 24

LD_PRELOAD=/home/user/nsjail-hooks.so exec /home/user/nsjail \
  --config /home/user/nsjail.cfg \
  --pass_fd "$SOCK_FD" \
  --pass_fd "$EXE_FD" \
  --bindmount /run/discord-upload-fuse/"$DISCORD_UPLOAD_UUID":/dev/discord \
  -- /proc/self/fd/"$EXE_FD"
