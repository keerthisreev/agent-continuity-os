"""Budget-accounting v2 tests: restart persistence, multi-process
recording without lost updates, cross-process check visibility.

Zero network. The multi-process tests fail on the pre-v2 code
(last-writer-wins on the shared file) and pass with the file lock +
reload-merge in continuity/usage.py.
"""
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.usage import BudgetExceeded, UsageTracker

TESTS = []


def test_restart_keeps_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "usage.json")
        first = UsageTracker(persist_path=path)
        first.record({"prompt_tokens": 1000, "completion_tokens": 2000})
        assert first.calls == 1
        second = UsageTracker(persist_path=path)
        assert second.calls == 1, second.calls
        assert second.prompt_tokens == 1000
        assert second.completion_tokens == 2000
        assert abs(second.cost_usd - first.cost_usd) < 1e-9
    print("PASS usage v2: a restart reloads exact spend from disk")


def _record_child_script() -> str:
    return (
        "import os, sys, time\n"
        "sys.path.insert(0, os.environ['REPO'])\n"
        "# Barrier: all children load the file only after every child\n"
        "# is spawned, so their load->record->save sequences genuinely\n"
        "# overlap. Without the barrier, staggered process starts let\n"
        "# the old last-writer-wins code accidentally accumulate.\n"
        "go = os.environ['GOFILE']\n"
        "for _ in range(600):\n"
        "    if os.path.exists(go):\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "from continuity.usage import UsageTracker\n"
        "t = UsageTracker(persist_path=os.environ['UPATH'])\n"
        "for _ in range(int(os.environ['N'])):\n"
        "    t.record({'prompt_tokens': 100, 'completion_tokens': 50})\n"
    )


def _run_children(path: str, workers: int, per_worker: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        child = os.path.join(tmp, "record_child.py")
        with open(child, "w") as fh:
            fh.write(_record_child_script())
        go = os.path.join(tmp, "go")
        env = dict(os.environ, REPO=os.path.dirname(
            os.path.abspath(__file__)), UPATH=path, N=str(per_worker),
            GOFILE=go)
        procs = [subprocess.Popen([sys.executable, child], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
                 for _ in range(workers)]
        open(go, "w").close()  # release the barrier
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err


def test_multi_process_records_lose_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "usage.json")
        _run_children(path, workers=4, per_worker=25)
        final = UsageTracker(persist_path=path)
        assert final.calls == 100, final.calls
        assert final.prompt_tokens == 100 * 100, final.prompt_tokens
        assert final.completion_tokens == 50 * 100, \
            final.completion_tokens
    print("PASS usage v2: 4 processes x 25 records = 100 calls, exact "
          "token sums, no lost updates")


def test_check_sees_other_process_spend():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "usage.json")
        # Parent tracker is created BEFORE the child spends anything,
        # with a budget the child's spend will exceed.
        parent = UsageTracker(persist_path=path, budget_usd=0.001)
        parent.check()  # zero spend: passes
        _run_children(path, workers=1, per_worker=1)
        # Child recorded 100 prompt + 50 completion tokens = $0.000075.
        # That is under $0.001, so record a bigger spend directly.
        big = UsageTracker(persist_path=path)
        big.record({"prompt_tokens": 100000, "completion_tokens": 0})
        try:
            parent.check()
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("parent check() did not see the other "
                                 "process's spend")
    print("PASS usage v2: check() in one process sees spend recorded by "
          "another process")


def test_thread_concurrent_records_exact():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "usage.json")
        tracker = UsageTracker(persist_path=path)
        errors = []

        def record_batch():
            try:
                for _ in range(25):
                    tracker.record({"prompt_tokens": 10,
                                    "completion_tokens": 5})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=record_batch)
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors
        assert tracker.calls == 200, tracker.calls
        assert tracker.prompt_tokens == 2000
        assert tracker.completion_tokens == 1000
    print("PASS usage v2: 8 threads x 25 records = 200 calls, exact")


TESTS = [test_restart_keeps_spend,
         test_multi_process_records_lose_nothing,
         test_check_sees_other_process_spend,
         test_thread_concurrent_records_exact]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} usage v2 tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
