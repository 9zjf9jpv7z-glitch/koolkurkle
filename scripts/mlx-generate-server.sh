#!/bin/bash
# Durable mlx_lm.server start. PROCESS=mlx_lm.server. Thinking-off is load-bearing.
# Do not omit --chat-template-args. Do not start LM Studio.app / lms server.
set -euo pipefail
LOGDIR="${MAILROOM_MLX_LOGDIR:-$HOME/MailArchive/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/mlx-generate.log"
exec >>"$LOG" 2>&1
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) starting mlx_lm.server pid=$$ thinking=off"
MLX_PY="${MAILROOM_MLX_PY:-$HOME/MailArchive/venv-mlx/bin/python}"
MODEL="${MAILROOM_MLX_MODEL:-$HOME/.lmstudio/models/mlx-community/Qwen3.5-35B-A3B-4bit}"
if [ ! -x "$MLX_PY" ]; then
  echo "mlx python missing: $MLX_PY"
  exit 1
fi
if [ ! -d "$MODEL" ]; then
  echo "model dir missing: $MODEL"
  exit 1
fi
exec "$MLX_PY" -m mlx_lm.server \
  --model "$MODEL" \
  --host 127.0.0.1 \
  --port 1234 \
  --max-tokens 768 \
  --chat-template-args '{"enable_thinking":false}'
