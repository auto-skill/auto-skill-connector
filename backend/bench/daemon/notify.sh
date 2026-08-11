#!/bin/bash
# Post a message to the Discord control channel using the existing bot
# credentials. Reads them at call time and never echoes them.
#
# Usage: notify.sh "message"
set -uo pipefail

MSG="${1:-}"
[[ -z "$MSG" ]] && exit 0

BOT_ENV=/home/sami/discord_codex/.env
[[ -f "$BOT_ENV" ]] || exit 0

python3 - "$MSG" <<'PY' 2>/dev/null
import json, os, re, sys, urllib.request, urllib.error

msg = "**[AutoSkill corpus]** " + sys.argv[1][:1850]
env = {}
try:
    with open("/home/sami/discord_codex/.env", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip("\"'")
except Exception:
    sys.exit(0)

tok = env.get("DISCORD_TOKEN")
# Post into the codex-autoskill thread, not the shared codex-console channel:
# corpus alerts mixed into the console were hard to tell apart from bot chatter.
# Overridable, and falls back to the control channel only if the thread is unset.
ch = (os.environ.get("AUTOSKILL_DISCORD_CHANNEL_ID")
      or "1524224001247805594"
      or env.get("DISCORD_CODEX_CONTROL_CHANNEL_ID"))
if not tok or not ch:
    sys.exit(0)

body = json.dumps({"content": msg, "allowed_mentions": {"parse": []}}).encode()
req = urllib.request.Request(
    f"https://discord.com/api/v10/channels/{ch}/messages",
    data=body, method="POST",
    headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
             "User-Agent": "autoskill-daemon"})
for attempt in range(3):
    try:
        with urllib.request.urlopen(req, timeout=25):
            sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 429 and attempt < 2:
            import time; time.sleep(5 * (attempt + 1)); continue
        sys.exit(0)
    except Exception:
        import time; time.sleep(3)
sys.exit(0)
PY
exit 0
