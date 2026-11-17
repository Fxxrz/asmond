import argparse
import contextlib
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import asmond
import asmond_models
import asmond_process
import asmond_record as recording
import plistlib


def arguments(root, *extra):
    with mock.patch.object(asmond, "load_settings", return_value={}):
        return asmond.build_parser().parse_args(["record", "--mock", "--groups", "memory,processes,cpu,io", "--output-dir", str(root), "--duration", "0.01", *extra])


def memory(now=1.0, size=16384, value=100):
    return asmond_models.MemoryStats(total_bytes=1024**3, active_bytes=4*size, wired_bytes=2*size,
        compressed_bytes=3*size, system_free_pct=80, vm_page_size_bytes=size, vm_sampled_at=now,
        vm_pages={"pages active": 4, "pages wired down": 2, "pages occupied by compressor": 3,
                  "pages stored in compressor": 9, **dict.fromkeys(recording.COUNTERS, value)},
        read_status=dict.fromkeys(("total", "vm_stat", "pressure", "swap"), "ok"))


def process(pid=1, cpu=10.0, rss=1024, start="Wed Oct 28 11:00:00 2026", name="worker"):
    return asmond_models.ProcessInfo(pid, 99, 1, rss, name, full_command="SECRET_ARGUMENTS", user="PRIVATE_USER", start_time=start, cpu_time_s=cpu)


class RecordingAcquisitionTests(unittest.TestCase):
    def test_battery_recording_keeps_measured_zero_without_device_enrichment(self):
        args = arguments("/tmp", "--groups", "battery")
        args.mock = False
        battery = {"ExternalConnected": True, "Temperature": 3000, "PowerTelemetryData": {"SystemPowerIn": 0}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(asmond, "HOST_MACHINE", "x86_64"))
            stack.enter_context(mock.patch.object(asmond, "read_battery_items", return_value=battery))
            stack.enter_context(mock.patch.object(asmond, "read_hpm_port_items", return_value=[]))
            readers = [stack.enter_context(mock.patch.object(asmond, name, side_effect=RuntimeError))
                       for name in ("read_usb_port_items", "read_usb_device_items", "cable_identity_items_for_hpm")]
            readers.append(stack.enter_context(mock.patch.object(asmond.USB_DEVICE_DETAILS, "snapshot", side_effect=RuntimeError)))
            row = recording.Capture(asmond, args, None).read()
        self.assertEqual(row["groups"]["battery"]["measured_input_w"], 0)
        self.assertEqual(row["sources"]["battery"]["status"], "ok")
        for reader in readers:
            reader.assert_not_called()

    def test_verified_intel_ram_power_survives_recording_and_csv_without_changing_total(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "power")
            fixture = Path(__file__).parent / "tests" / "fixtures" / "intel_macmini_sample.plist"
            sample = asmond.sample_from_plist(plistlib.loads(fixture.read_bytes()))
            feed = mock.Mock()
            feed.snapshot.return_value = (sample, {"status": "ok"})
            record = recording.Capture(asmond, args, feed).read()
            values = record["groups"]["power"]
            self.assertAlmostEqual(values["ram_power_mw"], 688.997)
            self.assertEqual(values["soc_power_mw"], sample.soc_power_mw)
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(record)
            writer.close()
            output = Path(root) / "csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 0)
            with (output / "metrics.csv").open() as handle:
                row = next(csv.DictReader(handle))
            self.assertAlmostEqual(float(row["power.ram_power_mw"]), 688.997)

    def test_verified_intel_and_separate_smart_temperatures_survive_csv_export(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "temperatures")
            sample = asmond.MetricSample(telemetry_source="intel", temperature_source="AppleSMC",
                cpu_temp_avg_c=75, gpu_die_temp_c=69, cpu_peci_temp_c=83.64,
                memory_proximity_temp_c=55, pch_die_temp_c=73, ssd_temp_c=49.85)
            feed = mock.Mock()
            feed.snapshot.return_value = (sample, {"status": "ok"})
            record = recording.Capture(asmond, args, feed).read()
            values = record["groups"]["temperatures"]
            self.assertEqual(values["source"], "AppleSMC")
            self.assertIn("NVMe SMART", values["ssd_source"])
            self.assertIsNone(values["gpu_temp_avg_c"])
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(record)
            writer.close()
            output = Path(root) / "csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 0)
            with (output / "metrics.csv").open() as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["temperatures.ssd_temp_c"], "49.85")
            self.assertEqual(float(row["temperatures.gpu_die_temp_c"]), 69)
            self.assertIn("NVMe SMART", row["temperatures.ssd_source"])

    def test_mock_process_intervals_accumulate_cpu_without_uptime_spikes_or_false_resets(self):
        args = argparse.Namespace(interval=5, mock=True, groups=["processes"])
        capture = recording.Capture(asmond, args, None)
        previous = {}
        times = (1000000, 1000005, 1000010, 1000015, 1000100, 1000105, 1000090, 1000095)
        with mock.patch.object(recording, "read_processes", side_effect=AssertionError("mock must not read real processes")):
            for index, now in enumerate(times):
                with mock.patch.object(recording.time, "monotonic", return_value=now), mock.patch.object(recording.time, "time", return_value=1700000000 + now):
                    result = capture.read()
                    expected = {process.pid: process.cpu_pct for process in asmond.mock_processes(now)}
                group = result["groups"]["processes"]
                if index in (0, 4, 6):
                    self.assertEqual(group["top_cpu"], [])
                    self.assertTrue(all(row["cpu_status"] == "baseline_or_reset" for row in group["top_memory"]))
                else:
                    self.assertEqual({row["pid"] for row in group["top_cpu"]}, set(expected))
                    for row in group["top_cpu"]:
                        self.assertEqual(row["cpu_status"], "ok")
                        self.assertAlmostEqual(row["cpu_pct"], expected[row["pid"]], places=6)
                        self.assertTrue(0 <= row["cpu_pct"] <= 1000)
                for row in group["top_memory"]:
                    self.assertGreaterEqual(row["cpu_time_s"], previous.get(row["pid"], 0))
                    previous[row["pid"]] = row["cpu_time_s"]

    def test_cpu_time_parser_preserves_centiseconds_and_long_running_totals(self):
        for raw, expected in (("0:00.00", 0), ("123:45.67", 7425.67), ("12:34:56.78", 45296.78), ("2-12:34:56.78", 218096.78)):
            self.assertAlmostEqual(asmond_process.parse_cpu_time(raw), expected)
        for raw in ("nan", "-1:00", "1:60.00", "1-10:00", "1:01.inf", "1:01.2secret"):
            self.assertIsNone(asmond_process.parse_cpu_time(raw))

    def test_record_process_query_is_one_snapshot_no_argv_fallback(self):
        line = "0:00.25 42 user 1 Wed Oct 28 11:00:00 2026 01:00 99.0 1.0 2048 /Applications/Worker\n"
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, line.encode(), b"")
        rows = asmond_process.read_processes(include_cpu_time=True, strict=True, run=run)
        self.assertEqual(rows[0].cpu_time_s, 0.25)
        self.assertEqual(rows[0].command, "Worker")
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0][-1].startswith("time=,"))
        self.assertTrue(commands[0][-1].endswith(",comm="))
        with self.assertRaises(OSError):
            asmond_process.read_processes(include_cpu_time=True, strict=True, run=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, b"", b"SECRET"))

    def test_memory_read_records_real_zero_counters_and_separate_compressor_sizes(self):
        from asmond_metrics_memory import read_memory_stats
        responses = {"hw.memsize": b"1073741824", "vm.swapusage": b"total = 0.00M used = 0.00M free = 0.00M",
            "/usr/bin/vm_stat": b"Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 4.\nPages wired down: 2.\nPages occupied by compressor: 3.\nPages stored in compressor: 9.\nSwapins: 0.\nSwapouts: 0.\nCompressions: 12.\nDecompressions: 2.\n",
            "/usr/bin/memory_pressure": b"System-wide memory free percentage: 80%"}
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, responses[cmd[-1]], b"")
        value = read_memory_stats(run)
        self.assertEqual(set(value.read_status.values()), {"ok"})
        raw = recording.memory_values(value, recording.CounterRates(10))
        self.assertEqual(raw["compressed_bytes"], 3*16384)
        self.assertEqual(raw["uncompressed_bytes_in_compressor"], 9*16384)
        self.assertEqual(raw["activity"]["swapins"]["pages_total"], 0)
        self.assertIsNone(raw["activity"]["swapins"]["pages_delta"])
        self.assertEqual(raw["swap_used_bytes"], 0)
        self.assertEqual(raw["pressure_pct"], 20)

    def test_unread_memory_fields_are_null_not_legacy_model_zeros_or_guessed_pages(self):
        from asmond_metrics_memory import read_memory_stats
        value = read_memory_stats(lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, b"", b""))
        result = recording.memory_values(value, recording.CounterRates(10))
        for key in ("total_bytes", "used_bytes", "compressed_bytes", "swap_used_bytes", "pressure_pct"):
            self.assertIsNone(result[key])
        self.assertEqual(result["activity"]["swapins"]["status"], "unavailable")

    def test_swap_rates_use_reported_pages_and_actual_interval_not_pageins(self):
        rates = recording.CounterRates(2)
        self.assertIsNone(rates.read(memory())["swapins"]["pages_delta"])
        current = memory(now=3, value=105)
        current.vm_pages["pageins"] = 1000000
        result = rates.read(current)
        self.assertEqual(result["swapins"]["pages_delta"], 5)
        self.assertEqual(result["swapins"]["pages_per_s"], 2.5)
        self.assertEqual(result["swapins"]["mib_equivalent_per_s"], 2.5 * 16384 / 1024**2)
        self.assertEqual(rates.read(memory(now=4, value=105))["swapouts"]["pages_delta"], 0)

    def test_counter_missing_reset_page_size_change_and_gaps_never_fabricate_activity(self):
        rates = recording.CounterRates(2)
        rates.read(memory())
        self.assertEqual(rates.read(memory(now=2, value=1))["swapins"]["status"], "reset")
        missing = memory(now=3, value=3)
        missing.vm_pages.pop("swapins")
        self.assertEqual(rates.read(missing)["swapins"]["status"], "unavailable")
        self.assertEqual(rates.read(memory(now=4, value=4))["swapins"]["status"], "baseline")
        for item in (memory(now=5, size=4096, value=5), memory(now=100, value=105), memory(now=99, value=106)):
            value = rates.read(item)
            self.assertIsNone(value["swapins"]["pages_delta"])
        broken = memory(now=101)
        broken.read_status["vm_stat"] = "unavailable"
        rates.read(broken)
        self.assertEqual(rates.read(memory(now=102))["swapins"]["status"], "baseline")

    def test_two_rankings_cover_cpu_and_memory_independently_and_keep_over_100_percent(self):
        ranks = recording.ProcessRanks(2)
        rows = [process(pid=i, cpu=0, rss=i) for i in range(1, 25)]
        first = ranks.read(rows, 1)
        self.assertEqual(first["top_cpu"], [])
        self.assertEqual(first["top_memory"][0]["pid"], 24)
        for row in rows:
            row.cpu_time_s = (25 - row.pid) * 0.5
        second = ranks.read(rows, 3)
        self.assertEqual(len(second["top_cpu"]), 10)
        self.assertEqual(len(second["top_memory"]), 10)
        self.assertEqual(second["top_cpu"][0]["pid"], 1)
        self.assertEqual(second["top_cpu"][0]["cpu_pct"], 600)
        self.assertEqual(second["top_memory"][0]["pid"], 24)
        text = json.dumps(second)
        self.assertNotIn("SECRET_ARGUMENTS", text)
        self.assertNotIn("PRIVATE_USER", text)

    def test_pid_reuse_exit_counter_reset_gap_and_snapshot_failure_reset_cpu_baselines(self):
        ranks = recording.ProcessRanks(1)
        ranks.read([process()], 1)
        self.assertEqual(ranks.read([process(cpu=11)], 2)["top_cpu"][0]["cpu_pct"], 100)
        self.assertEqual(ranks.read([process(cpu=20, start="NEW")], 3)["top_cpu"], [])
        self.assertEqual(ranks.read([], 4)["process_count"], 0)
        self.assertEqual(ranks.read([process(cpu=21, start="NEW")], 5)["top_cpu"], [])
        self.assertEqual(ranks.read([process(cpu=1, start="NEW")], 6)["top_cpu"], [])
        self.assertEqual(ranks.read([process(cpu=2, start="NEW")], 100)["top_cpu"], [])
        ranks.reset()
        self.assertEqual(ranks.read([process(cpu=3, start="NEW")], 101)["top_cpu"], [])


