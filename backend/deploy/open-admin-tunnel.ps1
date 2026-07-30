# Opens the loopback-only SSH tunnel to the droplet's admin-local container
# (127.0.0.1:8002 on the server) so http://127.0.0.1:8002/admin is reachable
# from this machine. Uses the "autoskill-admin-tunnel" alias in ~/.ssh/config
# -- keep that window open; closing it drops the tunnel.

$ErrorActionPreference = "Stop"
Write-Host "Opening admin tunnel -- leave this window open. Then browse to http://127.0.0.1:8002/admin"
ssh -N autoskill-admin-tunnel
