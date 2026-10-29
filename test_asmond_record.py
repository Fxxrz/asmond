import argparse
import contextlib
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
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
            with mock.patch.object(recording, "private_csv", side_effect=change), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as messages:
                self.assertEqual(recording.run_export(argparse.Namespace(session=writer.directory, output_dir=output), asmond), 1)
            self.assertNotIn("CSV export:", messages.getvalue())
            self.assertIn(b'"used_bytes":200', path.read_bytes())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
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
        with mock.patch.object(feed.stop_event, "wait", side_effect=lambda _: feed.stop_event.set()):
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
        with mock.patch.object(feed.stop_event, "wait", side_effect=wait):
            feed._run()
        self.assertEqual(feed.status, "unavailable")
        self.assertIsNone(feed.latest)
        backend.PowerMetricsStream.assert_called_once()
        stream = types.SimpleNamespace(samples=lambda: iter([asmond.MetricSample(cpu_power_mw=0), asmond.MetricSample(warning="invalid")]), stop=mock.Mock())
        backend.PowerMetricsStream = lambda _: stream
        feed = recording.StreamFeed(backend, 1, False)
        with mock.patch.object(feed.stop_event, "wait", side_effect=lambda delay: feed.stop_event.set()):
            feed._run()
        self.assertIsNone(feed.latest)
        stream.stop.assert_called_once()
        observed = []
        def samples():
            yield asmond.MetricSample(cpu_power_mw=0, warning="partial sample")
            observed.append(feed.snapshot()[1]["status"])
        stream.samples = samples
        feed = recording.StreamFeed(backend, 1, False)
        with mock.patch.object(feed.stop_event, "wait", side_effect=lambda delay: feed.stop_event.set()):
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
            self.assertEqual(samples[0]["groups"]["cpu"]["host_usage_pct"], 25)
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

    def test_clock_discontinuity_resets_rates_and_received_power_without_guessing_sleep(self):
        with tempfile.TemporaryDirectory() as root:
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

    def test_interrupt_finalizes_received_data_and_never_calls_kill_or_ui(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.Capture, "read", side_effect=KeyboardInterrupt), mock.patch.object(asmond.os, "kill", side_effect=AssertionError("no process actions")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recording.run_record(arguments(root), asmond), 130)
            records = list(recording.read_records(next(Path(root).iterdir())))
            self.assertEqual(records[-1]["reason"], "interrupted")

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
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def set(self):
                self.stopped = True
            def wait(self, delay):
                now[0] += delay
                return False
        epoch = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(recording.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(recording.time, "time", side_effect=lambda: epoch.timestamp() + now[0]), mock.patch.object(recording, "timestamp", side_effect=lambda: (epoch + timedelta(seconds=now[0])).isoformat().replace("+00:00", "Z")), mock.patch.object(recording.threading, "Event", side_effect=Stop), contextlib.redirect_stdout(io.StringIO()):
            args = arguments(root, "--groups", "memory", "--interval", "3600", "--duration", str(3*86400+1))
            self.assertEqual(recording.run_record(args, asmond), 0)
            folder = next(Path(root).iterdir())
            self.assertEqual(len(list(folder.glob("*.jsonl"))), 4)
            self.assertEqual(sum(row["type"] == "sample" for row in recording.read_records(folder)), 73)
            self.assertLess(sum(path.stat().st_size for path in folder.iterdir()), 512*1024)


if __name__ == "__main__":
    unittest.main()
