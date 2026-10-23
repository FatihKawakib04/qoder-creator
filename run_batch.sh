#!/bin/zsh
# Batch runner: try multiple signups, record outcomes.
cd "$(dirname "$0")"
source ../.venv/bin/activate

N=${1:-5}
for i in $(seq 1 $N); do
  echo "===== ATTEMPT $i/$N ====="
  python main.py signup -n 1 2>&1 | grep -aE "Email:|OTP:|Captcha solved|PAT valid|PAT invalid|FAIL|Account created|pending|Error|buttons=" | sed 's/\x1b\[[0-9;]*m//g'
  echo "---"
done
echo "===== DONE ====="
echo "Valid PATs so far:"
grep -c '"pat_valid": true' data/accounts.jsonl 2>/dev/null || echo 0