class RecordingFileTests(unittest.TestCase):
    def test_summary_cli_reads_old_schema_across_days_with_gaps_and_swap_peaks(self):
        start = datetime(2026, 10, 28, 23, 59, 50, tzinfo=timezone.utc)

        def at(seconds, kind, **fields):
            record = recording.entry(kind, **fields)
            record["timestamp"] = (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
            record["monotonic_s"] = 1000 + seconds
            return record

        def sample(seconds, swap_bytes, swap_out, status, name):
            groups = {
                "cpu": {"host_usage_pct": 0},
                "memory": {"swap_used_bytes": swap_bytes, "activity": {
                    "swapins": {"status": "ok", "mib_equivalent_per_s": 0},
                    "swapouts": {"status": status, "mib_equivalent_per_s": swap_out}}},
                "processes": {"top_cpu": [{"pid": 17, "name": name, "cpu_pct": 135}],
                              "top_memory": [{"pid": 23, "name": "rss-worker", "rss_bytes": 1024**2}]},
                "temperatures": {"temp_max_c": None, "fan_rpms": {"F0Ac": 0}},
            }
            return at(seconds, "sample", groups=groups, sources={key: {"status": "ok"} for key in groups})

        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(at(0, "session_start", version="0.8.0-dev", interval_s=5,
                            groups=["cpu", "memory", "processes", "temperatures"], max_jsonl_bytes=1024**2))
            writer.write(sample(5, 1024**3, 999, "baseline", "baseline-worker"))
            writer.write(sample(25, 2 * 1024**3, 42, "ok", "swap-peak-worker"))
            writer.write(sample(30, 3 * 1024**3, 0, "ok", "later-worker"))
            writer.write(at(31, "session_end", reason="size_limit", samples=3))
            writer.close()
            before = {path.name: path.read_bytes() for path in writer.directory.iterdir()}
            self.assertEqual(len(before), 2)
            with mock.patch.object(asmond, "load_settings", return_value={}), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(asmond.main(["summary", str(writer.directory)]), 0)
            result = output.getvalue()
            self.assertIn("Elapsed (monotonic): 0:00:31", result)
            self.assertIn("Stop reason: size_limit", result)
            self.assertIn("Samples: 3 (target interval 5 s)", result)
            self.assertIn("Sampling gaps / clock changes: 1", result)
            self.assertIn("Longest sample spacing: 20.000 s at 2026-10-29T00:00:15Z", result)
            self.assertIn("Samples with numeric temperatures: 0/3", result)
            self.assertIn("Peak host CPU: 0.00 %", result)
            self.assertIn("Peak swap occupied: 3.00 GiB", result)
            self.assertIn("Peak swap-out: 42.00 MiB-equivalent/s at 2026-10-29T00:00:15Z", result)
            self.assertIn("Same sample CPU leader: swap-peak-worker (PID 17), 135.00 %", result)
            self.assertIn("Same sample RSS leader: rss-worker (PID 23), 1.00 MiB", result)
            self.assertNotIn("later-worker", result)
            self.assertEqual({path.name: path.read_bytes() for path in writer.directory.iterdir()}, before)

    def test_summary_distinguishes_missing_end_from_truncated_input(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(recording.entry("session_start", version="0.8.0-dev", interval_s=5,
                                         groups=["power"], max_jsonl_bytes=1024**2))
            writer.write(recording.entry("sample", groups={"power": None}, sources={"power": {"status": "starting"}}))
            writer.close()
            args = argparse.Namespace(session=writer.directory)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(recording.run_summary(args, asmond), 0)
            self.assertIn("Stop reason: unknown (no end marker)", output.getvalue())
            self.assertIn("Source samples with status other than ok: power 1/1", output.getvalue())
            with next(writer.directory.iterdir()).open("ab") as handle:
                handle.write(b'{"incomplete"')
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(recording.run_summary(args, asmond), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("incomplete JSONL row", errors.getvalue())

    def test_summary_rejects_duplicate_measurement_groups_before_printing(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(recording.entry("session_start", version="0.8.0-dev", interval_s=5,
                                         groups=["power", "power"], max_jsonl_bytes=1024**2))
            writer.write(recording.entry("sample", groups={"power": None}, sources={"power": {"status": "starting"}}))
            writer.close()
            args = argparse.Namespace(session=writer.directory)
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(recording.run_summary(args, asmond), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("duplicate measurement group in recording header", errors.getvalue())

    def test_export_rejects_column_collisions_and_duplicate_json_keys(self):
        parts = (("activity", {"swapins": {"mib_equivalent_per_s": 4}}),
                 ("activity.swapins.mib_equivalent_per_s", 999))
        for case in ("nested first", "dotted first", "duplicate"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                writer = recording.DailyWriter(Path(root), 1024**2)
                memory_fields = dict(reversed(parts) if case == "dotted first" else parts)
                if case == "duplicate":
                    memory_fields = {"used_bytes": 4}
                writer.write(recording.entry("sample", groups={"memory": memory_fields}))
                writer.close()
                if case == "duplicate":
                    path = next(writer.directory.glob("*.jsonl"))
                    path.write_bytes(path.read_bytes().replace(b'"used_bytes":4', b'"used_bytes":4,"used_bytes":999'))
                output = Path(root) / "csv"
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    args = argparse.Namespace(session=writer.directory, output_dir=output)
                    self.assertEqual(recording.run_export(args, asmond), 1)
                self.assertIn("the recording is incomplete, malformed or uses an unsupported schema", errors.getvalue())
                self.assertNotIn("Incomplete CSV files", errors.getvalue())
                self.assertFalse(output.exists())
                rows = recording.read_records(writer.directory)
                if case == "duplicate":
                    with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                        next(rows)
                else:
                    with contextlib.closing(rows), self.assertRaisesRegex(ValueError, "colliding CSV"):
                        recording.sample_csv_row(next(rows))

    def test_export_rejects_scalars_or_null_for_required_measurement_and_source_objects(self):
        cases = [
            ({"memory": {"activity": 123}}, {}),
            ({"memory": {"activity": None}}, {}),
            ({"memory": {"read_status": 123}}, {}),
            ({"cpu": {"cores": {"P0": 45}}}, {}),
            ({"cpu": {"cores": {"P0": None}}}, {}),
            ({"temperatures": {"fan_rpms": 0}}, {}),
            ({"memory": {"activity": {"swapins": 123}}}, {}),
            ({"memory": {"used_bytes": 123}}, {"memory": "ok"}),
            ({"memory": {"used_bytes": 123}}, {"memory": None}),
            ({}, {"temperatures": {"status": "ok", "powermetrics": 123}}),
            ({}, {"power": {"status": 123}}),
        ]
        for groups, sources in cases:
            with self.subTest(groups=groups, sources=sources), tempfile.TemporaryDirectory() as root:
                writer = recording.DailyWriter(Path(root), 1024**2)
                writer.write(recording.entry("sample", groups=groups, sources=sources))
                writer.close()
                with self.assertRaises(ValueError):
                    list(recording.read_records(writer.directory))
                output = Path(root) / "csv"
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 1)
                self.assertFalse(output.exists())

    def test_export_preserves_valid_nested_objects_missing_numbers_zero_and_negative_power(self):
        groups = {"cpu": {"cores": {"P0": {"usage_pct": None, "freq_mhz": 1000}}},
                  "temperatures": {"fan_rpms": {"F0Ac": 0}},
                  "memory": {"read_status": {"vm_stat": "ok"}, "activity": {"swapins": {"pages_total": 0, "pages_delta": None, "status": "baseline"}}},
                  "battery": {"power_mw": -100}}
        sources = {"temperatures": {"status": "ok", "powermetrics": {"status": "not_requested"}}}
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(recording.entry("sample", groups=groups, sources=sources))
            writer.close()
            output = Path(root) / "csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 0)
            with (output / "metrics.csv").open() as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(result["cpu.cores.P0.usage_pct"], "")
            self.assertEqual(result["temperatures.fan_rpms.F0Ac"], "0")
            self.assertEqual(result["memory.activity.swapins.pages_total"], "0")
            self.assertEqual(result["battery.power_mw"], "-100")

    def test_export_rejects_bad_nested_types_nonfinite_numbers_and_process_entries(self):
        cases = [
            {"memory": {"used_bytes": ["invalid"]}},
            {"memory": {"used_bytes": {"not": "a number"}}},
            {"memory": {"used_bytes": "123"}},
            {"memory": {"used_bytes": float("inf")}},
            {"memory": {"used_bytes": 10**400}},
            {"processes": {"top_memory": ["not an object"]}},
            {"processes": {"top_memory": [{"pid": True, "name": "worker"}]}},
            {"processes": {"top_memory": [{"pid": 1, "name": "worker"}] * 11}},
        ]
        for groups in cases:
            with self.subTest(groups=repr(groups)[:120]), tempfile.TemporaryDirectory() as root:
                writer = recording.DailyWriter(Path(root), 1024**2)
                path = writer.directory / "records-2026-10-28-000001.jsonl"
                raw = json.dumps(recording.entry("sample", groups=groups)) + "\n"
                # 1e999 is valid JSON syntax, but cannot become finite float.
                path.write_text(raw.replace("Infinity", "1e999"))
                writer.close()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=Path(root) / "csv"), asmond), 1)
                self.assertFalse((Path(root) / "csv").exists())

    def test_all_process_csv_text_is_escaped_even_if_timestamp_validation_is_bypassed(self):
        for text in ("=1+1", "+1+1", "-1+1", "@SUM(1)", "  =1+1"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as root:
                writer = recording.DailyWriter(Path(root), 1024**2)
                record = recording.entry("sample", groups={"battery": {"power_mw": -100}, "processes": {"top_memory": [{"pid": 123, "name": text, "rss_bytes": 0}]}})
                record["timestamp"] = text
                # Bypass only writer day naming, to model an edited input.
                (writer.directory / "records-2026-10-28-000001.jsonl").write_text(json.dumps(record) + "\n")
                writer.close()
                args = argparse.Namespace(session=writer.directory, output_dir=Path(root) / "rejected")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(recording.run_export(args, asmond), 1)
                args.output_dir = Path(root) / "escaped"
                with mock.patch.object(recording, "validate_timestamp"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(recording.run_export(args, asmond), 0)
                with (args.output_dir / "processes.csv").open() as handle:
                    row = next(csv.DictReader(handle))
                self.assertEqual(row["timestamp"], "'" + text)
                self.assertEqual(row["name"], "'" + text)
                with (args.output_dir / "metrics.csv").open() as handle:
                    self.assertEqual(next(csv.DictReader(handle))["battery.power_mw"], "-100")

    def test_export_detects_changed_input_between_streaming_passes(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(recording.entry("sample", groups={"memory": {"used_bytes": 100}}))
            writer.close()
            path = next(writer.directory.glob("*.jsonl"))
            original_open = recording.private_csv
            def change(path_out):
                if path_out.name == "metrics.csv":
                    path.write_bytes(path.read_bytes().replace(b'"used_bytes":100', b'"used_bytes":200'))
                return original_open(path_out)
            output = Path(root) / "csv"
            args = argparse.Namespace(session=writer.directory, output_dir=output)
            with mock.patch.object(recording, "private_csv", side_effect=change), contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()) as messages:
                self.assertEqual(recording.run_export(args, asmond), 1)
            self.assertEqual(messages.getvalue(), "")
            self.assertIn("recording changed during export", errors.getvalue())
            self.assertIn(f"Incomplete CSV files may remain in {output}", errors.getvalue())
            self.assertIn("Choose a new --output-dir", errors.getvalue())
            self.assertIn(b'"used_bytes":200', path.read_bytes())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            previous = (output / "metrics.csv").read_bytes()
            with contextlib.redirect_stderr(io.StringIO()) as retry:
                self.assertEqual(recording.run_export(args, asmond), 1)
            self.assertIn(f"output path already exists: {output}", retry.getvalue())
            self.assertNotIn("Incomplete CSV files", retry.getvalue())
            self.assertEqual((output / "metrics.csv").read_bytes(), previous)

    def test_interrupted_export_names_its_partial_output(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(recording.entry("sample", groups={"memory": {"used_bytes": 100}}))
            writer.close()
            output = Path(root) / "csv"
            open_csv = recording.private_csv

            def interrupt(path):
                if path.name == "processes.csv":
                    raise KeyboardInterrupt
                return open_csv(path)

            with mock.patch.object(recording, "private_csv", side_effect=interrupt), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 130)
            self.assertIn("CSV export interrupted.", errors.getvalue())
            self.assertIn(f"Incomplete CSV files may remain in {output}", errors.getvalue())
            self.assertEqual({file.name for file in output.iterdir()}, {"metrics.csv"})

    def test_private_exclusive_daily_rotation_clock_rollback_and_reserved_final_record(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            for day in ("2026-10-28", "2026-10-29", "2026-10-28"):
                writer.write(dict(schema=1, type="sample", timestamp=day + "T12:00:00Z", monotonic_s=1, groups={}))
            self.assertEqual(len(list(writer.directory.glob("*.jsonl"))), 3)
            self.assertEqual(stat.S_IMODE(writer.directory.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in writer.directory.iterdir()))
            writer.total = writer.limit - 8192
            with self.assertRaises(recording.SizeLimit):
                writer.write(recording.entry("sample", value="large"))
            writer.write(recording.entry("session_end", reason="size_limit"), final=True)
            writer.close()
            self.assertEqual(list(recording.read_records(writer.directory))[-1]["type"], "session_end")
            self.assertEqual([row["timestamp"][:10] for row in recording.read_records(writer.directory)][:3], ["2026-10-28", "2026-10-29", "2026-10-28"])

    def test_symlink_and_writable_parent_are_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "actual"
            target.mkdir()
            link = Path(root) / "link"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                recording.DailyWriter(link, 1024**2)
            self.assertEqual(list(target.iterdir()), [])
            target.chmod(0o777)
            with self.assertRaises(ValueError):
                recording.DailyWriter(target, 1024**2)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin ACLs")
    def test_record_and_export_reject_inherited_acl_grants_before_writing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            shared = root / "shared"
            shared.mkdir(mode=0o700)
            grant = "everyone allow read,search,file_inherit,directory_inherit"
            subprocess.run(["/bin/chmod", "+a", grant, str(shared)], check=True)
            before = subprocess.check_output(["/bin/ls", "-led", str(shared)], text=True)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(recording.run_record(arguments(shared), asmond), 1)
            self.assertIn("macOS ACL grants access", errors.getvalue())
            self.assertEqual(list(shared.rglob("*.jsonl")), [])

            writer = recording.DailyWriter(root, 1024**2)
            writer.write(recording.entry("sample", groups={"cpu": {"host_usage_pct": 1}}, sources={"cpu": {"status": "ok"}}))
            writer.close()
            output = shared / "csv"
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 1)
            self.assertIn("macOS ACL grants access", errors.getvalue())
            self.assertEqual(list(output.iterdir()), [])
            after = subprocess.check_output(["/bin/ls", "-led", str(shared)], text=True)
            self.assertEqual(before.splitlines()[1:], after.splitlines()[1:])

            deny_only = root / "deny-only"
            deny_only.mkdir(mode=0o700)
            deny = "user:nobody deny write,file_inherit,directory_inherit"
            subprocess.run(["/bin/chmod", "+a", deny, str(deny_only)], check=True)
            writer = recording.DailyWriter(deny_only, 1024**2)
            writer.write(recording.entry("sample", groups={}))
            writer.close()
            self.assertEqual(len(list(writer.directory.glob("*.jsonl"))), 1)

    def test_partial_malformed_schema_and_linked_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = root / "records-2026-10-28-000001.jsonl"
            for raw in (b'{"schema":1,"type":"sample"}', b'not json\n', b'{"schema":999,"type":"sample"}\n'):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    list(recording.read_records(root))
            path.unlink()
            path.symlink_to(root / "private")
            with self.assertRaises(OSError):
                list(recording.read_records(root))

    def test_battery_event_threshold_matches_existing_dashboard_boundaries(self):
        for limit_w in (0, 15):
            for power_mw in (1000, 0, -0.5, -1000, -14999, -15000, -15001, None):
                with self.subTest(limit_w=limit_w, power_mw=power_mw):
                    args = argparse.Namespace(alert_battery_drain_w=limit_w)
                    alerts = asmond.build_alerts(asmond.MetricSample(), asmond.MemoryStats(), asmond.BatteryStats(power_mw=power_mw), args)
                    events = recording.Events(asmond.alert_thresholds(args))
                    sample = recording.entry("sample", groups={"battery": {"power_mw": power_mw}}, sources={})
                    rows = events.read(sample)
                    battery = [row for row in rows if row["event"] == "battery_drain_high"]
                    self.assertEqual(len(battery), 1)
                    expected = "unavailable" if power_mw is None else "active" if any(alert.startswith("BAT ") for alert in alerts) else "clear"
                    self.assertEqual(battery[0]["state"], expected)
                    self.assertEqual(events.read(sample), [])
        sample = recording.entry("sample", groups={"temperatures": {"temp_max_c": 80}, "memory": {"swap_used_bytes": 100}, "battery": {"power_mw": 0}}, sources={})
        events = recording.Events((80, 0, 0)).read(sample)
        self.assertEqual(next(row["state"] for row in events if row["event"] == "temperature_high"), "active")
        self.assertFalse(any(row["event"] == "swap_used_high" for row in events))

    def test_zero_battery_threshold_logs_edges_and_distinguishes_missing_from_clear(self):
        events = recording.Events((80, 0, 0))
        for power, expected in ((-1000, "active"), (-1000, None), (0, "clear"), (None, "unavailable"), (None, None), (-500, "active")):
            sample = recording.entry("sample", groups={"battery": {"power_mw": power}}, sources={})
            rows = [row for row in events.read(sample) if row["event"] == "battery_drain_high"]
            self.assertEqual([row["state"] for row in rows], [] if expected is None else [expected])

    def test_event_edges_not_repeated_and_missing_does_not_mean_recovered(self):
        events = recording.Events((80, 1024, 1000))
        sample = recording.entry("sample", groups={"temperatures": {"temp_max_c": 85, "throttled": True}, "memory": {"swap_used_bytes": 2048}}, sources={"memory": {"status": "ok"}})
        first = events.read(sample)
        self.assertEqual({e["event"] for e in first}, {"source.memory", "temperature_high", "swap_used_high", "throttled"})
        self.assertEqual(events.read(sample), [])
        sample["groups"]["temperatures"]["temp_max_c"] = None
        changed = events.read(sample)
        self.assertEqual(changed[0]["state"], "unavailable")
        sample["groups"]["temperatures"].update(temp_max_c=60, throttled=False)
        changed = events.read(sample)
        self.assertEqual({row["state"] for row in changed}, {"clear"})

    def test_csv_export_numeric_missing_process_privacy_formulas_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            row = process(name="=FORMULA()")
            ranks = recording.ProcessRanks(2).read([row], 1)
            writer.write(recording.entry("sample", groups={"battery": {"power_mw": -1200, "charge_pct": None}, "processes": ranks}, sources={"battery": {"status": "ok", "received_age_s": 0.5}}))
            writer.write(recording.entry("event", event="test", state="clear"))
            writer.close()
            output = Path(root) / "csv"
            args = argparse.Namespace(session=writer.directory, output_dir=output)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(recording.run_export(args, asmond), 0)
                self.assertEqual(recording.run_export(args, asmond), 1)
            with (output / "metrics.csv").open() as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(result["battery.power_mw"], "-1200")
            self.assertEqual(result["battery.charge_pct"], "")
            self.assertEqual(result["sources.battery.received_age_s"], "0.5")
            text = (output / "processes.csv").read_text()
            self.assertIn("'=FORMULA()", text)
            self.assertNotIn("SECRET_ARGUMENTS", text)
            self.assertNotIn("PRIVATE_USER", text)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir()))


class RecordingIntegrationTests(unittest.TestCase):
    def test_power_group_does_not_start_unselected_auxiliary_temperature_readers(self):
        payload = plistlib.dumps({"processor": {"cpu_power": 1000}}) + b"\0"
        for enabled in (False, True):
            with self.subTest(temperatures=enabled):
                stream = asmond.PowerMetricsStream(1000, include_temperatures=enabled)
                proc = types.SimpleNamespace(stdout=io.BytesIO(payload), stderr=io.BytesIO(), poll=lambda: 0)
                with mock.patch.object(asmond, "refresh_sudo_credentials", return_value=(True, "")), mock.patch.object(asmond.subprocess, "Popen", return_value=proc), mock.patch.object(stream.temperature_cache, "refresh_async") as refresh, mock.patch.object(asmond, "apply_hid_temperatures") as temps, mock.patch.object(asmond, "apply_indexed_fans") as fans:
                    list(stream.samples())
                self.assertEqual(refresh.call_count, int(enabled))
                self.assertEqual(temps.call_count + fans.call_count, int(enabled))
        backend = types.SimpleNamespace(PowerMetricsStream=mock.Mock(side_effect=OSError))
        feed = recording.StreamFeed(backend, 5, False, include_temperatures=False)
        with mock.patch.object(feed.retry_wake, "wait", side_effect=lambda _: feed.stop_event.set()):
            feed._run()
        backend.PowerMetricsStream.assert_called_once_with(5000, include_temperatures=False)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording, "StreamFeed") as factory, mock.patch.object(recording.Capture, "read", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", "power")
            recording.run_record(args, asmond)
            self.assertFalse(factory.call_args.kwargs["include_temperatures"])

    def test_missing_os_io_counter_invalidates_baseline_without_losing_genuine_zero(self):
        from asmond_metrics_memory import read_disk_counters, read_network_bytes
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "io", "--interval", "1")
            args.mock = False
            capture = recording.Capture(asmond, args, None)
            now = [0]
            values = iter((10000, None, 10100, 10150))
            def snapshot():
                now[0] += 1
                value = next(values)
                stats = {"Bytes (Read)": 0}
                if value is not None:
                    stats["Bytes (Write)"] = value
                payload = [{"IOObjectClass": "IOBlockStorageDriver", "Statistics": stats}]
                def run(command, **kwargs):
                    return subprocess.CompletedProcess(command, 0, plistlib.dumps(payload), b"")
                read, written = read_disk_counters(run)
                return asmond.IoSnapshot(timestamp=now[0], disk_read_bytes=read, disk_write_bytes=written, net_in_bytes=0, net_out_bytes=0)
            with mock.patch.object(asmond, "read_io_snapshot", side_effect=snapshot):
                records = [capture.read() for _ in range(4)]
            self.assertEqual(records[1]["sources"]["io"]["status"], "unavailable")
            self.assertIsNone(records[2]["groups"]["io"]["disk_write_bps"])
            self.assertEqual(records[3]["groups"]["io"]["disk_write_bps"], 50)
            self.assertEqual(records[3]["groups"]["io"]["disk_read_bps"], 0)
        header = "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\n"
        for raw, expected in (("0", (0, 0)), ("-", None), ("-1", None)):
            data = (header + f"en0 1500 <Link#4> 00:00:00:00:00:00 1 0 0 2 0 {raw} 0\n").encode()
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, data, b"")
            if expected is None:
                with self.assertRaises(RuntimeError):
                    read_network_bytes(run)
            else:
                self.assertEqual(read_network_bytes(run), expected)
        truncated = (header + "en0 1500 <Link#4> 00:00:00:00:00:00 1 0 1000\n").encode()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            read_network_bytes(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, truncated, b""))
    def test_record_invalid_size_root_and_nonmac_reject_before_creating_files(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stderr(io.StringIO()):
            args = arguments(root)
            args.max_size_mib = 0
            self.assertEqual(recording.run_record(args, asmond), 2)
            args.max_size_mib, args.mock = 1, False
            with mock.patch.object(asmond.platform, "system", return_value="Linux"):
                self.assertEqual(recording.run_record(args, asmond), 2)
            with mock.patch.object(asmond.platform, "system", return_value="Darwin"), mock.patch.object(recording.os, "geteuid", return_value=0):
                self.assertEqual(recording.run_record(args, asmond), 2)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_sudo_requested_only_for_power_and_denial_is_recorded_without_retry_prompt(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(asmond.platform, "system", return_value="Darwin"), mock.patch.object(asmond, "ensure_powermetrics_access", side_effect=SystemExit(1)) as access, mock.patch.object(asmond, "SudoKeeper") as keeper, contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", "power")
            args.mock = False
            self.assertEqual(recording.run_record(args, asmond), 1)
            access.assert_called_once()
            keeper.assert_not_called()
            rows = list(recording.read_records(next(Path(root).iterdir())))
            self.assertEqual(rows[-1]["reason"], "authorization_failed")

    def test_mock_all_groups_and_feed_stop_are_generated_and_bounded(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(asmond, "ensure_powermetrics_access", side_effect=AssertionError("sudo")), mock.patch.object(asmond, "apply_hid_temperatures", side_effect=AssertionError("real SMC")), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", ",".join(recording.GROUPS))
            self.assertEqual(recording.run_record(args, asmond), 0)
            rows = list(recording.read_records(next(Path(root).iterdir())))
            sample = next(row for row in rows if row["type"] == "sample")
            self.assertEqual(set(sample["groups"]), set(recording.GROUPS))
            self.assertEqual(rows[-1]["reason"], "completed")

    def test_power_authorization_keeper_and_owned_feed_are_stopped_on_interrupt(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(asmond.platform, "system", return_value="Darwin"), mock.patch.object(asmond, "ensure_powermetrics_access") as access, mock.patch.object(asmond, "SudoKeeper") as keeper, mock.patch.object(recording, "StreamFeed") as feed, mock.patch.object(recording.Capture, "read", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", "power")
            args.mock = False
            self.assertEqual(recording.run_record(args, asmond), 130)
            access.assert_called_once()
            keeper.return_value.start.assert_called_once()
            keeper.return_value.stop.assert_called_once()
            feed.return_value.start.assert_called_once()
            feed.return_value.stop.assert_called_once()

    def test_stream_factory_failure_and_bad_samples_are_retried_without_stale_telemetry(self):
        backend = types.SimpleNamespace(PowerMetricsStream=mock.Mock(side_effect=OSError), powermetrics_sample_is_usable=asmond.powermetrics_sample_is_usable)
        feed = recording.StreamFeed(backend, 1, False)
        def wait(delay):
            feed.stop_event.set()
        with mock.patch.object(feed.retry_wake, "wait", side_effect=wait):
            feed._run()
        self.assertEqual(feed.status, "unavailable")
        self.assertIsNone(feed.latest)
        backend.PowerMetricsStream.assert_called_once()
        stream = types.SimpleNamespace(samples=lambda: iter([asmond.MetricSample(cpu_power_mw=0), asmond.MetricSample(warning="invalid")]), stop=mock.Mock())
        backend.PowerMetricsStream = lambda _: stream
        feed = recording.StreamFeed(backend, 1, False)
        with mock.patch.object(feed.retry_wake, "wait", side_effect=lambda delay: feed.stop_event.set()):
            feed._run()
        self.assertIsNone(feed.latest)
        stream.stop.assert_called_once()
        observed = []
        def samples():
            yield asmond.MetricSample(cpu_power_mw=0, warning="partial sample")
            observed.append(feed.snapshot()[1]["status"])
        stream.samples = samples
        feed = recording.StreamFeed(backend, 1, False)
        with mock.patch.object(feed.retry_wake, "wait", side_effect=lambda delay: feed.stop_event.set()):
            feed._run()
        self.assertEqual(observed, ["partial"])

    def test_missing_native_temperatures_and_partial_memory_remain_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "memory,temperatures")
            args.mock = False
            capture = recording.Capture(asmond, args, None)
            partial = memory()
            partial.read_status["swap"] = "unavailable"
            with mock.patch.object(asmond, "read_memory_stats", return_value=partial), mock.patch.object(asmond, "apply_hid_temperatures"):
                record = capture.read()
            self.assertEqual(record["sources"]["temperatures"]["status"], "unavailable")
            self.assertEqual(record["sources"]["memory"]["status"], "partial")
            self.assertEqual(record["sources"]["memory.swap"]["status"], "unavailable")
            self.assertIsNone(record["groups"]["memory"]["swap_used_bytes"])

    def test_direct_temperature_fallback_and_unknown_battery_do_not_create_guessed_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "temperatures,battery")
            args.mock = False
            def apply(sample):
                sample.fan_rpms = {"F0Ac": 0}
                sample.trackpad_temp_c = 26
            capture = recording.Capture(asmond, args, None)
            with mock.patch.object(asmond, "apply_hid_temperatures", side_effect=apply), mock.patch.object(asmond, "read_charge_stats", return_value=(asmond.BatteryStats(), asmond.UsbCStats())):
                result = capture.read()
            values = result["groups"]["temperatures"]
            self.assertEqual(values["fan_rpms"], {"F0Ac": 0})
            self.assertEqual(values["trackpad_temp_c"], 26)
            self.assertIsNone(values["cpu_temp_avg_c"])
            self.assertEqual(result["sources"]["battery"]["status"], "no_data")

    def test_export_cli_and_conflicting_settings_actions_do_not_mutate_settings(self):
        with mock.patch.object(asmond, "load_settings", return_value={}), mock.patch.object(recording, "run_export", return_value=0) as export, mock.patch.object(asmond, "remove_settings", side_effect=AssertionError("settings")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(asmond.main(["export", "session", "--output-dir", "new"]), 0)
            export.assert_called_once()
            with self.assertRaises(SystemExit):
                asmond.main(["--remove-settings", "record", "--mock"])

    def test_write_failure_never_deletes_existing_session_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.DailyWriter, "write", side_effect=OSError("SECRET_DISK_PATH")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(recording.run_record(arguments(root), asmond), 1)
            self.assertEqual(len(list(Path(root).iterdir())), 1)

    def test_cli_defaults_selection_validation_and_tui_dispatch_are_independent(self):
        with mock.patch.object(asmond, "load_settings", return_value={"interval": 0.2}), contextlib.redirect_stderr(io.StringIO()):
            parser = asmond.build_parser()
            self.assertEqual(parser.parse_args(["record"]).interval, 10)
            self.assertEqual(parser.parse_args(["record", "-i", "120"]).interval, 120)
            self.assertEqual(parser.parse_args([]).interval, 0.2)
            for values in (["record", "-i", "nan"], ["record", "-i", "0"], ["record", "--duration", "inf"], ["record", "--groups", "memory,memory"], ["record", "--groups", "unknown"]):
                with self.assertRaises(SystemExit):
                    parser.parse_args(values)
        with mock.patch.object(recording, "run_record", return_value=7) as run, mock.patch.object(asmond.curses, "wrapper") as ui, mock.patch.object(asmond, "load_settings", return_value={}):
            self.assertEqual(asmond.main(["record", "--mock", "--duration", "1"]), 7)
            self.assertEqual(run.call_args.args[0].interval, 10)
            ui.assert_not_called()

    def test_mock_record_export_are_fully_generated_no_real_readers_or_settings_writes(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(asmond, "read_memory_stats", side_effect=AssertionError("real memory")), mock.patch.object(recording, "read_processes", side_effect=AssertionError("real ps")), mock.patch.object(asmond, "read_cpu_load_snapshot", side_effect=AssertionError("real cpu")), mock.patch.object(asmond, "read_io_snapshot", side_effect=AssertionError("real io")), mock.patch.object(asmond, "read_charge_stats", side_effect=AssertionError("real charge")), mock.patch.object(asmond, "save_settings", side_effect=AssertionError("settings write")), mock.patch.object(asmond, "refresh_sudo_credentials", side_effect=AssertionError("sudo")), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root)
            self.assertEqual(recording.run_record(args, asmond), 0)
            folder = next(Path(root).iterdir())
            records = list(recording.read_records(folder))
            self.assertTrue(records[0]["mock"])
            self.assertEqual(records[-1]["type"], "session_end")
            self.assertEqual(records[-1]["reason"], "completed")
            samples = [row for row in records if row["type"] == "sample"]
            self.assertEqual(len(samples), 1)
            cpu = samples[0]["groups"]["cpu"]
            self.assertAlmostEqual(cpu["host_usage_pct"], sum(core["usage_pct"] for core in cpu["cores"].values()) / len(cpu["cores"]))
            self.assertEqual(samples[0]["groups"]["memory"]["activity"]["swapins"]["pages_total"], 0)
            self.assertNotIn("SECRET", json.dumps(records))

    def test_selection_does_not_poll_unselected_sources(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "memory")
            capture = recording.Capture(asmond, args, None)
            with mock.patch.object(asmond, "read_charge_stats", side_effect=AssertionError), mock.patch.object(asmond, "read_cpu_load_snapshot", side_effect=AssertionError), mock.patch.object(recording, "read_processes", side_effect=AssertionError):
                result = capture.read()
            self.assertEqual(set(result["groups"]), {"memory"})

    def test_acquisition_failures_are_explicit_and_clear_counter_baselines(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root)
            args.mock = False
            capture = recording.Capture(asmond, args, None)
            with mock.patch.object(asmond, "read_memory_stats", side_effect=OSError("SECRET")), mock.patch.object(asmond, "read_cpu_load_snapshot", side_effect=OSError("SECRET")), mock.patch.object(recording, "read_processes", side_effect=OSError("SECRET")), mock.patch.object(asmond, "read_io_snapshot", side_effect=OSError("SECRET")):
                result = capture.read()
            self.assertTrue(all(value is None for value in result["groups"].values()))
            self.assertTrue(all(row["status"] == "unavailable" for row in result["sources"].values()))
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertIsNone(capture.rates.previous)
            self.assertIsNone(capture.ranks.previous)

    def test_record_partial_io_read_keeps_the_other_rates_and_reports_failure(self):
        key = ("en0", "<Link#4>", "00:00:00:00:00:01")
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "io", "--interval", "1")
            args.mock = False
            capture = recording.Capture(asmond, args, None)
            snapshots = [
                asmond.IoSnapshot(timestamp=10, disk_counters={1: (100, 200)}, net_counters={key: (50, 90)}),
                asmond.IoSnapshot(timestamp=12, disk_counters={}, net_counters={key: (150, 90)}, failed_readers=("disk",)),
                asmond.IoSnapshot(timestamp=14, disk_counters={}, net_counters={}, failed_readers=("network", "disk")),
            ]
            with mock.patch.object(asmond, "read_io_snapshot", side_effect=snapshots):
                rows = [capture.read() for _ in snapshots]
        baseline, partial, absent = rows
        self.assertEqual(baseline["sources"]["io"]["status"], "baseline_or_reset")
        self.assertEqual(partial["groups"]["io"], {
            "disk_read_bps": None, "disk_write_bps": None, "net_in_bps": 50, "net_out_bps": 0,
        })
        self.assertEqual(partial["sources"]["io"]["status"], "partial")
        self.assertEqual(partial["sources"]["io"]["error"], "disk")
        self.assertIsNone(absent["groups"]["io"])
        self.assertEqual(absent["sources"]["io"]["status"], "unavailable")
        for row in rows:
            recording.validate_record(row)

    def test_io_counter_resets_and_gaps_do_not_become_fake_zero_traffic(self):
        with tempfile.TemporaryDirectory() as root:
            args = arguments(root, "--groups", "io", "--interval", "1")
            args.mock = False
            capture = recording.Capture(asmond, args, None)
            snapshots = [asmond.IoSnapshot(timestamp=t, disk_read_bytes=n, disk_write_bytes=n, net_in_bytes=n, net_out_bytes=n) for t, n in ((1, 10), (2, 20), (3, 0), (100, 1000))]
            with mock.patch.object(asmond, "read_io_snapshot", side_effect=snapshots):
                values = [capture.read()["groups"]["io"] for _ in snapshots]
            self.assertIsNone(values[0]["disk_read_bps"])
            self.assertEqual(values[1]["disk_read_bps"], 10)
            self.assertIsNone(values[2]["disk_read_bps"])
            self.assertIsNone(values[3]["disk_read_bps"])

    def test_feed_age_stale_stop_and_no_old_value_substitution(self):
        feed = recording.StreamFeed(asmond, 1, False)
        sample = asmond.MetricSample(cpu_power_mw=0)
        stream = types.SimpleNamespace(stop=mock.Mock())
        feed.stream = stream
        with mock.patch.object(recording.time, "monotonic", return_value=10):
            feed.started, feed.latest = 1, (sample, 9)
            value, info = feed.snapshot()
            self.assertEqual(value.cpu_power_mw, 0)
            self.assertEqual(info["received_age_s"], 1)
            value.cpu_power_mw = 99
            self.assertEqual(sample.cpu_power_mw, 0)
            feed.latest = (sample, 1)
            value, info = feed.snapshot()
            self.assertIsNone(value)
            self.assertEqual(info["status"], "stale")
            stream.stop.assert_called_once()
            feed.invalidate()
            self.assertIsNone(feed.latest)
            self.assertEqual(feed.status, "clock_gap")

    def test_feed_rejects_inflight_old_samples_and_recovers_after_invalidation(self):
        for hold_constructor, stale in ((False, False), (True, False), (False, True)):
            with self.subTest(constructor=hold_constructor, stale=stale):
                ready, release, progress, fresh_release, published, finish = (threading.Event() for _ in range(6))
                old = asmond.MetricSample(cpu_power_mw=1234)
                fresh = asmond.MetricSample(cpu_power_mw=222)
                created = []

                class FakeStream:
                    def __init__(self, first):
                        self.first = first

                    def samples(self):
                        if self.first:
                            if not hold_constructor:
                                ready.set()
                                release.wait(timeout=2)
                            yield old
                            progress.set()
                        else:
                            fresh_release.wait(timeout=2)
                            yield fresh
                            published.set()
                        finish.wait(timeout=2)

                    def stop(self):
                        pass

                def factory(*_args, **_kwargs):
                    stream = FakeStream(not created)
                    created.append(stream)
                    if stream.first and hold_constructor:
                        ready.set()
                        release.wait(timeout=2)
                    elif not stream.first:
                        progress.set()
                    return stream

                backend = types.SimpleNamespace(PowerMetricsStream=factory, powermetrics_sample_is_usable=asmond.powermetrics_sample_is_usable)
                feed = recording.StreamFeed(backend, 1, False)
                try:
                    feed.start()
                    self.assertTrue(ready.wait(timeout=1))
                    if stale:
                        with feed.lock:
                            feed.latest = (old, time.monotonic() - 10)
                        self.assertIsNone(feed.snapshot()[0])
                    else:
                        feed.invalidate()
                    release.set()
                    self.assertTrue(progress.wait(timeout=1))
                    value, source = feed.snapshot()
                    self.assertIsNone(value)
                    self.assertEqual(source["status"], "stale" if stale else "clock_gap")
                    fresh_release.set()
                    self.assertTrue(published.wait(timeout=1))
                    value, source = feed.snapshot()
                    self.assertEqual(value.cpu_power_mw, 222)
                    self.assertEqual(source["status"], "ok")
                finally:
                    release.set()
                    fresh_release.set()
                    finish.set()
                    feed.stop()
                self.assertFalse(feed.thread.is_alive())

    def test_gap_wakes_pending_retry_and_stop_leaves_no_retry_worker(self):
        retry_entered, restarted = threading.Event(), threading.Event()
        created = []

        def factory(*_args, **_kwargs):
            stream = types.SimpleNamespace(samples=lambda: iter(()), stop=mock.Mock())
            created.append(stream)
            if len(created) == 2:
                restarted.set()
            return stream

        def delay(_retry):
            retry_entered.set()
            return 30

        backend = types.SimpleNamespace(PowerMetricsStream=factory, powermetrics_sample_is_usable=asmond.powermetrics_sample_is_usable)
        feed = recording.StreamFeed(backend, 1, False)
        with mock.patch.object(recording.SamplerRetryState, "schedule", delay):
            try:
                feed.start()
                self.assertTrue(retry_entered.wait(timeout=1))
                feed.invalidate()
                self.assertTrue(restarted.wait(timeout=1), "clock gap must not wait for the previous retry timer")
                self.assertEqual(len(created), 2)
            finally:
                feed.stop()
        self.assertFalse(feed.thread.is_alive())

    def test_clock_discontinuity_resets_rates_and_received_power_without_guessing_sleep(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(asmond.SSD_TEMPS, "invalidate") as invalidate_storage:
            args = arguments(root, "--groups", "memory,processes")
            capture = recording.Capture(asmond, args, None)
            with mock.patch.object(recording.time, "time", return_value=100), mock.patch.object(recording.time, "monotonic", return_value=1):
                capture.read()
            capture.feed = mock.Mock()
            capture.feed.snapshot.return_value = None, {"status": "clock_gap"}
            with mock.patch.object(recording.time, "time", return_value=200), mock.patch.object(recording.time, "monotonic", return_value=2):
                result = capture.read()
            capture.feed.invalidate.assert_called_once()
            self.assertEqual(result["observation_gap"]["wall_elapsed_s"], 100)
            self.assertEqual(result["observation_gap"]["monotonic_elapsed_s"], 1)
            self.assertEqual(result["groups"]["memory"]["activity"]["swapins"]["status"], "baseline")
            self.assertEqual(result["groups"]["processes"]["top_cpu"], [])
            self.assertIn("observation_gap", {row["event"] for row in recording.Events((80, 1024, 100)).read(result)})
            invalidate_storage.assert_called_once()

    def test_failed_first_file_does_not_create_an_orphan_end_marker(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(recording, "open_private_file", side_effect=OSError("first file unavailable")) as opened:
                self.assertEqual(recording.run_record(arguments(root), asmond), 1)
            opened.assert_called_once()
            self.assertEqual(list(Path(root).rglob("*.jsonl")), [])

    def test_interrupt_finalizes_received_data_and_never_calls_kill_or_ui(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.Capture, "read", side_effect=KeyboardInterrupt), mock.patch.object(asmond.os, "kill", side_effect=AssertionError("no process actions")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recording.run_record(arguments(root), asmond), 130)
            records = list(recording.read_records(next(Path(root).iterdir())))
            self.assertEqual(records[-1]["reason"], "interrupted")

    def test_sigint_after_flush_counts_sample_and_summary_can_read_it(self):
        write = recording.DailyWriter.write

        def interrupt_after_flush(writer, row, **kwargs):
            write(writer, row, **kwargs)
            if row["type"] == "sample":
                signal.raise_signal(signal.SIGINT)

        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(recording.DailyWriter, "write", new=interrupt_after_flush):
                self.assertEqual(recording.run_record(arguments(root), asmond), 130)
            session = next(Path(root).iterdir())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(recording.run_summary(argparse.Namespace(session=session), asmond), 0)
            self.assertIn("Stop reason: interrupted", output.getvalue())
            self.assertIn("Samples: 1 ", output.getvalue())

    def test_sigterm_during_authorization_or_cleanup_finalizes_the_record(self):
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            for during_auth in (True, False):
                with self.subTest(during_auth=during_auth), tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    args = arguments(root, "--groups", "power")
                    args.mock = not during_auth
                    if during_auth:
                        stack.enter_context(mock.patch.object(asmond.platform, "system", return_value="Darwin"))
                        stack.enter_context(mock.patch.object(asmond, "ensure_powermetrics_access",
                                                              side_effect=lambda _: signal.raise_signal(signal.SIGTERM)))
                        keeper = stack.enter_context(mock.patch.object(asmond, "SudoKeeper"))
                    else:
                        stack.enter_context(mock.patch.object(recording.StreamFeed, "start"))
                        stack.enter_context(mock.patch.object(recording.StreamFeed, "stop",
                                                              side_effect=lambda: signal.raise_signal(signal.SIGTERM)))
                    self.assertEqual(recording.run_record(args, asmond), 0)
                    end = list(recording.read_records(next(Path(root).iterdir())))[-1]
                    self.assertEqual((end["type"], end["reason"], end["samples"]),
                                     ("session_end", "terminated", 0 if during_auth else 1))
                    self.assertIs(signal.getsignal(signal.SIGTERM), handler)
                    if during_auth:
                        keeper.assert_not_called()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_size_limit_stops_without_deleting_recordings(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            original = recording.DailyWriter.write
            def write(writer, row, **kwargs):
                if row["type"] == "sample":
                    writer.total = writer.limit - 8192
                return original(writer, row, **kwargs)
            with mock.patch.object(recording.DailyWriter, "write", new=write):
                self.assertEqual(recording.run_record(arguments(root), asmond), 0)
            records = list(recording.read_records(next(Path(root).iterdir())))
            self.assertEqual(records[0]["type"], "session_start")
            self.assertEqual(records[-1]["reason"], "size_limit")

    def test_unexpected_failure_is_not_labelled_completed(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.Capture, "read", side_effect=RuntimeError("SECRET")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(recording.run_record(arguments(root), asmond), 1)
            records = list(recording.read_records(next(Path(root).iterdir())))
            self.assertEqual(records[-1]["reason"], "recording_error")
            self.assertNotIn("SECRET", json.dumps(records))

    def test_three_virtual_days_have_bounded_memory_daily_files_and_fresh_sessions(self):
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        epoch = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(recording.time, "time", side_effect=lambda: epoch.timestamp() + now[0]), mock.patch.object(recording, "timestamp", side_effect=lambda: (epoch + timedelta(seconds=now[0])).isoformat().replace("+00:00", "Z")), mock.patch.object(recording.time, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", "memory", "--interval", "3600", "--duration", str(3*86400+1))
            self.assertEqual(recording.run_record(args, asmond), 0)
            folder = next(Path(root).iterdir())
            self.assertEqual(len(list(folder.glob("*.jsonl"))), 4)
            self.assertEqual(sum(row["type"] == "sample" for row in recording.read_records(folder)), 73)
            self.assertLess(sum(path.stat().st_size for path in folder.iterdir()), 512*1024)


if __name__ == "__main__":
    unittest.main()
