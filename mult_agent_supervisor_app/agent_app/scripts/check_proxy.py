#!/usr/bin/env python3
"""Pre-deploy smoke test: probe every wheel in uv.lock on the Databricks pip
proxy for the Apps runtime (Linux x86_64, CPython 3.11).

Why this exists
---------------
Databricks Apps install dependencies from ``pypi-proxy.cloud.databricks.com``,
a *lazy caching* mirror of PyPI. If a resolved wheel is not present it 404s and
the build fails. This script walks the resolved ``uv.lock`` and asks the proxy
whether each wheel exists, so a genuinely-missing wheel is caught before a
deploy is spent on it.

IMPORTANT — what this does and does NOT tell you
-----------------------------------------------
The proxy is edge/region-distributed and backfills lazily. A 200 from here (a
developer laptop) does NOT guarantee the workspace's build edge will serve the
same wheel at deploy time — we have observed a wheel return 200 here yet 404 on
the platform minutes later. So treat a green run as "the versions exist on the
proxy", not "the deploy is guaranteed".

The reliable way to get a clean one-shot deploy is to keep the lock on a version
set that a prior deploy already pulled successfully (see the note in
pyproject.toml). Use this script only as a fast existence smoke test after
``uv lock``:

    uv run check-proxy
"""

from __future__ import annotations

import concurrent.futures
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

# The Databricks Apps Python runtime. Keep in sync with .python-version.
TARGET_PYTHON = "cp311"
# Apps run on Linux x86_64.
LINUX_X86_MARKERS = ("manylinux", "linux_x86_64")
ARCH = "x86_64"
PROXY_HOST = "pypi-proxy.cloud.databricks.com"

LOCK_PATH = Path(__file__).resolve().parent.parent / "uv.lock"


def wheel_is_compatible(filename: str) -> bool:
    """True if this wheel would be selected by pip/uv on Apps (Linux x86_64, py3.11)."""
    name = filename.lower()
    if not name.endswith(".whl"):
        return False
    # Pure-Python wheels run anywhere.
    if name.endswith("-none-any.whl") and (
        "-py3-" in name or "-py2.py3-" in name or f"-{TARGET_PYTHON}-" in name
    ):
        return True
    # Compiled wheels: must target Linux x86_64.
    is_linux_x86 = any(m in name for m in LINUX_X86_MARKERS) and ARCH in name
    if not is_linux_x86:
        return False
    # ABI: exact cp311, or a stable-ABI (abi3) wheel built for <= 3.11.
    if f"-{TARGET_PYTHON}-" in name:
        return True
    if "-abi3-" in name:
        for tag in ("cp38", "cp39", "cp310", "cp311"):
            if f"-{tag}-abi3-" in name:
                return True
    return False


def pick_wheel(pkg: dict) -> tuple[str, str | None, str]:
    """Return (package_name, wheel_url_or_None, note) for a locked package."""
    name = pkg.get("name", "?")
    version = pkg.get("version", "")
    wheels = pkg.get("wheels", [])
    if not wheels:
        # Local/editable (source =) packages have no wheels — nothing to fetch.
        if "source" in pkg and pkg.get("source", {}).get("editable"):
            return name, None, "local (editable) — skipped"
        # sdist-only means Apps would compile it; that needs a toolchain and is
        # a latent risk, so surface it.
        return name, None, "NO WHEEL in lock (sdist-only) — may fail to build on Apps"

    candidates = [w["url"] for w in wheels if wheel_is_compatible(w["url"].rsplit("/", 1)[-1])]
    if not candidates:
        return name, None, f"no cp311/linux-x86_64 wheel among {len(wheels)} wheels"

    # Prefer a version-specific compiled wheel over a pure one when both exist;
    # either is fine, but this checks the blob the runtime actually pulls.
    candidates.sort(key=lambda u: ("none-any" in u.lower(), u))
    url = candidates[0]
    if PROXY_HOST not in url:
        note = f"lock points at non-proxy host: {url.split('/')[2]}"
    else:
        note = f"v{version}"
    return name, url, note


def check_url(url: str) -> tuple[int, str]:
    """Ask the proxy whether the wheel exists (cheap 1-byte range request).

    We only need presence, not the bytes, so request a single byte. 200/206 ==
    present. This intentionally does not download the whole wheel.
    """
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.reason
    except Exception as e:  # noqa: BLE001 - report any transport error verbatim
        return 0, str(e)


def main() -> int:
    if not LOCK_PATH.exists():
        print(f"uv.lock not found at {LOCK_PATH}. Run `uv lock` first.", file=sys.stderr)
        return 2

    with LOCK_PATH.open("rb") as fh:
        lock = tomllib.load(fh)

    packages = lock.get("package", [])
    picks = [pick_wheel(p) for p in packages]

    print(f"Checking {len(picks)} packages against {PROXY_HOST} "
          f"(target: Linux x86_64, {TARGET_PYTHON})\n")

    to_fetch = [(n, u, note) for (n, u, note) in picks if u]
    skipped = [(n, note) for (n, u, note) in picks if not u]

    failures: list[tuple[str, str, int, str]] = []
    results: dict[str, tuple[int, str]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(check_url, u): (n, u, note) for (n, u, note) in to_fetch}
        for fut in concurrent.futures.as_completed(futs):
            n, u, note = futs[fut]
            code, reason = fut.result()
            results[n] = (code, u)
            if code not in (200, 206):
                failures.append((n, u, code, reason))

    for n, note in sorted(skipped):
        # sdist-only is a real warning; local editable is benign.
        flag = "  ok " if "skipped" in note else "WARN "
        print(f"[{flag}] {n:<32} {note}")

    for n in sorted(results):
        code, u = results[n]
        ok = code in (200, 206)
        print(f"[{'  ok ' if ok else 'FAIL '}] {n:<32} HTTP {code}  {u.rsplit('/', 1)[-1]}")

    print()
    if failures:
        print(f"❌ {len(failures)} wheel(s) NOT downloadable from the proxy:")
        for n, u, code, reason in failures:
            print(f"   - {n}: HTTP {code} {reason}\n     {u}")
        print("\nFix: bump the offending package to a version whose Linux/cp311 wheel\n"
              "is present on the proxy (check https://pypi-proxy.cloud.databricks.com/simple/<pkg>/),\n"
              "re-run `uv lock`, then re-run `uv run check-proxy`. Do NOT deploy until this passes.")
        return 1

    print("✅ All required wheels exist on the Databricks proxy right now.\n"
          "   (Reminder: this is an existence smoke test, not a deploy guarantee — the\n"
          "   platform's build edge caches independently. Reliability comes from keeping\n"
          "   the lock on a proven, previously-deployed version set.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
