#!/bin/sh
# Template only. Provision a dedicated restricted SSH identity; never embed a key.
# The remote files are unchanged M0 preflight/node_supervisor copies at this commit.
set -eu
/usr/bin/ssh -F /etc/agentseek-m2-broker/ssh_config cube-m2-gate \
  '/usr/bin/python3 /opt/agentseek-m2-supervisor/preflight.py --manifest /var/lib/agentseek-m2-supervisor/manifest.json --alarm-file /var/lib/agentseek-m2-supervisor/alarm' >/dev/null
printf '%s\n' READY
