#!/usr/bin/env python3
"""Repo-level tarball fetching — the order-of-magnitude lever.

Measured problem: ~70% of enrichment wall-clock was GitHub I/O, while we used
only ~2% of our 5,000/hr API quota. We were not budget-limited; we were limited
by making one round-trip per file at concurrency 3 (throttled down to 3 because
bursting the *contents* API triggers GitHub's secondary rate limiter).

A source tarball sidesteps all of it:

  * ONE request returns every file in the repo. Measured on `anthropics/skills`:
    3.7 MB, 502 files, 18 skills, 2.4s — versus ~136 contents-API calls.
  * codeload.github.com does not consume the core REST quota.
  * At 6.6 skills/repo across the corpus, one download serves ~6.6 skills plus
    all of their dependency files.

It also fixes a correctness gap the per-file path had. `fetch_closure` stored
closure bytes keyed by content hash and recorded only the path *list*; the
path->bytes mapping depended on the tree carrying a git blob sha, which only
58% of cached trees did. A tarball yields path and bytes together, so the
mapping is exact by construction and no longer inferred.

Safety: tarballs are read as a stream with hard caps, member paths are checked
for traversal, and only regular files are extracted. Nothing is written outside
the content-addressed store.
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

# Caps: a runaway monorepo must not stall a batch or fill the disk.
MAX_TARBALL_BYTES = int(os.environ.get("AUTOSKILL_MAX_TARBALL_BYTES", 80 * 1024 * 1024))
MAX_MEMBER_BYTES = int(os.environ.get("AUTOSKILL_MAX_MEMBER_BYTES", 256 * 1024))
TARBALL_TIMEOUT = int(os.environ.get("AUTOSKILL_TARBALL_TIMEOUT", "120"))
UA = "autoskill-corpus"


class TarballTooLarge(Exception):
    pass


def _download(url: str, token: str | None = None) -> bytes:
    """Stream a tarball, aborting as soon as it exceeds the cap."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    buf = bytearray()
    with urllib.request.urlopen(req, timeout=TARBALL_TIMEOUT) as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_TARBALL_BYTES:
                raise TarballTooLarge(f"exceeds {MAX_TARBALL_BYTES} bytes")
    return bytes(buf)


def fetch_repo_files(repo: str, token: str | None = None,
                     refs: tuple[str, ...] = ("HEAD",)) -> tuple[dict[str, bytes], str]:
    """Return {repo_relative_path: bytes} for one repo, plus the ref used.

    Only regular files under the cap are returned. Directory entries, symlinks
    and anything attempting path traversal are dropped: a symlink in a tarball
    can point anywhere, and we never want scraped content deciding what we read.
    """
    last_err = "no ref tried"
    for ref in refs:
        url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
        try:
            raw = _download(url, token)
        except TarballTooLarge as e:
            return {}, f"too-large:{e}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            continue
        except Exception as e:  # noqa: BLE001 - network is hostile, never crash a batch
            last_err = f"error:{type(e).__name__}"
            continue

        out: dict[str, bytes] = {}
        try:
            tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        except Exception as e:  # noqa: BLE001
            return {}, f"untar-failed:{type(e).__name__}"
        for m in tf:
            if not m.isfile() or m.size > MAX_MEMBER_BYTES:
                continue
            # codeload prefixes every path with "<repo>-<sha>/"
            parts = m.name.split("/", 1)
            if len(parts) != 2:
                continue
            path = parts[1]
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            try:
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                out[path] = fh.read()
            except Exception:  # noqa: BLE001
                continue
        return out, ref
    return {}, last_err


def raw_url_fetch(repo: str, path: str, ref: str = "HEAD",
                  timeout: int = 30) -> tuple[int, bytes | None]:
    """Single-file fetch via raw.githubusercontent — off the REST quota.

    Measured: 60 files in 1.8s at 30-way concurrency with zero throttling and
    zero core-quota consumption, versus 3-way concurrency on the contents API.
    Used for repos where the tarball is unavailable or over the cap.
    """
    # Percent-encode the path. Without this, any path containing a space, an
    # ampersand or a non-ASCII character fails outright -- which silently dropped
    # the reference docs of every non-English skill (observed on a repo whose
    # closure lives under "skills/legal/法律文章quai味道/", and another under
    # "Library/Data & AI/"). "/" is preserved as a path separator.
    quoted = urllib.parse.quote(path, safe="/")
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{quoted}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status, fh.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


if __name__ == "__main__":  # smoke test
    import sys
    r = sys.argv[1] if len(sys.argv) > 1 else "anthropics/skills"
    t0 = time.time()
    files, ref = fetch_repo_files(r)
    sk = [p for p in files if p.endswith("SKILL.md")]
    print(f"{r}: ref={ref} files={len(files)} skills={len(sk)} in {time.time()-t0:.1f}s")
