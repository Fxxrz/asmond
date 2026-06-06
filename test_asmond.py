import argparse
import os
import plistlib
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import asmond


class SwapParsingTests(unittest.TestCase):
    def test_parse_swapusage_megabytes(self) -> None:
        total, used, free = asmond.parse_swapusage(
            "total = 2048.00M  used = 1024.00M  free = 1024.00M  (encrypted)"
        )
        self.assertEqual(total, 2 * 1024**3)
        self.assertEqual(used, 1024**3)
        self.assertEqual(free, 1024**3)

    def test_parse_swapusage_gigabytes(self) -> None:
        total, used, free = asmond.parse_swapusage(
            "vm.swapusage: total = 4.00G  used = 1.30G  free = 2.70G  (encrypted)"
        )
        self.assertEqual(total, 4 * 1024**3)
        self.assertEqual(used, int(1.30 * 1024**3))
        self.assertEqual(free, int(2.70 * 1024**3))

    def test_parse_swapusage_decimal_comma(self) -> None:
        total, used, free = asmond.parse_swapusage(
            "total = 4,00G  used = 1,30G  free = 2,70G  (encrypted)"
        )
        self.assertEqual(total, 4 * 1024**3)
        self.assertEqual(used, int(1.30 * 1024**3))
        self.assertEqual(free, int(2.70 * 1024**3))


class BatteryParsingTests(unittest.TestCase):
    def test_normalize_battery_temp_hundredths_celsius(self) -> None:
        self.assertAlmostEqual(asmond.normalize_battery_temp_c(2970), 29.7)

    def test_normalize_battery_temp_tenths_celsius(self) -> None:
        self.assertAlmostEqual(asmond.normalize_battery_temp_c(202), 20.2)

    def test_read_battery_items_uses_ioreg_plist(self) -> None:
        old_run = asmond.subprocess.run
        expected_command = ["ioreg", "-a", "-r", "-c", "AppleSmartBattery"]
        payload = [{"IOObjectClass": "AppleSmartBattery", "CurrentCapacity": 71}]
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            self.assertTrue(kwargs.get("capture_output"))
            return subprocess.CompletedProcess(command, 0, plistlib.dumps(payload), b"")

        try:
            asmond.subprocess.run = fake_run
            self.assertEqual(asmond.read_battery_items(), payload[0])
            self.assertEqual(calls, [expected_command])
        finally:
            asmond.subprocess.run = old_run


class IoStatsTests(unittest.TestCase):
    def test_io_stats_keeps_disk_read_and_write_separate(self) -> None:
        previous = asmond.IoSnapshot(timestamp=10.0, disk_read_bytes=1000, disk_write_bytes=2000)
        current = asmond.IoSnapshot(timestamp=12.0, disk_read_bytes=5000, disk_write_bytes=3000)
        stats = asmond.io_stats_from_snapshots(previous, current)
        self.assertEqual(stats.disk_read_bps, 2000)
        self.assertEqual(stats.disk_write_bps, 500)
        self.assertEqual(stats.disk_bps, 2500)

    def test_side_worker_skips_hidden_io_and_process_polling(self) -> None:
        calls = {"io": 0, "processes": 0}
        old_read_io = asmond.read_io_snapshot
        old_read_processes = asmond.read_processes
        old_read_memory = asmond.read_memory_stats
        old_read_charge = asmond.read_charge_stats

        def fake_read_io() -> asmond.IoSnapshot:
            calls["io"] += 1
            return asmond.IoSnapshot(timestamp=time.monotonic())

        def fake_read_processes() -> list[asmond.ProcessInfo]:
            calls["processes"] += 1
            return []

        try:
            asmond.read_io_snapshot = fake_read_io
            asmond.read_processes = fake_read_processes
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda: (asmond.BatteryStats(), asmond.UsbCStats())
            updates: queue.Queue[asmond.SideMetricsUpdate] = queue.Queue()
            stop = threading.Event()
            poll_state = asmond.SideMetricsPollState(poll_io=False, poll_processes=False)
            worker = threading.Thread(target=asmond.side_metrics_worker, args=(updates, stop, poll_state))
            worker.start()
            time.sleep(0.1)
            stop.set()
            worker.join(timeout=1.0)
            self.assertEqual(calls["io"], 0)
            self.assertEqual(calls["processes"], 0)
        finally:
            asmond.read_io_snapshot = old_read_io
            asmond.read_processes = old_read_processes
            asmond.read_memory_stats = old_read_memory
            asmond.read_charge_stats = old_read_charge


