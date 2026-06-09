import argparse
import contextlib
import io
import shutil
import sys
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
        report = asmond.usb_to_report_dict(stats)
        self.assertEqual(report["state"], "no input")
        self.assertEqual(report["voltage"], "n/a")
        self.assertEqual(report["current"], "n/a")
        self.assertEqual(report["power"], "n/a")


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


class CapabilityTests(unittest.TestCase):
    def test_power_fallback_ignores_state_level_and_count_fields(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "gpu_power_state": 2,
                "cpu_power_level": 1,
                "processor_power_state": 3,
                "ane_power_count": 4,
                "cpu_power": 123,
                "gpu_power_mw": 456,
                "processor_package_power": 789,
            }
        )
        self.assertEqual(sample.cpu_power_mw, 123)
        self.assertEqual(sample.gpu_power_mw, 456)
        self.assertEqual(sample.soc_power_mw, 789)
        self.assertIsNone(sample.ane_power_mw)

    def test_usage_fallback_ignores_active_count_rates(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "duty_cycles": {
                        "cpu0": {
                            "active_count": 1,
                            "active_count_per_s": 100,
                        }
                    }
                }
            }
        )
        self.assertIsNone(sample.cpu_usage_pct)

    def test_power_zero_is_supported_but_missing_none_is_not(self) -> None:
        history = asmond.History(4)
        sample = asmond.MetricSample(cpu_power_mw=0.0)
        self.assertTrue(asmond.power_row_supported(sample, history, "cpu"))
        self.assertFalse(asmond.power_row_supported(sample, history, "gpu"))

    def test_battery_support_distinguishes_empty_from_disconnected(self) -> None:
        self.assertFalse(asmond.battery_supported(asmond.BatteryStats()))
        self.assertTrue(asmond.battery_supported(asmond.BatteryStats(external_connected=False)))
        self.assertTrue(asmond.battery_supported(asmond.BatteryStats(charge_pct=0.0)))

    def test_usb_c_support_keeps_known_ports_when_no_input_is_connected(self) -> None:
        self.assertFalse(asmond.usb_c_supported(asmond.UsbCStats()))
        self.assertTrue(asmond.usb_c_supported(asmond.UsbCStats(ports=[asmond.UsbCPortStats("USB-C 1", connected=False)])))
        self.assertFalse(asmond.usb_c_supported(asmond.UsbCStats(external_connected=False)))
        self.assertTrue(asmond.usb_c_supported(asmond.UsbCStats(system_voltage_v=20.0)))

    def test_usb_c_hpm_active_without_external_connected_keeps_measurements(self) -> None:
        usb_c = asmond.usb_c_stats_from_item(
            {
                "PowerTelemetryData": {
                    "SystemVoltageIn": 20_000,
                    "SystemCurrentIn": 1_500,
                    "SystemPowerIn": 30_000,
                },
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x10025800,
                        "PortControllerSrcPdoCount": 1,
                        "PortControllerSrcPdo1": 0x0001912C,
                    }
                ],
            },
            [{"PortDescription": "USB-C@1", "ConnectionActive": True}],
        )
        self.assertTrue(asmond.usb_c_supported(usb_c))
        self.assertEqual(usb_c.active_port.label, "USB-C 1")
        self.assertEqual(usb_c.active_port.role, "sink")
        self.assertAlmostEqual(usb_c.system_voltage_v, 20.0)
        self.assertAlmostEqual(usb_c.system_current_a, 1.5)
        self.assertAlmostEqual(usb_c.system_power_w, 30.0)
        self.assertEqual(asmond.usb_c_state_text(usb_c), "PD active")

    def test_report_marks_unsupported_battery_and_usb_c(self) -> None:
        battery_report = asmond.battery_to_report_dict(asmond.BatteryStats())
        usb_report = asmond.usb_to_report_dict(asmond.UsbCStats())
        self.assertFalse(battery_report["supported"])
        self.assertEqual(battery_report["state"], "unsupported")
        self.assertFalse(usb_report["supported"])
        self.assertEqual(usb_report["state"], "unsupported")
        no_input_report = asmond.usb_to_report_dict(asmond.UsbCStats(ports=[asmond.UsbCPortStats("USB-C 1")], external_connected=False))
        self.assertTrue(no_input_report["supported"])
        self.assertEqual(no_input_report["state"], "no input")

    def test_charge_panel_falls_back_only_when_requested_source_is_unsupported(self) -> None:
        battery = asmond.BatteryStats(charge_pct=50.0)
        usb_c = asmond.UsbCStats(ports=[asmond.UsbCPortStats("USB-C 1", connected=False)])
        self.assertEqual(asmond.effective_charge_panel("battery", battery, usb_c), "battery")
        self.assertEqual(asmond.effective_charge_panel("usb", battery, usb_c), "usb")
        self.assertEqual(asmond.effective_charge_panel("battery", asmond.BatteryStats(), usb_c), "usb")
        self.assertEqual(asmond.effective_charge_panel("usb", battery, asmond.UsbCStats()), "battery")


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
            asmond.real_user_home = lambda: Path("/Users/alice")
            self.assertEqual(asmond.display_path(Path("/Users/alice/Library/x")), "~/Library/x")
            self.assertEqual(asmond.display_path(Path("/Users/alice2/Library/x")), "/Users/alice2/Library/x")
        finally:
            asmond.real_user_home = old_home

    def test_live_report_reuses_collected_powermetrics_sample(self) -> None:
        calls: list[list[str]] = []
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_refresh = asmond.refresh_battery_power
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats
        payload = plistlib.dumps({"processor": {"cpu_power": 123}, "thermal_pressure": "Nominal"})

        def fake_run(command, **kwargs):
            if "powermetrics" not in command:
                return old_run(command, **kwargs)
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, payload, b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.refresh_battery_power = lambda sample: None
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda: (asmond.BatteryStats(), asmond.UsbCStats())
            args = argparse.Namespace(mock=False, live=True, interval=1.0)
            data = asmond.report_data(args)
            self.assertEqual(len(calls), 1)
            self.assertEqual(data["snapshot"]["powermetrics"]["cpu_power"], "123 mW")
            self.assertEqual(data["diagnostics"][-1]["name"], "powermetrics sample")
            self.assertEqual(data["diagnostics"][-1]["status"], "ok")
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure
            asmond.refresh_battery_power = old_refresh
            asmond.read_memory_stats = old_memory
            asmond.read_charge_stats = old_charge

    def test_probe_returns_error_for_invalid_plist(self) -> None:
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, b"not a plist", b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            args = argparse.Namespace(mock=False, interval=1.0, raw=False)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(asmond.run_probe(args), 1)
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure

    def test_mock_flag_is_accepted_after_subcommands(self) -> None:
        for argv in (["probe", "--mock"], ["doctor", "--mock"], ["report", "--mock"]):
            with self.subTest(argv=argv):
                args = asmond.build_parser().parse_args(argv)
                self.assertTrue(args.mock)


