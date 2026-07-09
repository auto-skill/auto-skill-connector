"""Local-only launcher: loads OAuth client id/secret from a file outside the
repo (~/.autoskill/backend_oauth.env, same convention as the CLI's own
credentials.json) into the environment, then runs scraper.py in-process --
so the secret values never appear as command-line arguments in process
listings. Not part of the package; delete once these are set persistently
some other way (setx, a real secrets manager, etc).
"""
import os
import runpy
from pathlib import Path

env_path = Path.home() / ".autoskill" / "backend_oauth.env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()

# auth.py's own default fallback now points at the planned autoskill.dev
# domain, not what this machine's cloudflared tunnel actually serves -- pin
# it explicitly so this doesn't silently start building OAuth redirect URIs
# for a domain that isn't registered against the Google/GitHub OAuth apps.
os.environ.setdefault("BACKEND_BASE_URL", "https://skills.avalahome.com")

runpy.run_path(str(Path(__file__).parent / "scraper.py"), run_name="__main__")