class BandwidthParsingTests(unittest.TestCase):
    def test_bandwidth_counters_from_plist(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "bandwidth_counters": [
                    {"name": "CPU", "value": 12_500_000_000},
                    {"name": "GPU", "value": 3_250_000_000},
                    {"name": "ANE0", "value": 750_000_000},
                    {"name": "DRAM", "value": 20_000_000_000},
                ]
            }
        )
        self.assertAlmostEqual(sample.memory_bandwidth_gbps["CPU"], 12.5)
        self.assertAlmostEqual(sample.memory_bandwidth_gbps["GPU"], 3.25)
        self.assertAlmostEqual(sample.memory_bandwidth_gbps["ANE"], 0.75)
        self.assertAlmostEqual(sample.memory_bandwidth_gbps["DRAM"], 20.0)

    def test_bandwidth_labels_combine_multiple_ane_counters(self) -> None:
        counters = asmond.bandwidth_counters_from_plist(
            {
                "bandwidth_counters": [
                    {"name": "ANE0", "value": 1_000_000_000},
                    {"name": "ANE1", "value": 2_000_000_000},
                ]
            }
        )
        self.assertEqual(counters, {"ANE": 3.0})


class UsbCTests(unittest.TestCase):
    def test_decode_fixed_pdo(self) -> None:
        label, voltage, current, power = asmond.decode_fixed_pdo(0x0004B12C)
        self.assertEqual(label, "15V 3A")
        self.assertAlmostEqual(voltage or 0.0, 15.0)
        self.assertAlmostEqual(current or 0.0, 3.0)
        self.assertAlmostEqual(power or 0.0, 45.0)

    def test_usb_c_stats_decodes_active_contract(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "IsCharging": True,
                "BestAdapterIndex": 0,
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    }
                ],
            }
        )
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "USB-C 1")
        self.assertEqual(stats.active_port.role, "sink")
        self.assertAlmostEqual(stats.active_port.voltage_v or 0.0, 20.0)
        self.assertAlmostEqual(stats.active_port.current_a or 0.0, 1.5)
        self.assertAlmostEqual(stats.active_port.power_w or 0.0, 30.0)

    def test_usb_c_stats_prefers_connected_port_over_best_adapter_index(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 0,
                "AdapterDetails": {"UsbHvcHvcIndex": 3, "AdapterVoltage": 20000, "Current": 1740},
                "PortControllerInfo": [
                    {"PortControllerActiveContractRdo": 0, "PortControllerNPDOs": 0, "PortControllerPortPDO": []},
                    {"PortControllerActiveContractRdo": 0, "PortControllerNPDOs": 0, "PortControllerPortPDO": []},
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                ],
                "FedDetails": [
                    {"FedExternalConnected": 0},
                    {"FedExternalConnected": 0},
                    {"FedExternalConnected": 1},
                ],
            }
        )
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "MagSafe")
        self.assertEqual([port.label for port in stats.ports], ["USB-C 1", "USB-C 2", "MagSafe"])
        self.assertEqual(stats.active_port.role, "sink")
        self.assertAlmostEqual(stats.adapter_voltage_v or 0.0, 20.0)
        self.assertAlmostEqual(stats.adapter_current_a or 0.0, 1.74)

    def test_usb_c_stats_uses_hpm_port_activity_for_physical_port(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 0,
                "AdapterDetails": {"UsbHvcHvcIndex": 3, "AdapterVoltage": 20000, "Current": 1700},
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x478258A8,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0901F42C],
                    },
                    {
                        "PortControllerActiveContractRdo": 0x478258A8,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0901F42C],
                    },
                    {
                        "PortControllerActiveContractRdo": 0x2582582C,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0801F428],
                    },
                ],
                "FedDetails": [
                    {"FedExternalConnected": 1},
                    {"FedExternalConnected": 1},
                    {"FedExternalConnected": 1},
                ],
            },
            [
                {"PortDescription": "Port-USB-C@1", "PortTypeDescription": "USB-C", "ConnectionActive": 1},
                {"PortDescription": "Port-USB-C@2", "PortTypeDescription": "USB-C", "ConnectionActive": 0},
                {"PortDescription": "Port-MagSafe 3@1", "PortTypeDescription": "MagSafe 3", "ConnectionActive": 0},
            ],
        )
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "USB-C 1")
        self.assertEqual([port.connected for port in stats.ports], [True, False, False])
        self.assertEqual([port.label for port in stats.ports], ["USB-C 1", "USB-C 2", "MagSafe"])
        self.assertAlmostEqual(stats.adapter_voltage_v or 0.0, 20.0)
        self.assertAlmostEqual(stats.adapter_current_a or 0.0, 1.7)

    def test_usb_c_stats_ignores_stale_contract_when_disconnected(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": False,
                "AdapterDetails": {"UsbHvcHvcIndex": 2, "AdapterVoltage": 20000, "Current": 3000, "Watts": 60},
                "PowerTelemetryData": {"SystemVoltageIn": 20000, "SystemCurrentIn": 3000, "SystemPowerIn": 60000},
                "PortControllerInfo": [
                    {"PortControllerActiveContractRdo": 0, "PortControllerNPDOs": 0, "PortControllerPortPDO": []},
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                ],
                "FedDetails": [{"FedExternalConnected": 0}, {"FedExternalConnected": 0}],
            }
        )
        self.assertIsNone(stats.active_port)
        self.assertEqual([port.connected for port in stats.ports], [False, False])
        self.assertIsNone(stats.adapter_voltage_v)
        self.assertIsNone(stats.adapter_power_w)


