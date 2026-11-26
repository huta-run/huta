"""Run the test suite in parallel: `python3 -I tests/run.py [-j N] [-v] [pattern ...]`.

The same tests as `python3 -I -m unittest discover -s tests -t .`, spread over worker
processes (standard library only). Almost all the time is the crash tests, so each of their
test methods is its own unit and the slowest units start first; every other test class is
one unit. The two step loops are split further: each shard crashes every n-th step
(HUTA_CRASH_SHARD, see tests/test_crash.py). Tests already isolate themselves (each fake host
is its own temporary directory), so units can run side by side. A pattern keeps only the test ids that contain it.
"""

import argparse
import concurrent.futures as cf
import io
import multiprocessing
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLOW_MODULES = ("tests.test_crash",)   # split per test method; everything else per class
SLOW_FIRST = ("test_reconcile_crashes_too", "test_crash_after_every_step")
SHARDS = {"test_reconcile_crashes_too": 4, "test_crash_after_every_step": 2}


def _flatten(suite):
    for x in suite:
        if isinstance(x, unittest.TestSuite):
            yield from _flatten(x)
        else:
            yield x


def units(patterns: list[str]) -> list[tuple[list[str], str]]:
    """Work units: (test ids, crash shard "i/n" or "")."""
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    errors = unittest.defaultTestLoader.errors
    if errors:
        sys.exit("".join(errors))
    groups: dict[str, list[str]] = {}
    for test in _flatten(suite):
        tid = test.id()
        if patterns and not any(p in tid for p in patterns):
            continue
        if tid.startswith("unittest.loader."):      # a module that failed to import
            sys.exit(f"cannot load {tid}")
        cls = tid.rsplit(".", 1)[0]
        key = tid if cls.rsplit(".", 1)[0] in SLOW_MODULES else cls
        groups.setdefault(key, []).append(tid)

    def cost(key):
        name = key.rsplit(".", 1)[-1]
        if name in SLOW_FIRST:
            return (0, SLOW_FIRST.index(name), key)
        return (1 if key.startswith(SLOW_MODULES) else 2, 0, key)
    out = []
    for k in sorted(groups, key=cost):
        n = SHARDS.get(k.rsplit(".", 1)[-1], 1) if k.startswith(SLOW_MODULES) else 1
        out += [(groups[k], f"{i}/{n}" if n > 1 else "") for i in range(n)]
    return out


def run_unit(unit: tuple[list[str], str]) -> dict:
    ids, shard = unit
    os.chdir(ROOT)
    if shard:                              # workers are reused: set or clear it every time
        os.environ["HUTA_CRASH_SHARD"] = shard
    else:
        os.environ.pop("HUTA_CRASH_SHARD", None)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames(ids)
    t0 = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=0, buffer=True).run(suite)
    return {"ids": ids, "shard": shard, "seconds": time.monotonic() - t0, "run": result.testsRun,
            "failures": [(str(t), tb) for t, tb in result.failures],
            "errors": [(str(t), tb) for t, tb in result.errors],
            "skipped": len(result.skipped), "expected": len(result.expectedFailures),
            "unexpected": [str(t) for t in result.unexpectedSuccesses]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("-v", "--verbose", action="store_true", help="print every unit's time")
    ap.add_argument("patterns", nargs="*")
    args = ap.parse_args()
    work = units(args.patterns)
    if not work:
        sys.exit("no tests match")
    t0 = time.monotonic()
    total = skipped = expected = 0
    bad: list[tuple[str, str, str]] = []
    unexpected: list[str] = []
    slowest: list[tuple[float, str]] = []
    # fork: workers inherit the loaded test modules; the crash tests fork again inside a worker.
    # A worker that dies (rather than a test that fails) breaks the pool and stops the run with
    # BrokenProcessPool instead of waiting forever for its unit.
    ctx = multiprocessing.get_context("fork")
    with cf.ProcessPoolExecutor(max(1, args.jobs), mp_context=ctx) as pool:
        for fut in cf.as_completed([pool.submit(run_unit, u) for u in work]):
            r = fut.result()
            if not r["shard"] or r["shard"].startswith("0/"):
                total += r["run"]          # a sharded test counts once
            skipped += r["skipped"]
            expected += r["expected"]
            unexpected += r["unexpected"]
            bad += [("FAIL", t, tb) for t, tb in r["failures"]]
            bad += [("ERROR", t, tb) for t, tb in r["errors"]]
            name = r["ids"][0] if len(r["ids"]) == 1 else r["ids"][0].rsplit(".", 1)[0]
            name += f" [{r['shard']}]" if r["shard"] else ""
            slowest.append((r["seconds"], name))
            mark = "F" if r["failures"] or r["errors"] else "."
            if args.verbose:
                print(f"{mark} {r['seconds']:6.1f}s {name}", flush=True)
            else:
                print(mark, end="", flush=True)
    wall = time.monotonic() - t0
    print()
    for kind, test, tb in bad:
        print("=" * 70 + f"\n{kind}: {test}\n" + "-" * 70 + f"\n{tb}")
    if args.verbose:
        print("slowest units:")
        for s, n in sorted(slowest, reverse=True)[:5]:
            print(f"  {s:6.1f}s {n}")
    print("-" * 70)
    print(f"Ran {total} tests in {wall:.1f}s ({len(work)} units, {args.jobs} workers)")
    extra = [f"skipped={skipped}"] * bool(skipped) + [f"expected failures={expected}"] * bool(expected)
    if bad or unexpected:
        print(f"FAILED (failures/errors={len(bad)}, unexpected successes={len(unexpected)})")
        return 1
    print("OK" + (f" ({', '.join(extra)})" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
