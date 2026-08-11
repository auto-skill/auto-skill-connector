#!/bin/bash
# OnFailure handler. Fires when a unit fails, including when it exceeds
# StartLimitBurst=3 restarts inside StartLimitIntervalSec=3600 — i.e. exactly
# the "dying more than 3 restarts in an hour" case.
source /srv/mobile-codex/autoskill-daemon/bin/common.sh

FAILED_UNIT="${1:-unknown}"

RESULT=$(systemctl --user show -p Result --value "$FAILED_UNIT" 2>/dev/null)
NRESTARTS=$(systemctl --user show -p NRestarts --value "$FAILED_UNIT" 2>/dev/null)
STATE=$(systemctl --user show -p ActiveState --value "$FAILED_UNIT" 2>/dev/null)
TAIL=$(tail -n 8 "$LOG_DIR/${FAILED_UNIT%%.service}" 2>/dev/null \
       || tail -n 8 "$LOG_DIR/$(echo "$FAILED_UNIT" | sed 's/autoskill-//; s/\.service//')-unit.log" 2>/dev/null \
       || echo "(no log tail available)")

status "alert" "unit=$FAILED_UNIT result=$RESULT restarts=$NRESTARTS state=$STATE"

notify ":rotating_light: **Auto-Skill unit failed — \`$FAILED_UNIT\`**
result=\`$RESULT\` · restarts=\`$NRESTARTS\` · state=\`$STATE\`

If restarts hit the limit (3/hour) systemd has stopped retrying and the unit is now down.
Recent log:
\`\`\`
$(echo "$TAIL" | tail -c 1200)
\`\`\`
Restart with: \`systemctl --user start $FAILED_UNIT\`"
exit 0