class AlertTests(unittest.TestCase):
    def test_alert_thresholds(self) -> None:
        alerts = asmond.build_alerts(
            asmond.MetricSample(throttled=True, temp_max_c=90.0),
            asmond.MemoryStats(swap_used_bytes=2 * 1024**3),
            asmond.BatteryStats(power_mw=-20000.0),
        )
        self.assertIn("THROTTLE", alerts)
        self.assertIn("TEMP 90C", alerts)
        self.assertIn("SWAP 2.00 GiB", alerts)
        self.assertIn("BAT -20.00 W", alerts)

    def test_alert_thresholds_can_be_configured(self) -> None:
        args = argparse.Namespace(alert_temp_c=95.0, alert_swap_gib=3.0, alert_battery_drain_w=25.0)
        alerts = asmond.build_alerts(
            asmond.MetricSample(temp_max_c=90.0),
            asmond.MemoryStats(swap_used_bytes=2 * 1024**3),
            asmond.BatteryStats(power_mw=-20000.0),
            args,
        )
        self.assertEqual(alerts, [])


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostics_exit_code_treats_fail_as_error(self) -> None:
        self.assertEqual(asmond.diagnostics_exit_code([("python", "ok", "3"), ("pm", "warn", "cached")]), 0)
        self.assertEqual(asmond.diagnostics_exit_code([("powermetrics", "fail", "missing")]), 1)

    def test_mock_report_contains_anonymized_core_sections(self) -> None:
        args = argparse.Namespace(mock=True, live=False, interval=1.0)
        data = asmond.report_data(args)
        self.assertEqual(data["app"], "Asmond")
        self.assertIn("diagnostics", data)
        self.assertIn("snapshot", data)
        self.assertEqual(data["snapshot"]["usb_c"]["active_port"], "MagSafe")
        self.assertNotIn(str(Path.home()), data["settings_path"])

    def test_source_status_marks_missing_expected_sources(self) -> None:
        args = argparse.Namespace(mock=False, show_io=True, layout="full")
        text = asmond.source_status(
            args,
            asmond.MetricSample(warning="sampler stopped"),
            asmond.MemoryStats(),
            asmond.BatteryStats(),
            asmond.UsbCStats(),
            asmond.IoStats(),
            [],
            "left",
        )
        self.assertIn("src pm", text)
        self.assertIn("miss", text)
        self.assertIn("ioreg", text)
        self.assertIn("ps", text)

    def test_display_path_uses_path_relative_to_home(self) -> None:
        old_home = asmond.real_user_home
        try:
            asmond.real_user_home = lambda: Path("/Users/franz")
            self.assertEqual(asmond.display_path(Path("/Users/franz/Library/x")), "~/Library/x")
            self.assertEqual(asmond.display_path(Path("/Users/franz2/Library/x")), "/Users/franz2/Library/x")
        finally:
            asmond.real_user_home = old_home


