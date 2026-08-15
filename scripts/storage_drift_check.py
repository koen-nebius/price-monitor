"""
Daily drift probe for config.STORAGE_PRICES (the curated storage benchmark).

Fetches the cheap machine-readable sources and greps each provider's page for
the configured price; writes any mismatch/unreachable-source lines to
store/storage_drift.txt (cleared when all good). main.py folds that file into
run_manifest warnings (internal-only, never the exec broadcast). Soft-fail
everywhere: a broken probe must never block the pipeline.

Probe design: presence check, not parse — we search the fetched text for the
exact configured price string near relevant keywords. Cheap, low-false-positive
(a repriced page simply stops containing the old number).
"""
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import STORAGE_PRICES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "store" / "storage_drift.txt"
UA = {"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"}

# (section, provider) -> URL worth probing daily + the price-bearing keyword.
# Deliberately only the reliably static/server-rendered subset; the rest are
# re-verified manually on STORAGE_PRICES_VERIFIED refresh.
PROBES = {
    ("object", "backblaze"): ("https://www.backblaze.com/cloud-storage/pricing", "B2"),
    ("object", "cloudflare"): ("https://developers.cloudflare.com/r2/pricing/", "Standard"),
    ("object", "nebius"): ("https://nebius.com/prices", "Object"),
    ("object", "coreweave"): ("https://www.coreweave.com/pricing", "Object"),
    ("block", "nebius"): ("https://nebius.com/prices", "SSD"),
    ("block", "crusoe"): ("https://docs.crusoecloud.com/storage/disks/overview", "disk"),
    ("shared_fs", "together"): ("https://www.together.ai/pricing", "filesystem"),   # docs subdomain is a JS shell
    ("shared_fs", "lambda"): ("https://lambda.ai/service/gpu-cloud", "filesystem"),
}


def _variants(price: float):
    # 0.0147 -> "0.0147"; 0.07 -> "0.07" and "0.070"; 0.02 -> "0.02" and "0.020"
    s = f"{price:.4f}".rstrip("0").rstrip(".")
    out = {s}
    if len(s.split(".")[-1]) < 3:
        out.add(f"{price:.3f}")
    return out


def main() -> int:
    drift = []
    cache = {}
    for section, rows in STORAGE_PRICES.items():
        for r in rows:
            probe = PROBES.get((section, r["provider"]))
            if not probe:
                continue
            url, _kw = probe
            if url not in cache:
                try:
                    req = urllib.request.Request(url, headers=UA)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        cache[url] = resp.read().decode("utf-8", "replace")
                except Exception as e:
                    cache[url] = None
                    drift.append(f"storage-drift: {r['provider']} source unreachable ({e})")
            text = cache[url]
            if text is None:
                continue
            if not any(v in text for v in _variants(r["price"])):
                drift.append(
                    f"storage-drift: {r['provider']} {r['name']} — configured "
                    f"{r['price']} not found on {url} (page may have repriced; "
                    f"re-verify and update config.STORAGE_PRICES)")
    if drift:
        OUT.write_text("\n".join(sorted(set(drift))) + "\n")
        print("\n".join(sorted(set(drift))))
    else:
        if OUT.exists():
            OUT.unlink()
        print(f"storage drift check: all {len(PROBES)} probed sources match config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
