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
import asmond_metrics_memory
import asmond_process


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

    def test_read_network_bytes_ignores_virtual_and_duplicate_interfaces(self) -> None:
        text = """Name  Mtu   Network     Address            Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
lo0   16384 <Link#1>    00:00:00:00:00:00  1     0     100    1     0     200    0
en0   1500  <Link#4>    aa:bb:cc:dd:ee:ff  10    0     1000   20    0     2000   0
en0   1500  <Link#4>    aa:bb:cc:dd:ee:ff  10    0     9999   20    0     9999   0
utun0 1380  <Link#9>    00:00:00:00:00:00  1     0     300    1     0     400    0
"""

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, text.encode(), b"")

        self.assertEqual(asmond_metrics_memory.read_network_bytes(run=fake_run), (1000, 2000))

    def test_read_disk_counters_uses_ioreg_plist_statistics(self) -> None:
        payload = [
            {
                "IOObjectClass": "IOBlockStorageDriver",
                "Statistics": {
                    "Bytes (Read)": 4096,
                    "Bytes (Write)": 8192,
                },
            }
        ]

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, plistlib.dumps(payload), b"")

        self.assertEqual(asmond_metrics_memory.read_disk_counters(run=fake_run), (4096, 8192))

    def test_read_memory_stats_combines_vm_stat_pressure_and_swap(self) -> None:
        vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               100.
