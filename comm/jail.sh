#!/usr/bin/env bash
set -euo pipefail

stty cols 80 rows 24

exec /home/user/nsjail --config /home/user/nsjail.cfg -- /osaibot-bash
