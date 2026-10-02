"""工程量測工具：僅建立暫存假資料，不讀取 config.json 或使用者目錄。

執行：python tests/benchmark_performance.py
此檔不列入 pytest；耗時為觀測值，不作為測試通過門檻。
"""

import json
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter
import tracemalloc
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend


def measure(call, repetitions=3):
    samples = []
    result = None
    for _ in range(repetitions):
        started = perf_counter()
        result = call()
        samples.append(perf_counter() - started)
    return {"median_seconds": round(median(samples), 6),
            "samples_seconds": [round(sample, 6) for sample in samples]}, result


def main():
    with TemporaryDirectory(prefix="finder-benchmark-") as temporary:
        base = Path(temporary)
        root = base / "root"
        root.mkdir()
        for number in range(20000):
            (root / f"report_2026_{number:05}.sql").touch()
        config = backend.Config((backend.Root("benchmark", root),))
        index = backend.DirectoryIndex(base / "cache.sqlite3")
        metrics = {"entries": 20000, "python": sys.version.split()[0]}
        metrics["cold_search"], report = measure(lambda: backend.scan_names(config, "report 2026", index=index), 1)
        metrics["cold_search"].update(total=report.total, read_directories=report.read_directories)
        calls = {"scandir": 0, "lstat": 0}
        original_scan, original_stat = backend.os.scandir, Path.lstat

        def counted_scan(path):
            calls["scandir"] += 1
            return original_scan(path)

        def counted_stat(path, *args, **kwargs):
            calls["lstat"] += 1
            return original_stat(path, *args, **kwargs)

        with patch.object(backend.os, "scandir", counted_scan), patch.object(Path, "lstat", counted_stat):
            metrics["warm_search"], report = measure(lambda: backend.scan_names(config, "report 2026", index=index), 5)
            metrics["warm_search"].update(calls=calls.copy(), total=report.total, cached_directories=report.cached_directories)
            calls.update(scandir=0, lstat=0)
            metrics["warm_browse"], _ = measure(lambda: backend.browse_folder(root, index=index))
            metrics["warm_browse"]["calls_total_3_runs"] = calls.copy()
            calls.update(scandir=0, lstat=0)
            metrics["metadata_refresh"], check = measure(lambda: index.check_changes(Event(), refresh_times=True), 1)
            metrics["metadata_refresh"].update(calls=calls.copy(), changed=check.changed)
        metrics["flush"], _ = measure(index.flush, 1)
        restored = backend.DirectoryIndex(index.file)
        metrics["restore"], count = measure(lambda: restored.load((root,), Event()), 1)
        metrics["restore"]["directories"] = count
        metrics["cache_bytes"] = index.file.stat().st_size
        tracemalloc.start()
        memory_index = backend.DirectoryIndex()
        backend.browse_folder(root, index=memory_index)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        metrics["browse_memory_bytes"] = {"current": current, "peak": peak}
        print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