Pages active:                             200.
Pages speculative:                         50.
Pages wired down:                          80.
Pages purgeable:                           10.
File-backed pages:                         60.
Pages occupied by compressor:              40.
"""
        pressure = "System-wide memory free percentage: 73%\n"
        swap = "total = 2.00G  used = 512.00M  free = 1.50G  (encrypted)\n"

        def fake_run(command, **kwargs):
            if command[:3] == ["sysctl", "-n", "hw.memsize"]:
                return subprocess.CompletedProcess(command, 0, str(4096 * 1000).encode(), b"")
            if command == ["vm_stat"]:
                return subprocess.CompletedProcess(command, 0, vm_stat.encode(), b"")
            if command == ["memory_pressure"]:
                return subprocess.CompletedProcess(command, 0, pressure.encode(), b"")
            if command[:3] == ["sysctl", "-n", "vm.swapusage"]:
                return subprocess.CompletedProcess(command, 0, swap.encode(), b"")
            return subprocess.CompletedProcess(command, 1, b"", b"")

        stats = asmond_metrics_memory.read_memory_stats(run=fake_run)
        self.assertEqual(stats.total_bytes, 4096 * 1000)
        self.assertEqual(stats.free_bytes, 150 * 4096)
        self.assertEqual(stats.cached_bytes, 70 * 4096)
        self.assertEqual(stats.active_bytes, 200 * 4096)
        self.assertEqual(stats.wired_bytes, 80 * 4096)
        self.assertEqual(stats.compressed_bytes, 40 * 4096)
        self.assertEqual(stats.system_free_pct, 73.0)
        self.assertEqual(stats.swap_used_bytes, 512 * 1024**2)

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


class PowermetricsFixtureTests(unittest.TestCase):
    def test_apple_silicon_fixture_parses_structured_sample(self) -> None:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "apple_silicon_sample.plist"
        sample = asmond.sample_from_plist(plistlib.loads(fixture.read_bytes()), interval_s=0.5)

        self.assertAlmostEqual(sample.cpu_power_mw or 0.0, 500.0)
        self.assertAlmostEqual(sample.gpu_power_mw or 0.0, 200.0)
        self.assertEqual(sample.ane_power_mw, 0.0)
        self.assertAlmostEqual(sample.soc_power_mw or 0.0, 1250.0)
        self.assertAlmostEqual(sample.e_usage_pct or 0.0, 25.0)
        self.assertAlmostEqual(sample.p_usage_pct or 0.0, 50.0)
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 37.5)
        self.assertAlmostEqual(sample.gpu_usage_pct or 0.0, 10.0)
        self.assertAlmostEqual(sample.e_freq_mhz or 0.0, 1800.0)
        self.assertAlmostEqual(sample.p_freq_mhz or 0.0, 3200.0)
        self.assertAlmostEqual(sample.gpu_freq_mhz or 0.0, 600.0)
        self.assertAlmostEqual(sample.soc_temp_c or 0.0, 37.3)
        self.assertAlmostEqual(sample.temp_max_c or 0.0, 38.4)
        self.assertEqual(sample.thermal_pressure, "Nominal")
        self.assertEqual(
            [(core.label, round(core.usage_pct or 0.0)) for core in sample.cores],
            [("P6", 55), ("P7", 45), ("E0", 20), ("E1", 30)],
        )
        self.assertEqual(sample.memory_bandwidth_gbps, {"CPU": 1.5, "DRAM": 3.0})

    def test_intel_macmini_fixture_parses_supported_fields(self) -> None:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "intel_macmini_sample.plist"
        sample = asmond.sample_from_plist(plistlib.loads(fixture.read_bytes()), interval_s=1.0)

        self.assertEqual(sample.telemetry_source, "intel")
        self.assertAlmostEqual(sample.soc_power_mw or 0.0, 5417.14, places=2)
        self.assertAlmostEqual(sample.cpu_power_mw or 0.0, 2406.56, places=2)
        self.assertAlmostEqual(sample.gpu_power_mw or 0.0, 4.81177, places=5)
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 26.7445, places=4)
        self.assertAlmostEqual(sample.gpu_usage_pct or 0.0, 0.0680779, places=6)
        self.assertAlmostEqual(sample.p_freq_mhz or 0.0, 2236.85, places=2)
        self.assertAlmostEqual(sample.gpu_freq_mhz or 0.0, 750.0)
        self.assertEqual(sample.thermal_pressure, "Nominal")
        self.assertIsNone(sample.soc_temp_c)
        self.assertEqual([core.label for core in sample.cores], ["C0", "C1", "C2", "C3", "C4", "C5"])
        self.assertEqual(asmond.clock_panel_rows(sample)[:2], [("CPU", "2.24 GHz"), ("GPU", "750 MHz")])

    def test_temperature_fallback_ignores_limits_battery_and_adapter(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "soc_die_temperature": "309.15 K",
                "battery_temperature": "99 C",
                "adapter_temperature": "100 C",
                "soc_temp_limit": "110 C",
                "thermal_target_temperature": "85 C",
            }
        )
        self.assertAlmostEqual(sample.soc_temp_c or 0.0, 36.0)
        self.assertAlmostEqual(sample.temp_max_c or 0.0, 36.0)

    def test_throttle_reasons_are_limited_and_mark_sample_throttled(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "thermal_pressure": "Nominal",
                "cpu_throttle": 1,
                "gpu_limit": True,
                "ane_limit": "active",
                "extra_limit": 2,
                "ignored_limit": 0,
            }
        )
        self.assertTrue(sample.throttled)
        self.assertEqual(len(sample.throttle_reasons), 4)
        self.assertTrue(any("cpu throttle" in reason for reason in sample.throttle_reasons))
        self.assertFalse(any("ignored limit" in reason for reason in sample.throttle_reasons))

    def test_intel_processor_fields_parse_targeted_sample(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "thermal_pressure": "Moderate",
                "processor": {
                    "package_watts": 6.10,
                    "cpu_watts": 2.96,
                    "igpu_watts": 0.057,
                    "dram_watts": 0.738,
                    "freq_hz": 2_170_000_000,
                    "packages": [
                        {
                            "average_num_cores": 1.8,
                            "cores_active_ratio": 0.35,
                            "gpu_active_ratio": 0.12,
                            "cores": [
                                {"core": 0, "c_state_ratio": 0.80, "cpus": [{"cpu": 0, "freq_hz": 2_100_000_000}]},
                                {"core": 1, "c_state_ratio": 0.60, "cpus": [{"cpu": 1, "freq_hz": 2_240_000_000}]},
                            ],
                        }
                    ],
                },
                "GPU": [
                    {
                        "freq_hz": 750_000_000,
                        "misc_counters": {
                            "GPU Busy                      :": "18.5%",
                        }
                    }
                ],
            }
        )
        self.assertAlmostEqual(sample.soc_power_mw or 0.0, 6100.0)
        self.assertAlmostEqual(sample.cpu_power_mw or 0.0, 2960.0)
        self.assertAlmostEqual(sample.gpu_power_mw or 0.0, 57.0)
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 35.0)
        self.assertAlmostEqual(sample.gpu_usage_pct or 0.0, 18.5)
        self.assertAlmostEqual(sample.p_freq_mhz or 0.0, 2170.0)
        self.assertAlmostEqual(sample.gpu_freq_mhz or 0.0, 750.0)
        self.assertEqual([(core.label, round(core.usage_pct or 0.0)) for core in sample.cores], [("C0", 20), ("C1", 40)])
        self.assertEqual(sample.thermal_pressure, "Moderate")

    def test_intel_average_num_cores_can_estimate_cpu_usage(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "packages": [
                        {
                            "average_num_cores": 1.5,
                            "cpus": [{"cpu": 0}, {"cpu": 1}, {"cpu": 2}, {"cpu": 3}, {"cpu": 4}, {"cpu": 5}],
                        }
                    ]
                }
            }
        )
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 25.0)

    def test_generic_package_list_does_not_trigger_intel_core_rows(self) -> None:
        sample = asmond.sample_from_plist({"processor": {"packages": [{"cpus": [{"cpu": 0, "active_ratio": 0.5}]}]}})
        self.assertEqual(sample.cores, [])
        self.assertIsNone(sample.cpu_usage_pct)

    def test_apple_silicon_energy_fields_take_precedence_over_intel_watt_fields(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "combined_power": 2.0,
                    "package_watts": 9.9,
                    "cpu_energy": 250,
                    "cpu_watts": 9.9,
                    "gpu_energy": 100,
                    "igpu_watts": 9.9,
                }
            },
            interval_s=0.5,
        )
        self.assertEqual(sample.soc_power_mw, 2000.0)
        self.assertEqual(sample.cpu_power_mw, 500.0)
        self.assertEqual(sample.gpu_power_mw, 200.0)

    def test_intel_gpu_freq_prefers_live_gpu_block_over_p_states(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "package_watts": 5.0,
                    "packages": [{"gpu_active_ratio": 0.01}],
                },
                "GPU": [
                    {
                        "freq_hz": 750_000_000,
                        "p_states": [{"frequency": 1_100_000_000, "used_ratio": 0.0}],
                    }
                ],
            }
        )
        self.assertAlmostEqual(sample.gpu_freq_mhz or 0.0, 750.0)


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

    def test_usb_c_stats_reads_numbered_source_pdos(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerSrcPdoCount": 1,
                        "PortControllerSrcPdo1": 0x0006412C,
                    }
                ],
            }
        )
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.pdo_labels, ["20V 3A"])
        self.assertAlmostEqual(stats.active_port.max_power_w or 0.0, 60.0)
        self.assertAlmostEqual(stats.active_port.voltage_v or 0.0, 20.0)

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

    def test_soc_fallback_does_not_treat_component_power_as_total(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "cpu_power": 123,
                    "gpu_power": 50,
                }
            }
        )
        self.assertEqual(sample.cpu_power_mw, 123)
        self.assertEqual(sample.gpu_power_mw, 50)
        self.assertIsNone(sample.soc_power_mw)
        self.assertEqual(asmond.effective_total_power_mw(sample), 173)

    def test_structured_processor_combined_power_still_sets_total(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "combined_power": 5.0,
                    "cpu_power": 123,
                }
            }
        )
        self.assertEqual(sample.cpu_power_mw, 123)
        self.assertEqual(sample.soc_power_mw, 5000)

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

    def test_doctor_marks_intel_platform_as_experimental_warning(self) -> None:
        old_machine = asmond.platform.machine
        old_can_write = asmond.can_write_settings
        old_check = asmond.check_command
        old_needs_sudo = asmond.powermetrics_needs_sudo
        old_refresh = asmond.refresh_sudo_credentials
        try:
            asmond.platform.machine = lambda: "x86_64"
            asmond.can_write_settings = lambda: True
            asmond.check_command = lambda name: ("ok", f"/usr/bin/{name}")
            asmond.powermetrics_needs_sudo = lambda: True
            asmond.refresh_sudo_credentials = lambda prompt=False: (False, "sudo not cached")
            rows = asmond.diagnostic_rows(argparse.Namespace(mock=False, live=False, interval=1.0))
        finally:
            asmond.platform.machine = old_machine
            asmond.can_write_settings = old_can_write
            asmond.check_command = old_check
            asmond.powermetrics_needs_sudo = old_needs_sudo
            asmond.refresh_sudo_credentials = old_refresh

        platform_row = next(row for row in rows if row[0] == "platform")
        self.assertEqual(platform_row[1], "warn")
        self.assertIn("experimental", platform_row[2])
        self.assertEqual(asmond.diagnostics_exit_code(rows), 0)

    def test_powermetrics_command_uses_platform_specific_samplers(self) -> None:
        old_machine = asmond.platform.machine
        old_needs_sudo = asmond.powermetrics_needs_sudo
        try:
            asmond.powermetrics_needs_sudo = lambda: False
            asmond.platform.machine = lambda: "arm64"
            self.assertIn(asmond.POWER_SAMPLERS, asmond.powermetrics_command(1000, "1"))
            asmond.platform.machine = lambda: "x86_64"
            command = asmond.powermetrics_command(1000, "1")
            self.assertIn(asmond.INTEL_POWER_SAMPLERS, command)
            self.assertNotIn("ane_power", command)
            self.assertNotIn("battery", command)
        finally:
            asmond.platform.machine = old_machine
            asmond.powermetrics_needs_sudo = old_needs_sudo

    def test_mock_report_contains_anonymized_core_sections(self) -> None:
        args = argparse.Namespace(mock=True, live=False, interval=1.0)
        data = asmond.report_data(args)
        self.assertEqual(data["app"], "Asmond")
        self.assertIn("diagnostics", data)
        self.assertIn("snapshot", data)
        self.assertEqual(data["snapshot"]["usb_c"]["active_port"], "MagSafe")
        self.assertNotIn(str(Path.home()), data["settings_path"])

    def test_report_diagnostics_anonymize_user_local_command_paths(self) -> None:
        old_home = asmond.real_user_home
        old_check = asmond.check_command
        old_can_write = asmond.can_write_settings
        try:
            asmond.real_user_home = lambda: Path("/Users/alice")
            asmond.check_command = lambda name: ("ok", f"/Users/alice/bin/{name}")
            asmond.can_write_settings = lambda: True
            rows = asmond.diagnostic_rows_for_report(argparse.Namespace(mock=True, live=False, interval=1.0), None)
        finally:
            asmond.real_user_home = old_home
            asmond.check_command = old_check
            asmond.can_write_settings = old_can_write

        powermetrics_row = next(row for row in rows if row[0] == "powermetrics")
        self.assertEqual(powermetrics_row, ("powermetrics", "ok", "~/bin/powermetrics"))

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

    def test_source_status_includes_side_metric_warnings(self) -> None:
        args = argparse.Namespace(mock=False, show_io=False, layout="focus")
        text = asmond.source_status(
            args,
            asmond.MetricSample(),
            asmond.MemoryStats(total_bytes=1, available_bytes=1),
            asmond.BatteryStats(charge_pct=50.0),
            asmond.UsbCStats(),
            asmond.IoStats(),
            [],
            "hidden",
            ("vm:RuntimeError", "io:TimeoutError"),
        )
        self.assertIn("warn vm:RuntimeError,io:TimeoutError", text)

    def test_side_worker_reports_polling_errors(self) -> None:
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats
        updates: queue.Queue[asmond.SideMetricsUpdate] = queue.Queue()
        stop_event = threading.Event()

        def broken_memory():
            raise RuntimeError("boom")

        try:
            asmond.read_memory_stats = broken_memory
            asmond.read_charge_stats = lambda: (asmond.BatteryStats(), asmond.UsbCStats())
            worker = threading.Thread(
                target=asmond.side_metrics_worker,
                args=(updates, stop_event, asmond.SideMetricsPollState(), False),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 1.0
            warnings: list[str] = []
            while time.monotonic() < deadline and not warnings:
                try:
                    warnings.extend(updates.get(timeout=0.1).warnings)
                except queue.Empty:
                    pass
            self.assertIn("vm:RuntimeError", warnings)
        finally:
            stop_event.set()
            if "worker" in locals():
                worker.join(timeout=1.0)
            asmond.read_memory_stats = old_memory
            asmond.read_charge_stats = old_charge

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

    def test_live_report_timeout_returns_warning_sample(self) -> None:
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats

        def fake_run(command, **kwargs):
            if "powermetrics" not in command:
                return old_run(command, **kwargs)
            raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout"))

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda: (asmond.BatteryStats(), asmond.UsbCStats())
            args = argparse.Namespace(mock=False, live=True, interval=1.0)
            data = asmond.report_data(args)
            self.assertIn("timed out", data["snapshot"]["powermetrics"]["warning"])
            self.assertEqual(data["diagnostics"][-1]["name"], "powermetrics sample")
            self.assertEqual(data["diagnostics"][-1]["status"], "fail")
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure
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

    def test_probe_raw_help_says_values_are_not_anonymized(self) -> None:
        parser = asmond.build_parser()
        probe_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        help_text = probe_action.choices["probe"].format_help()
        self.assertIn("keys and values", help_text)
        self.assertIn("not anonymized", help_text)

    def test_mock_live_report_labels_demo_sample(self) -> None:
        args = argparse.Namespace(mock=True, live=True, interval=1.0)
        data = asmond.report_data(args)
        self.assertEqual(data["diagnostics"][-1], {"name": "mock sample", "status": "ok", "note": "generated demo snapshot"})

    def test_print_sample_uses_effective_total_power(self) -> None:
        sample = asmond.MetricSample(cpu_power_mw=100, gpu_power_mw=50)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asmond.print_sample(sample)
        self.assertIn("SoC/Total:    150 mW", output.getvalue())


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
    def test_clock_panel_rows_keep_apple_silicon_cluster_labels(self) -> None:
        sample = asmond.MetricSample(
            p_freq_mhz=3200.0,
            e_freq_mhz=1800.0,
            gpu_freq_mhz=600.0,
            raw_keys=12,
        )

        rows = asmond.clock_panel_rows(sample)
        labels = [label for label, _value in rows]

        self.assertEqual(rows[:3], [("P cores", "3.20 GHz"), ("E cores", "1.80 GHz"), ("GPU", "600 MHz")])
        self.assertNotIn("CPU", labels)
        self.assertNotIn("Cores", labels)
        self.assertNotIn("Temp avg", labels)
        self.assertNotIn("Temp max", labels)

    def test_clock_panel_rows_use_single_intel_cpu_clock(self) -> None:
        sample = asmond.MetricSample(
            telemetry_source="intel",
            p_freq_mhz=2300.0,
            gpu_freq_mhz=750.0,
            raw_keys=12,
            cores=[
                asmond.CoreMetric("C0", freq_mhz=2200.0),
                asmond.CoreMetric("C1", freq_mhz=2400.0),
            ],
        )

        rows = asmond.clock_panel_rows(sample)
        labels = [label for label, _value in rows]

        self.assertEqual(rows[:2], [("CPU", "2.30 GHz"), ("GPU", "750 MHz")])
        self.assertNotIn("P cores", labels)
        self.assertNotIn("E cores", labels)
        self.assertNotIn("Cores", labels)

    def test_clock_panel_rows_use_intel_label_without_core_rows(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "package_watts": 6.1,
                    "cpu_watts": 2.9,
                    "freq_hz": 2_170_000_000,
                }
            }
        )

        rows = asmond.clock_panel_rows(sample)
        labels = [label for label, _value in rows]

        self.assertEqual(rows[0], ("CPU", "2.17 GHz"))
        self.assertNotIn("P cores", labels)
        self.assertNotIn("E cores", labels)

    def test_power_graph_live_label_hides_unsupported_ane(self) -> None:
        history = asmond.History(8)
        sample = asmond.MetricSample(soc_power_mw=1200.0, cpu_power_mw=800.0, gpu_power_mw=200.0)

        label = asmond.power_graph_live_label(sample, history)

        self.assertEqual(label, "SoC 1.20 W  CPU 800 mW  GPU 200 mW")
        self.assertNotIn("ANE", label)

    def test_power_graph_live_label_keeps_real_zero_ane(self) -> None:
        history = asmond.History(8)
        sample = asmond.MetricSample(soc_power_mw=1200.0, cpu_power_mw=800.0, gpu_power_mw=200.0, ane_power_mw=0.0)

        label = asmond.power_graph_live_label(sample, history)

        self.assertIn("ANE/NPU 0 mW", label)

    def test_power_panel_rows_do_not_include_battery(self) -> None:
        history = asmond.History(8)
        sample = asmond.MetricSample(soc_power_mw=1200.0, cpu_power_mw=800.0, gpu_power_mw=200.0, ane_power_mw=0.0)
        rows = [mode for mode in asmond.supported_power_modes(sample, history)]

        self.assertEqual(rows, ["soc", "cpu", "gpu", "ane"])

    def test_ram_detail_rows_prioritize_swap(self) -> None:
        memory = asmond.MemoryStats(
            total_bytes=24 * 1024**3,
            free_bytes=512 * 1024**2,
            cached_bytes=4 * 1024**3,
            available_bytes=6 * 1024**3,
            active_bytes=8 * 1024**3,
            wired_bytes=3 * 1024**3,
            compressed_bytes=2 * 1024**3,
            swap_total_bytes=4 * 1024**3,
            swap_used_bytes=1024**3,
        )

        labels = [label for label, _text, _value, _good in asmond.memory_detail_value_rows(memory)]

        self.assertEqual(labels[:4], ["Phys", "Swap", "Free", "Cache"])

    def test_full_layout_geometry_matches_existing_dashboard_grid(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden")
        self.assertEqual(layout.slot("top").rect, asmond.Rect(2, 0, 11, 181))
        self.assertEqual(layout.slot("upper_left").rect, asmond.Rect(13, 0, 7, 90))
        self.assertEqual(layout.slot("upper_right").rect, asmond.Rect(13, 90, 7, 91))
        self.assertEqual(layout.slot("lower_left").rect, asmond.Rect(20, 0, 40, 90))
        self.assertEqual(layout.slot("right_top").rect, asmond.Rect(20, 90, 6, 91))
        self.assertEqual(layout.slot("right_middle").rect, asmond.Rect(26, 90, 26, 91))
        self.assertEqual(layout.slot("right_lower").rect, asmond.Rect(52, 90, 8, 91))
        self.assertIsNone(layout.slot("right_bottom"))
        self.assertTrue(layout.left_io_panel)

    def test_full_layout_process_right_reuses_ram_slot(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", False, "right")
        process_slot = layout.slot("right_middle")
        self.assertIsNotNone(process_slot)
        self.assertEqual(process_slot.panel_id, "process")
        self.assertEqual(process_slot.rect, asmond.Rect(26, 90, 26, 91))

    def test_full_layout_delays_charge_until_ram_has_room(self) -> None:
        compact_height = asmond.dashboard_layout(37, 120, "full", False, "hidden")
        self.assertIsNone(compact_height.slot("right_lower"))
        self.assertGreaterEqual(compact_height.slot("right_middle").rect.h, asmond.MIN_RAM_PANEL_H)

        enough_height = asmond.dashboard_layout(41, 120, "full", False, "hidden")
        self.assertIsNotNone(enough_height.slot("right_lower"))
        self.assertGreaterEqual(enough_height.slot("right_middle").rect.h, asmond.MIN_RAM_PANEL_H)

    def test_focus_layout_keeps_charge_area_below_top_panels(self) -> None:
        layout = asmond.dashboard_layout(40, 120, "focus", False, "hidden")
        self.assertEqual(layout.slot("top").rect, asmond.Rect(2, 0, 10, 120))
        self.assertEqual(layout.slot("upper_left").rect, asmond.Rect(12, 0, 7, 60))
        self.assertEqual(layout.slot("upper_right").rect, asmond.Rect(12, 60, 7, 60))
        self.assertEqual(layout.slot("main").rect, asmond.Rect(19, 0, 20, 120))

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

    def test_process_gpu_exception_path_backs_off(self) -> None:
        calls = 0
        state = asmond.ProcessGpuProbeState()

        def fake_run(command, **kwargs):
            nonlocal calls
            calls += 1
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

        values = asmond_process.read_process_gpu_pcts(
            probe_state=state,
            run=fake_run,
            refresh_sudo=lambda prompt=False: (True, ""),
        )
        self.assertEqual(values, {})
        self.assertFalse(state.can_probe())
        self.assertEqual(calls, 2)

    def test_read_processes_can_prefer_full_command_for_validation(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if "command=" in command[-1]:
                text = "123 alice 1 02:03 12.5 3.4 45678 /usr/bin/python3 worker.py --job a\n"
            else:
                text = "123 alice 1 02:03 12.5 3.4 45678 python3\n"
            return subprocess.CompletedProcess(command, 0, text.encode(), b"")

        process = asmond_process.read_processes(run=fake_run, full_command=True)[0]
        self.assertIn("command=", calls[0][-1])
        self.assertEqual(process.command, "python3 worker.py --job a")
        self.assertEqual(process.full_command, "/usr/bin/python3 worker.py --job a")

    def test_pending_kill_detects_changed_full_command_arguments(self) -> None:
        original = asmond.ProcessInfo(123, 1.0, 1.0, 100, "python3", 1, "02:03", "/usr/bin/python3 a.py", "alice")
        changed = asmond.ProcessInfo(123, 1.0, 1.0, 100, "python3", 1, "02:04", "/usr/bin/python3 b.py", "alice")
        pending = asmond.PendingKill.from_process(original, time.monotonic() + 1.0)
        self.assertFalse(pending.matches(changed))

    def test_merge_process_gpu_pcts(self) -> None:
        processes = [asmond.ProcessInfo(123, 1.0, 2.0, 4096, "a"), asmond.ProcessInfo(456, 3.0, 4.0, 8192, "b")]
        asmond.merge_process_gpu_pcts(processes, {456: 33.0})
        self.assertIsNone(processes[0].gpu_pct)
        self.assertEqual(processes[1].gpu_pct, 33.0)


if __name__ == "__main__":
    unittest.main()
