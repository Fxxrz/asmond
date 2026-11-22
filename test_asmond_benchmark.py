from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import test_asmond as fixtures
from unittest import mock


spec = importlib.util.spec_from_file_location("asmond_overhead_benchmark", Path(__file__).parent / "tools/benchmark-overhead.py")
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


class OverheadBenchmarkTests(fixtures.IsolatedTestCase):
    def sample(self, at=10.0, cpu=2.0):
        return benchmark.Usage(at, (123, b"uuid"), cpu, 4096, 2048)

    def test_cpu_percentage_is_per_logical_cpu_and_idle_zero_is_valid(self):
        first = self.sample()
        result = benchmark.summarize([first, self.sample(12, 5)])
        self.assertEqual(result["cpu_pct_one_logical_cpu"], 150)
        self.assertEqual(result["measured_s"], 2)
        self.assertEqual(benchmark.summarize([first, self.sample(12, 2)])["cpu_s"], 0)

    def test_reused_identity_reset_and_nonprogressing_window_are_rejected(self):
        first = self.sample()
        for following in (replace(first, at=12, identity=(456, b"uuid")),
                          self.sample(12, 1), self.sample(10, 3), self.sample(12, float("nan"))):
            with self.subTest(sample=following), self.assertRaises(ValueError):
                benchmark.summarize([first, following])
        with self.assertRaises(ValueError):
            benchmark.summarize([first])
        with self.assertRaises(ValueError):
            benchmark.summarize([first, self.sample(9, 2), self.sample(12, 3)])

    def test_memory_reports_sampled_values_without_summing_rss_and_footprint(self):
        result = benchmark.summarize([self.sample(), replace(self.sample(12, 3), rss_bytes=8192, footprint_bytes=3072)])
        self.assertEqual(result["rss_median_bytes"], 6144)
        self.assertEqual(result["rss_sampled_max_bytes"], 8192)
        self.assertEqual(result["footprint_sampled_max_bytes"], 3072)

    def test_nonfinite_and_nonpositive_arguments_are_rejected(self):
        for value in ("nan", "inf", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                benchmark.positive(value)

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS counters")
    @fixtures.native_process_counter_test
    def test_native_cpu_time_matches_python_process_clock(self):
        self.assertEqual(ctypes.sizeof(benchmark.RusageV0), 96)
        reader = benchmark.UsageReader()
        first = reader.read(os.getpid())
        before = time.process_time()
        while time.process_time() - before < .08:
            sum(i * i for i in range(200))
        expected = time.process_time() - before
        last = reader.read(os.getpid())
        self.assertAlmostEqual(last.cpu_s - first.cpu_s, expected, delta=.01 + expected * .15)
        self.assertEqual(first.identity, last.identity)
        self.assertGreater(last.rss_bytes, 0)
        self.assertGreater(last.footprint_bytes, 0)

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS and a real PTY")
    @fixtures.local_process_test
    def test_failed_observation_closes_and_reaps_only_the_launched_mock_child(self):
        args = argparse.Namespace(rows=48, columns=170, live=False, warmup=.1, seconds=.2)
        processes = []
        original = subprocess.Popen

        def launch(*values, **options):
            child = original(*values, **options)
            if str(benchmark.ROOT / "asmond.py") in values[0]:
                processes.append(child)
            return child

        reader = mock.Mock()
        reader.read.side_effect = OSError("simulated unavailable counter")
        with (mock.patch.object(benchmark.subprocess, "Popen", side_effect=launch),
              mock.patch.object(benchmark, "power_source", return_value="fixture AC"),
              mock.patch.object(benchmark.os, "getloadavg", return_value=(0.0, 0.0, 0.0))):
            with self.assertRaisesRegex(OSError, "unavailable counter"):
                benchmark.run_case(args, reader, sys.executable, {"version": "test"}, "full", .7, 1)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(processes[0].returncode, 0)


if __name__ == "__main__":
    unittest.main()