class SettingsTests(unittest.TestCase):
    def test_save_and_load_all_interactive_settings(self) -> None:
        old_path = asmond.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as tmp_dir:
            asmond.SETTINGS_PATH = Path(tmp_dir) / "settings.json"
            try:
                args = argparse.Namespace(
                    theme="dracula",
                    interval=0.2,
                    layout="compact",
                    show_io=True,
                    upper_power_mode="gpu",
                    lower_power_mode="ane",
                    upper_io_mode="disk_write",
                    lower_io_mode="net_out",
                    load_view="graph",
                    process_panel="right",
                    process_sort="ram",
                    charge_panel="usb",
                    allow_root_kill=True,
                    alert_temp_c=90.0,
                    alert_swap_gib=2.5,
                    alert_battery_drain_w=20.0,
                )
                asmond.save_settings(args)
                self.assertEqual(
                    asmond.load_settings(),
                    {
                        "theme": "dracula",
                        "interval": 0.2,
                        "layout": "compact",
                        "show_io": True,
                        "upper_power_mode": "gpu",
                        "lower_power_mode": "ane",
                        "upper_io_mode": "disk_write",
                        "lower_io_mode": "net_out",
                        "load_view": "graph",
                        "process_panel": "right",
                        "process_sort": "ram",
                        "charge_panel": "usb",
                        "allow_root_kill": True,
                        "alert_temp_c": 90.0,
                        "alert_swap_gib": 2.5,
                        "alert_battery_drain_w": 20.0,
                    },
                )
            finally:
                asmond.SETTINGS_PATH = old_path

    def test_menu_value_text_formats_alerts(self) -> None:
        args = argparse.Namespace(
            theme="classic",
            layout="full",
            interval=0.5,
            show_io=False,
            alert_temp_c=88.0,
            alert_swap_gib=1.5,
            alert_battery_drain_w=18.0,
        )
        self.assertEqual(
            asmond.menu_value_text("alert_temp", args, "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery"),
            "88.0 C",
        )
        self.assertEqual(
            asmond.menu_value_text("alert_swap", args, "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery"),
            "1.50 GiB",
        )

    def test_legacy_layout_names_map_to_focus(self) -> None:
        old_path = asmond.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as tmp_dir:
            asmond.SETTINGS_PATH = Path(tmp_dir) / "settings.json"
            try:
                asmond.SETTINGS_PATH.write_text('{"layout": "thermals-only"}', encoding="utf-8")
                self.assertEqual(asmond.load_settings()["layout"], "focus")
                args = argparse.Namespace(
                    theme="classic",
                    interval=1.0,
                    layout="power-only",
                    show_io=False,
                    upper_power_mode="soc",
                    lower_power_mode="cpu",
                    upper_io_mode="disk_read",
                    lower_io_mode="net_in",
                    load_view="rows",
                    process_panel="hidden",
                    process_sort="cpu",
                    charge_panel="battery",
                    allow_root_kill=False,
                    alert_temp_c=85.0,
                    alert_swap_gib=1.0,
                    alert_battery_drain_w=15.0,
                )
                asmond.save_settings(args)
                self.assertEqual(asmond.load_settings()["layout"], "focus")
            finally:
                asmond.SETTINGS_PATH = old_path

    def test_default_settings_path_uses_application_support(self) -> None:
        old_env = os.environ.get(asmond.SETTINGS_DIR_ENV)
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.environ[asmond.SETTINGS_DIR_ENV] = tmp_dir
            try:
                self.assertEqual(asmond.default_settings_path(), Path(tmp_dir) / "settings.json")
            finally:
                if old_env is None:
                    os.environ.pop(asmond.SETTINGS_DIR_ENV, None)
                else:
                    os.environ[asmond.SETTINGS_DIR_ENV] = old_env

    def test_remove_settings_deletes_file(self) -> None:
        old_path = asmond.SETTINGS_PATH
        with tempfile.TemporaryDirectory() as tmp_dir:
            asmond.SETTINGS_PATH = Path(tmp_dir) / "Asmond" / "settings.json"
            try:
                asmond.SETTINGS_PATH.parent.mkdir()
                asmond.SETTINGS_PATH.write_text("{}", encoding="utf-8")
                self.assertIsNone(asmond.remove_settings())
                self.assertFalse(asmond.SETTINGS_PATH.exists())
            finally:
                asmond.SETTINGS_PATH = old_path


