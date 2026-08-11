#!/usr/bin/env python3
"""What is raw.githubusercontent's actual concurrency ceiling for us?

Two stages are dominated by GitHub fetches and both are running at a concurrency
that was picked, not measured:

  combine/closure  CLOSURE_CONCURRENCY=20 -> `closure_wait` 396.7s of a 412s stage
  fetch            FETCH_CONCURRENCY=16   -> 322s

raw.githubusercontent.com is OFF the REST API quota, so the binding limit is
whatever the CDN and our network actually sustain, not the 5000/hr core budget.
That number has never been measured here.

Fires N concurrent GETs at real object URLs already in the corpus and reports
aggregate throughput plus the error mix at each level. The ceiling is the highest
level that is BOTH faster and clean -- a level that goes faster while returning
403s is not a ceiling, it is the start of the secondary rate limit that has
already cost this pipeline a batch of silently-lost closure files.

Read-only: issues GETs for content we already hold and discards the bytes.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))


def sample_urls(n: int) -> list[str]:
    """Real (repo, path) pairs from recent fetch caches -> raw URLs."""
    out: list[str] = []
    for f in sorted(BENCH.glob("run2_fetch_b*.json"))[-6:]:
        try:
            cache = json.loads(f.read_text())
        except Exception:
            continue
        for fe in cache.values():
            repo = fe.get("repo")
            sha = fe.get("commit_sha")
            if not repo or not sha:
                continue
            for e in (fe.get("tree") or []):
                if e.get("type") == "file" and e.get("path"):
                    out.append(f"https://raw.githubusercontent.com/{repo}/{sha}/{e['path']}")
                    if len(out) >= n * 3:
                        break
            if len(out) >= n * 3:
                break
        if len(out) >= n * 3:
            break
    random.Random(9).shuffle(out)
    return out[:n]


def get(url: str) -> tuple[int, float, int]:
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "autoskill-perf-probe"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, time.time() - t0, len(body)
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, 0
    except Exception:
        return -1, time.time() - t0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="16,32,64,96")
    ap.add_argument("--per-level", type=int, default=120)
    ap.add_argument("--out", default=str(BENCH / "exp_github_concurrency.json"))
    a = ap.parse_args()

    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    urls = sample_urls(a.per_level * len(levels))
    if len(urls) < a.per_level:
        print(f"only found {len(urls)} urls; reduce --per-level")
        return 1
    print(f"probing raw.githubusercontent with {len(urls)} real object URLs\n", flush=True)

    results = {}
    idx = 0
    for lvl in levels:
        chunk = urls[idx:idx + a.per_level]
        idx += a.per_level
        if len(chunk) < a.per_level // 2:
            break
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=lvl) as ex:
            rs = list(ex.map(get, chunk))
        wall = time.time() - t0
        codes = Counter(r[0] for r in rs)
        lat = [r[1] for r in rs]
        ok = codes.get(200, 0)
        bad = sum(v for k, v in codes.items() if k not in (200, 404))
        results[lvl] = {
            "n": len(chunk), "wall_s": round(wall, 1),
            "fetches_per_s": round(len(chunk) / wall, 1),
            "median_latency": round(st.median(lat), 2),
            "p95_latency": round(sorted(lat)[max(0, int(len(lat) * .95) - 1)], 2),
            "ok": ok, "not_found": codes.get(404, 0), "errors": bad,
            "codes": dict(codes),
        }
        r = results[lvl]
        flag = "  <-- ERRORS" if bad else ""
        print(f"  conc {lvl:>3}: {r['fetches_per_s']:>6.1f} fetch/s  "
              f"median {r['median_latency']:>5.2f}s  p95 {r['p95_latency']:>5.2f}s  "
              f"ok={ok} 404={r['not_found']} err={bad}{flag}", flush=True)
        if bad:
            print(f"      codes: {r['codes']}", flush=True)
        time.sleep(3)   # let any burst counter decay between levels

    print("\n=== READING ===")
    clean = {k: v for k, v in results.items() if v["errors"] == 0}
    if clean:
        best = max(clean, key=lambda k: clean[k]["fetches_per_s"])
        base = min(clean)
        gain = clean[best]["fetches_per_s"] / clean[base]["fetches_per_s"]
        print(f"  fastest CLEAN level: {best} at {clean[best]['fetches_per_s']} fetch/s "
              f"({gain:.1f}x over concurrency {base})")
        print(f"  current settings: CLOSURE_CONCURRENCY=20, FETCH_CONCURRENCY=16")
        cw = 396.7
        print(f"  closure_wait was {cw}s at 20 -> ~{cw * clean[base]['fetches_per_s'] / clean[best]['fetches_per_s']:.0f}s at {best}")
    dirty = [k for k, v in results.items() if v["errors"]]
    if dirty:
        print(f"  levels returning errors (do NOT use): {dirty}")
        print(f"  GitHub's secondary limit is a 403 burst response; a level that is")
        print(f"  faster while erroring silently loses closure files.")

    Path(a.out).write_text(json.dumps(results, indent=1))
    print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