class PackagingTests(unittest.TestCase):
    def test_install_layout_runs_with_helper_modules_beside_entrypoint(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for path in root.glob("asmond*.py"):
                shutil.copy2(path, target / path.name)
            proc = subprocess.run(
                [sys.executable, str(target / "asmond.py"), "--version"],
                cwd="/tmp",
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Asmond", proc.stdout)


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
                    custom_slot="right_middle",
                    custom_layout={"right_middle": {"panel": "process", "detail": "detail"}},
                    custom_name="LLM view",
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
                        "custom_slot": "right_middle",
                        "custom_layout": {"right_middle": {"panel": "process", "detail": "detail"}},
                        "custom_name": "LLM view",
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
                    custom_slot="upper_left",
                    custom_layout={},
                    custom_name="full",
                )
                asmond.save_settings(args)
                self.assertEqual(asmond.load_settings()["layout"], "focus")
                self.assertNotIn("custom_name", asmond.load_settings())
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


class LayoutTests(unittest.TestCase):
    def test_full_layout_geometry_matches_existing_dashboard_grid(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden")
        self.assertEqual(layout.slot("top").rect, asmond.Rect(2, 0, 11, 181))
        self.assertEqual(layout.slot("upper_left").rect, asmond.Rect(13, 0, 9, 90))
        self.assertEqual(layout.slot("upper_right").rect, asmond.Rect(13, 90, 9, 91))
        self.assertEqual(layout.slot("lower_left").rect, asmond.Rect(22, 0, 38, 90))
        self.assertEqual(layout.slot("right_top").rect, asmond.Rect(22, 90, 8, 91))
        self.assertEqual(layout.slot("right_middle").rect, asmond.Rect(30, 90, 22, 91))
        self.assertEqual(layout.slot("right_lower").rect, asmond.Rect(52, 90, 8, 91))
        self.assertIsNone(layout.slot("right_bottom"))
        self.assertTrue(layout.left_io_panel)

    def test_full_layout_process_right_reuses_ram_slot(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", False, "right")
        process_slot = layout.slot("right_middle")
        self.assertIsNotNone(process_slot)
        self.assertEqual(process_slot.panel_id, "process")
        self.assertEqual(process_slot.rect, asmond.Rect(30, 90, 22, 91))

    def test_focus_layout_keeps_charge_area_below_top_panels(self) -> None:
        layout = asmond.dashboard_layout(40, 120, "focus", False, "hidden")
        self.assertEqual(layout.slot("top").rect, asmond.Rect(2, 0, 10, 120))
        self.assertEqual(layout.slot("upper_left").rect, asmond.Rect(12, 0, 8, 60))
        self.assertEqual(layout.slot("upper_right").rect, asmond.Rect(12, 60, 8, 60))
        self.assertEqual(layout.slot("main").rect, asmond.Rect(20, 0, 19, 120))

    def test_panel_specs_cover_builtin_dashboard_panels(self) -> None:
        self.assertIn("full", asmond.LAYOUT_TEMPLATES)
        for panel_id in ("power_graph", "power", "thermals", "load", "clocks", "ram", "charge", "io", "process"):
            self.assertIn(panel_id, asmond.PANEL_SPECS)

    def test_builtin_layout_templates_define_named_panel_slots(self) -> None:
        full_slots = [(slot.slot_id, slot.default_panel_id, slot.optional) for slot in asmond.LAYOUT_TEMPLATES["full"].slots]
        self.assertEqual(
            full_slots,
            [
                ("top", "power_graph", False),
                ("upper_left", "power", False),
                ("upper_right", "thermals", False),
                ("lower_left", "load", False),
                ("right_top", "clocks", False),
                ("right_middle", "ram", False),
                ("right_lower", "charge", True),
                ("right_bottom", "io", True),
            ],
        )
        focus_slots = [(slot.slot_id, slot.default_panel_id) for slot in asmond.LAYOUT_TEMPLATES["focus"].slots]
        self.assertEqual(focus_slots, [("top", "power_graph"), ("upper_left", "power"), ("upper_right", "thermals"), ("main", "charge")])
        custom_slots = [(slot.slot_id, slot.default_panel_id, slot.optional) for slot in asmond.LAYOUT_TEMPLATES["custom"].slots]
        self.assertEqual(custom_slots, full_slots)

    def test_full_layout_panel_placements_have_detail_levels(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden")
        panels = {(panel.panel_id, panel.slot): panel for panel in layout.panels}
        self.assertEqual(panels[("power_graph", "top")].detail, "detail")
        self.assertEqual(panels[("power", "upper_left")].detail, "normal")
        self.assertEqual(panels[("thermals", "upper_right")].detail, "normal")
        self.assertEqual(panels[("load", "lower_left")].detail, "detail")
        self.assertEqual(panels[("ram", "right_middle")].detail, "detail")
        self.assertEqual(panels[("charge", "right_lower")].detail, "normal")

    def test_compact_layout_uses_compact_panel_details(self) -> None:
        layout = asmond.dashboard_layout(32, 100, "compact", True, "hidden")
        self.assertTrue(layout.panels)
        panels = {(panel.panel_id, panel.slot): panel.detail for panel in layout.panels}
        self.assertEqual(panels[("power", "upper_left")], "compact")
        self.assertEqual(panels[("ram", "right_middle")], "compact")
        self.assertEqual(panels[("charge", "right_lower")], "compact")
        self.assertEqual(panels[("thermals", "upper_right")], "normal")
        self.assertEqual(panels[("load", "lower_left")], "detail")

    def test_panel_specs_only_offer_real_detail_variants(self) -> None:
        self.assertEqual(asmond.detail_levels_for_panel("power"), ("compact", "normal"))
        self.assertEqual(asmond.detail_levels_for_panel("ram"), ("compact", "detail"))
        self.assertEqual(asmond.detail_levels_for_panel("charge"), ("compact", "normal", "detail"))
        self.assertEqual(asmond.detail_levels_for_panel("thermals"), ("normal",))
        self.assertEqual(asmond.detail_levels_for_panel("load"), ("detail",))
        self.assertEqual(asmond.cycle_value(asmond.detail_levels_for_panel("load"), "detail", 1), "detail")

    def test_tailor_slot_key_mapping_is_scoped_to_custom_slots(self) -> None:
        self.assertEqual(asmond.tailor_slot_number("upper_left"), 1)
        self.assertEqual(asmond.tailor_slot_for_key(ord("1")), "upper_left")
        self.assertEqual(asmond.tailor_slot_for_key(ord("7")), "right_bottom")
        self.assertIsNone(asmond.tailor_slot_for_key(ord("8")))
        self.assertEqual(asmond.TAILOR_MENU_ITEMS, ("panel", "detail", "name"))

    def test_tailor_menu_stays_at_top_and_switches_sides(self) -> None:
        left_anchor = asmond.Rect(20, 0, 10, 80)
        right_anchor = asmond.Rect(20, 100, 10, 80)
        self.assertEqual(asmond.tailor_menu_rect(60, 180, left_anchor), asmond.Rect(2, 132, 8, 46))
        self.assertEqual(asmond.tailor_menu_rect(60, 180, right_anchor), asmond.Rect(2, 2, 8, 46))

    def test_custom_layout_name_is_cleaned_and_rejects_reserved_names(self) -> None:
        self.assertEqual(asmond.clean_custom_name("  LLM   view  "), "LLM view")
        self.assertIsNone(asmond.custom_name_error("LLM view"))
        self.assertEqual(asmond.custom_name_error("custom"), "name is reserved")
        self.assertEqual(asmond.custom_name_error(" "), "name is empty")
        args = argparse.Namespace(layout="custom", custom_name="LLM view")
        self.assertEqual(asmond.custom_layout_label(args), "custom:LLM view")

    def test_process_panel_replaces_the_matching_slot_metadata(self) -> None:
        left = asmond.dashboard_layout(61, 181, "full", False, "left")
        right = asmond.dashboard_layout(61, 181, "full", False, "right")
        self.assertEqual(asmond.layout_panel(left, "process", "lower_left").rect, left.slot("lower_left").rect)
        self.assertEqual(asmond.layout_panel(right, "process", "right_middle").rect, right.slot("right_middle").rect)

    def test_dashboard_layout_can_lookup_panels_by_slot_or_panel_id(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden")
        self.assertEqual(layout.slot("upper_left"), layout.panel("power", "upper_left"))
        self.assertEqual(layout.slot("right_middle"), layout.panel("ram", "right_middle"))
        self.assertIsNone(layout.slot("missing"))

    def test_io_slot_is_only_present_when_layout_allocates_it(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden")
        self.assertIsNone(asmond.layout_panel(layout, "io", "right_bottom"))
        placements = asmond.panel_placements_from_template(
            "full",
            asmond.LAYOUT_TEMPLATES["full"],
            {"right_bottom": asmond.Rect(52, 90, 7, 91)},
        )
        self.assertEqual(placements, (asmond.PanelPlacement("io", asmond.Rect(52, 90, 7, 91), "normal", "right_bottom"),))

    def test_custom_layout_assigns_panel_and_detail_to_slots(self) -> None:
        layout = asmond.dashboard_layout(
            61,
            181,
            "custom",
            False,
            "hidden",
            {
                "upper_left": {"panel": "ram", "detail": "compact"},
                "right_middle": {"panel": "process", "detail": "detail"},
            },
        )
        self.assertEqual(layout.slot("upper_left").panel_id, "ram")
        self.assertEqual(layout.slot("upper_left").detail, "compact")
        self.assertEqual(layout.slot("right_middle").panel_id, "process")
        self.assertEqual(layout.slot("right_middle").detail, "detail")
        self.assertEqual(layout.slot("upper_right").panel_id, "thermals")

    def test_empty_custom_layout_starts_from_full_grid(self) -> None:
        full = asmond.dashboard_layout(61, 181, "full", False, "hidden")
        custom = asmond.dashboard_layout(61, 181, "custom", False, "hidden", {})
        self.assertEqual(
            [(panel.slot, panel.panel_id, panel.rect) for panel in custom.panels],
            [(panel.slot, panel.panel_id, panel.rect) for panel in full.panels],
        )

    def test_custom_layout_sanitizes_invalid_saved_slots(self) -> None:
        cleaned = asmond.sanitize_custom_layout(
            {
                "upper_left": {"panel": "ram", "detail": "tiny"},
                "nope": {"panel": "power", "detail": "normal"},
                "right_top": {"panel": "unknown", "detail": "detail"},
            }
        )
        self.assertEqual(cleaned, {"upper_left": {"panel": "ram", "detail": "detail"}})

    def test_custom_layout_visibility_uses_effective_default_slots(self) -> None:
        args = argparse.Namespace(layout="custom", show_io=False, custom_layout={})
        self.assertFalse(asmond.layout_uses_io(args))
        self.assertFalse(asmond.layout_uses_process(args, "hidden"))
        args.show_io = True
        self.assertTrue(asmond.layout_uses_io(args))
        args.show_io = False
        args.custom_layout = {"right_bottom": {"panel": "process", "detail": "detail"}}
        self.assertFalse(asmond.layout_uses_io(args))
        self.assertTrue(asmond.layout_uses_process(args, "hidden"))


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