class ProcessTests(unittest.TestCase):
    def test_parse_process_line_accepts_decimal_comma(self) -> None:
        process = asmond.parse_process_line("123  12,5  3,4  45678 /Applications/Foo Bar.app/Contents/MacOS/Foo Bar")
        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.pid, 123)
        self.assertEqual(process.cpu_pct, 12.5)
        self.assertEqual(process.mem_pct, 3.4)
        self.assertEqual(process.rss_kib, 45678)
        self.assertEqual(process.command, "Foo Bar")

    def test_parse_process_line_with_ppid_and_runtime(self) -> None:
        process = asmond.parse_process_line("123  1  02:03  12.5  3.4  45678 /usr/bin/python3")
        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.ppid, 1)
        self.assertEqual(process.etime, "02:03")
        self.assertEqual(process.full_command, "/usr/bin/python3")

    def test_parse_process_line_with_user(self) -> None:
        process = asmond.parse_process_line("123 user 1 02:03 12.5 3.4 45678 /usr/bin/python3")
        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.user, "user")
        self.assertEqual(process.ppid, 1)
        self.assertEqual(process.full_command, "/usr/bin/python3")

    def test_sort_processes_by_cpu_and_ram(self) -> None:
        processes = [
            asmond.ProcessInfo(1, 1.0, 10.0, 100, "a"),
            asmond.ProcessInfo(2, 9.0, 1.0, 50, "b", gpu_pct=2.0),
            asmond.ProcessInfo(3, 2.0, 20.0, 200, "c", gpu_pct=25.0),
        ]
        self.assertEqual([item.pid for item in asmond.sorted_processes(processes, "cpu")], [2, 3, 1])
        self.assertEqual([item.pid for item in asmond.sorted_processes(processes, "ram")], [3, 1, 2])
        self.assertEqual([item.pid for item in asmond.sorted_processes(processes, "gpu")], [3, 2, 1])

    def test_process_gpu_pcts_from_plist(self) -> None:
        values = asmond.process_gpu_pcts_from_plist(
            {
                "tasks": [
                    {"pid": 123, "name": "WindowServer", "gpu_time_ms": 250.0},
                    {"pid": 456, "name": "Safari", "gpu_percent": 12.5},
                    {"pid": 789, "name": "Preview", "gpu": {"time_ns": 100_000_000}},
                    {"pid": 987, "name": "MetalApp", "gpu_time": 50_000_000},
                ]
            },
            interval_s=1.0,
        )
        self.assertAlmostEqual(values[123], 25.0)
        self.assertAlmostEqual(values[456], 12.5)
        self.assertAlmostEqual(values[789], 10.0)
        self.assertAlmostEqual(values[987], 5.0)

    def test_process_gpu_pcts_from_text(self) -> None:
        text = """
Name                               ID     CPU ms/s  User%  Deadlines (<2 ms, 2-5 ms)  Wakeups (Intr, Pkg idle)  GPU ms/s  Energy Impact
WindowServer                       611    188.39    63.79  30.43   5.89               207.14  114.86            125.00    14.98
Code Helper (GPU)                  59691  0.26      77.26  0.00    0.00               0.98    0.00              0.98      0.02
ALL_TASKS                          -2     649.10    59.87  145.06  12.83              1768.40 915.78            0.00      58.89
"""
        values = asmond.process_gpu_pcts_from_text(text)
        self.assertAlmostEqual(values[611], 12.5)
        self.assertAlmostEqual(values[59691], 0.098)
        self.assertNotIn(-2, values)

    def test_process_gpu_text_all_zero_means_unavailable(self) -> None:
        text = (Path(__file__).parent / "tests" / "fixtures" / "process_gpu_zero.txt").read_text()
        self.assertEqual(asmond.process_gpu_pcts_from_text(text), {})

    def test_process_gpu_unavailable_backoff(self) -> None:
        calls: list[list[str]] = []
        old_run = asmond.subprocess.run
        old_refresh = asmond.refresh_sudo_credentials
        state = asmond.ProcessGpuProbeState()

        def fake_run(command, **kwargs):
            calls.append(command)
            if "text" in command:
                return subprocess.CompletedProcess(command, 0, b"WindowServer 611 1.00 50.00 0.00 0.00 0.00 0.00 0.00 1.00\n", b"")
            return subprocess.CompletedProcess(command, 0, plistlib.dumps({"tasks": []}), b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.refresh_sudo_credentials = lambda prompt=False: (True, "")
            self.assertEqual(asmond.read_process_gpu_pcts(probe_state=state), {})
            self.assertEqual(len(calls), 2)
            self.assertEqual(asmond.read_process_gpu_pcts(probe_state=state), {})
            self.assertEqual(len(calls), 2)
            state.unavailable_until = 0.0
            self.assertEqual(asmond.read_process_gpu_pcts(probe_state=state), {})
            self.assertEqual(len(calls), 4)
        finally:
            asmond.subprocess.run = old_run
            asmond.refresh_sudo_credentials = old_refresh

    def test_merge_process_gpu_pcts(self) -> None:
        processes = [asmond.ProcessInfo(123, 1.0, 2.0, 4096, "a"), asmond.ProcessInfo(456, 3.0, 4.0, 8192, "b")]
        asmond.merge_process_gpu_pcts(processes, {456: 33.0})
        self.assertIsNone(processes[0].gpu_pct)
        self.assertEqual(processes[1].gpu_pct, 33.0)


if __name__ == "__main__":
    unittest.main()
