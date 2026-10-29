import argparse
import contextlib
import io
import importlib.util
import gzip
import hashlib
import json
import shutil
import sys
import os
import plistlib
import queue
import subprocess
import tempfile
import threading
import tarfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import asmond
import asmond_cpu
import asmond_layout
import asmond_ports
import asmond_metrics_memory
import asmond_process
import asmond_settings
import asmond_smc
import asmond_system


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


class AppleSMCTemperatureTests(unittest.TestCase):
    def test_verified_base_m4_mapping_requires_exact_topology(self) -> None:
        groups = asmond_smc.temperature_key_groups("Apple M4", 4, 6)

        self.assertEqual(len(groups.cpu), 10)
        self.assertEqual(groups.cpu_labels, ("E0", "E1", "E2", "E3", "E4", "E5", "P6", "P7", "P8", "P9"))
        self.assertEqual(len(groups.gpu), 8)
        self.assertIn("Te0X", groups.cpu)
        self.assertIn("Tg0k", groups.gpu)
        self.assertEqual(
            dict(groups.auxiliary),
            {
                "airport_proximity_c": "TW0P",
                "power_supply_proximity_c": "TPSP",
                "trackpad_c": "Ts0P",
                "trackpad_actuator_c": "Ts1P",
            },
        )
        self.assertEqual(asmond_smc.temperature_key_groups("Apple M4 Pro", 8, 4), asmond_smc.TemperatureKeyGroups())
        self.assertEqual(asmond_smc.temperature_key_groups("Apple M4", 8, 4), asmond_smc.TemperatureKeyGroups())

    def test_temperature_summary_requires_complete_real_group(self) -> None:
        self.assertEqual(asmond_smc.summarize_temperatures([40.0, 44.0], 2), (42.0, 44.0))
        self.assertEqual(asmond_smc.summarize_temperatures([40.0, None], 2), (None, None))
        self.assertEqual(asmond_smc.summarize_temperatures([40.0, 0.0], 2), (None, None))
        self.assertEqual(asmond_smc.summarize_temperatures([], 0), (None, None))

    def test_smc_temperatures_take_priority_over_generic_hid_fallback(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(
                    41.5,
                    45.5,
                    38.2,
                    42.0,
                    airport_proximity_c=31.6,
                    power_supply_proximity_c=31.5,
                    trackpad_c=27.0,
                    trackpad_actuator_c=25.8,
                    cpu_max_label="E5",
                )

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(
                    average_c=80.0,
                    maximum_c=90.0,
                    pmu_average_c=31.8,
                    pmu_maximum_c=35.3,
                )

            def read(self):
                raise AssertionError("generic HID fallback must not replace verified SMC groups")

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample(temp_max_c=44.0, temperature_source="IOHID")
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertEqual(sample.cpu_temp_avg_c, 41.5)
        self.assertEqual(sample.cpu_temp_max_c, 45.5)
        self.assertEqual(sample.cpu_temp_max_label, "E5")
        self.assertEqual(sample.gpu_temp_avg_c, 38.2)
        self.assertEqual(sample.gpu_temp_max_c, 42.0)
        self.assertEqual(sample.pmu_temp_avg_c, 31.8)
        self.assertEqual(sample.pmu_temp_max_c, 35.3)
        self.assertEqual(sample.airport_temp_c, 31.6)
        self.assertEqual(sample.power_supply_temp_c, 31.5)
        self.assertEqual(sample.trackpad_temp_c, 27.0)
        self.assertEqual(sample.trackpad_actuator_temp_c, 25.8)
        self.assertEqual(sample.temperature_source, "AppleSMC + IOHID")
        self.assertEqual(sample.soc_temp_c, 41.5)
        self.assertEqual(sample.temp_max_c, 45.5)
        self.assertEqual(sample.temp_max_source, "CPU@E5")
        self.assertEqual(asmond.thermal_hotspot(sample), (45.5, "CPU@E5"))

        report = asmond.sample_to_report_dict(sample)
        assert report is not None
        self.assertEqual(report["pmu_temp_avg"], "31.8°C")
        self.assertEqual(report["pmu_temp_max"], "35.3°C")
        self.assertEqual(report["airport_temp"], "31.6°C")
        self.assertEqual(report["power_supply_temp"], "31.5°C")
        self.assertEqual(report["trackpad_temp"], "27.0°C")
        self.assertEqual(report["trackpad_actuator_temp"], "25.8°C")
        self.assertEqual(report["cpu_temp_max_source"], "E5")
        self.assertEqual(report["temp_max_source"], "CPU@E5")

    def test_pmu_hotspot_keeps_iohid_in_complete_smc_provenance(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(
                    cpu_avg_c=40.0,
                    cpu_max_c=41.0,
                    cpu_max_label="P6",
                )

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(
                    pmu_average_c=42.0,
                    pmu_maximum_c=43.0,
                )

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample()
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertEqual(sample.temp_max_c, 43.0)
        self.assertEqual(sample.temp_max_source, "PMU")
        self.assertEqual(sample.temperature_source, "AppleSMC + IOHID")

    def test_partial_smc_gpu_still_drives_hotspot_alert_and_provenance(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(
                    gpu_avg_c=80.0,
                    gpu_max_c=100.0,
                    airport_proximity_c=31.0,
                )

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(
                    average_c=30.0,
                    maximum_c=48.0,
                    pmu_average_c=31.0,
                    pmu_maximum_c=35.0,
                )

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample()
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertIsNone(sample.cpu_temp_avg_c)
        self.assertEqual(sample.gpu_temp_max_c, 100.0)
        self.assertEqual(sample.temp_max_c, 100.0)
        self.assertEqual(sample.temp_max_source, "GPU")
        self.assertEqual(sample.temperature_source, "AppleSMC + IOHID")
        self.assertEqual(asmond.thermal_hotspot(sample), (100.0, "GPU"))
        self.assertIn("TEMP 100°C", asmond.build_alerts(sample, asmond.MemoryStats(), asmond.BatteryStats()))
        self.assertEqual(asmond.sample_to_report_dict(sample)["temperature_source"], "AppleSMC + IOHID")

    def test_partial_smc_aux_sensor_does_not_hide_higher_hid_hotspot(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(airport_proximity_c=31.0)

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(average_c=30.0, maximum_c=48.0)

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample()
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertEqual(sample.temp_max_c, 48.0)
        self.assertEqual(sample.temp_max_source, "IOHID")
        self.assertEqual(asmond.thermal_hotspot(sample), (48.0, "IOHID"))

    def test_pure_hid_fallback_keeps_hotspot_alert_and_source_consistent(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample()

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(
                    average_c=40.0,
                    maximum_c=90.0,
                    pmu_average_c=31.0,
                    pmu_maximum_c=35.0,
                )

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample()
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertEqual(sample.soc_temp_c, 40.0)
        self.assertEqual(sample.temp_max_c, 90.0)
        self.assertEqual(sample.temp_max_source, "IOHID")
        self.assertEqual(sample.temperature_source, "IOHID")
        self.assertEqual(asmond.thermal_hotspot(sample), (90.0, "IOHID"))
        self.assertIn("TEMP 90°C", asmond.build_alerts(sample, asmond.MemoryStats(), asmond.BatteryStats()))

    def test_probe_prints_gpu_temperature_and_source_when_cpu_group_is_missing(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(gpu_avg_c=80.0, gpu_max_c=100.0)

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(average_c=30.0, maximum_c=110.0)

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample()
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid
        self.assertEqual(asmond.thermal_hotspot(sample), (110.0, "IOHID"))
        self.assertIn("TEMP 110°C", asmond.build_alerts(sample, asmond.MemoryStats(), asmond.BatteryStats()))
        self.assertEqual(asmond.sample_to_report_dict(sample)["temp_max_source"], "IOHID")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            asmond.print_sample(sample)

        text = output.getvalue()
        self.assertIn("CPU temp avg: n/a", text)
        self.assertIn("GPU temp avg: 80.0°C", text)
        self.assertIn("GPU temp max: 100.0°C", text)
        self.assertIn("Temp max:     110.0°C", text)
        self.assertIn("Max source:   IOHID", text)
        self.assertIn("Temp source:  AppleSMC + IOHID", text)

    def test_partial_smc_keeps_powermetrics_maximum_visible_without_hid(self) -> None:
        class FakeSMC:
            def read(self):
                return asmond_smc.SMCTemperatureSample(airport_proximity_c=31.0)

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample()

        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = FakeSMC()
            asmond.HID_TEMPS = FakeHID()
            sample = asmond.MetricSample(soc_temp_c=40.0, temp_max_c=90.0)
            asmond.apply_hid_temperatures(sample)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertEqual(sample.temperature_source, "AppleSMC + powermetrics")
        self.assertEqual(sample.temp_max_c, 90.0)
        self.assertEqual(sample.temp_max_source, "powermetrics")
        self.assertEqual(asmond.thermal_hotspot(sample), (90.0, "powermetrics"))
        self.assertIn("TEMP 90°C", asmond.build_alerts(sample, asmond.MemoryStats(), asmond.BatteryStats()))

    def test_smc_reader_keeps_the_label_of_the_hottest_cpu_core(self) -> None:
        reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
        reader.available = True
        reader.groups = asmond_smc.temperature_key_groups("Apple M4", 4, 6)
        reader._lock = threading.Lock()
        values = {key: 40.0 + index for index, key in enumerate(reader.groups.cpu)}
        values.update({key: 35.0 for key in reader.groups.gpu})
        values.update({key: 30.0 for _name, key in reader.groups.auxiliary})
        reader._read_value = values.get

        sample = reader.read()

        self.assertEqual(sample.cpu_max_c, 49.0)
        self.assertEqual(sample.cpu_max_label, "P9")

    def test_aux_temperature_cache_limits_reads_at_fast_sample_intervals(self) -> None:
        class FakeSMC:
            calls = 0

            def read(self):
                self.calls += 1
                return asmond_smc.SMCTemperatureSample(cpu_avg_c=40.0 + self.calls)

        class FakeHID:
            calls = 0

            def read_metrics(self):
                self.calls += 1
                return asmond.HIDTemperatureSample(pmu_average_c=30.0 + self.calls)

        fake_smc = FakeSMC()
        fake_hid = FakeHID()
        cache = asmond.AuxiliaryTemperatureCache()
        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = fake_smc
            asmond.HID_TEMPS = fake_hid
            cache.refresh_async()
            first_thread = cache.thread
            self.assertIsNotNone(first_thread)
            assert first_thread is not None
            first_thread.join(timeout=1.0)
            self.assertFalse(first_thread.is_alive())

            first = cache.snapshot(now=cache.refreshed_at)
            cached = cache.snapshot(now=cache.refreshed_at + 0.1)
            with cache.lock:
                cache.refreshed_at -= asmond.AUX_TEMPERATURE_REFRESH_INTERVAL
            cache.snapshot()
            second_thread = cache.thread
            self.assertIsNotNone(second_thread)
            assert second_thread is not None
            second_thread.join(timeout=1.0)
            self.assertFalse(second_thread.is_alive())
            refreshed = cache.snapshot(now=cache.refreshed_at)
        finally:
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        self.assertIs(first[0], cached[0])
        self.assertIs(first[1], cached[1])
        self.assertIsNot(first[0], refreshed[0])
        self.assertIsNot(first[1], refreshed[1])
        self.assertEqual((fake_smc.calls, fake_hid.calls), (2, 2))

    def test_aux_temperature_cache_snapshot_does_not_wait_for_slow_reader(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowSMC:
            def read(self):
                started.set()
                release.wait(timeout=1.0)
                return asmond_smc.SMCTemperatureSample(cpu_avg_c=42.0)

        class FakeHID:
            def read_metrics(self):
                return asmond.HIDTemperatureSample(pmu_average_c=31.0)

        cache = asmond.AuxiliaryTemperatureCache()
        old_smc = asmond.SMC_TEMPS
        old_hid = asmond.HID_TEMPS
        try:
            asmond.SMC_TEMPS = SlowSMC()
            asmond.HID_TEMPS = FakeHID()
            cache.refresh_async()
            self.assertTrue(started.wait(timeout=1.0))
            began = time.monotonic()
            smc, hid = cache.snapshot()
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 0.1)
            self.assertIsNone(smc.cpu_avg_c)
            self.assertIsNone(hid.pmu_average_c)
        finally:
            release.set()
            if cache.thread is not None:
                cache.thread.join(timeout=1.0)
            asmond.SMC_TEMPS = old_smc
            asmond.HID_TEMPS = old_hid

        refreshed_smc, refreshed_hid = cache.snapshot(now=cache.refreshed_at)
        self.assertEqual(refreshed_smc.cpu_avg_c, 42.0)
        self.assertEqual(refreshed_hid.pmu_average_c, 31.0)

class BatteryParsingTests(unittest.TestCase):
    def test_normalize_battery_temp_hundredths_celsius(self) -> None:
        self.assertAlmostEqual(asmond.normalize_battery_temp_c(2970), 29.7)

    def test_normalize_battery_temp_tenths_celsius(self) -> None:
        self.assertAlmostEqual(asmond.normalize_battery_temp_c(202), 20.2)

    def test_hid_battery_temperature_averages_gas_gauge_sensors(self) -> None:
        temperature = asmond.battery_temperature_from_hid_readings(
            [
                ("PMU tdie8", 36.7),
                ("gas gauge battery", 27.7),
                ("Gas Gauge Battery 2", 28.3),
            ]
        )
        self.assertAlmostEqual(temperature or 0.0, 28.0)

    def test_read_battery_items_uses_ioreg_plist(self) -> None:
        old_run = asmond.subprocess.run
        expected_command = [asmond.IOREG_PATH, "-a", "-r", "-c", "AppleSmartBattery"]
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

    def test_direct_percentage_capacity_remains_supported(self) -> None:
        stats = asmond.battery_stats_from_item(
            {"CurrentCapacity": 71, "AppleRawMaxCapacity": 4800, "DesignCapacity": 5000}
        )
        self.assertEqual(stats.charge_pct, 71.0)
        self.assertEqual(stats.health_pct, 96.0)

    def test_intel_mah_capacity_and_adapter_only_fixture(self) -> None:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "intel_macbookair_battery_sample.plist"
        payload = plistlib.loads(fixture.read_bytes())
        battery = payload[0]

        stats = asmond.battery_stats_from_item(battery)
        charge_input = asmond.usb_c_stats_from_item(battery)

        self.assertEqual(stats.charge_pct, 99.0)
        self.assertAlmostEqual(stats.health_pct or 0.0, 84.71, places=2)
        self.assertEqual(stats.max_capacity, 6057)
        self.assertEqual(stats.cycle_count, 346)
        self.assertEqual(charge_input.input_kind, "ac")
        self.assertIsNone(charge_input.active_port)
        self.assertTrue(asmond.charge_input_active(charge_input))
        self.assertAlmostEqual(charge_input.adapter_voltage_v or 0.0, 15.0)
        self.assertAlmostEqual(charge_input.adapter_current_a or 0.0, 3.05)
        report = asmond.usb_to_report_dict(charge_input)
        self.assertEqual(report["input_type"], "AC Input")
        self.assertEqual(report["active_port"], "AC Input")
        self.assertEqual(report["voltage"], "15.0 V")

    def test_intel_mah_capacity_falls_back_to_ratio_without_state_of_charge(self) -> None:
        stats = asmond.battery_stats_from_item(
            {"CurrentCapacity": 3000, "AppleRawCurrentCapacity": 3000, "AppleRawMaxCapacity": 6000}
        )
        self.assertEqual(stats.charge_pct, 50.0)

    def test_unplugged_intel_battery_ignores_stale_charging_flag(self) -> None:
        stats = asmond.battery_stats_from_item(
            {
                "ExternalConnected": False,
                "IsCharging": True,
                "InstantAmperage": -1681,
                "Voltage": 7983,
                "CurrentCapacity": 1522,
                "AppleRawMaxCapacity": 5925,
            }
        )

        self.assertFalse(stats.charging)
        self.assertLess(stats.power_mw or 0.0, 0.0)
        self.assertEqual(asmond.battery_state_text(stats), "discharging")

    def test_intel_charging_prefers_instant_amperage_over_stale_average(self) -> None:
        stats = asmond.battery_stats_from_item(
            {
                "ExternalConnected": True,
                "IsCharging": True,
                "InstantAmperage": 1547,
                "Amperage": -137,
                "Voltage": 7743,
            }
        )

        self.assertTrue(stats.charging)
        self.assertAlmostEqual(stats.power_mw or 0.0, 11_978.421)
        self.assertEqual(asmond.battery_state_text(stats), "charging")

    def test_modern_battery_health_prefers_nominal_capacity(self) -> None:
        stats = asmond.battery_stats_from_item(
            {
                "CurrentCapacity": 71,
                "AppleRawMaxCapacity": 4800,
                "DesignCapacity": 5000,
                "BatteryData": {"NominalChargeCapacity": 4500},
            }
        )
        self.assertEqual(stats.max_capacity, 4500)
        self.assertEqual(stats.health_pct, 90.0)

    def test_macos_27_nested_health_and_hid_temperature_fallback(self) -> None:
        class FakeHidTemperatureReader:
            def __init__(self) -> None:
                self.calls = 0

            def read_battery_temperature(self) -> float:
                self.calls += 1
                return 27.9

        battery = {
            "CurrentCapacity": 38,
            "CycleCount": 66,
            "BatteryData": {
                "DesignCapacity": 4629,
                "FullChargeCapacity": 4299,
                "NominalChargeCapacity": 4426,
            },
        }
        fake_hid = FakeHidTemperatureReader()
        old_read_battery_items = asmond.read_battery_items
        old_read_hpm_port_items = asmond.read_hpm_port_items
        old_hid_temps = asmond.HID_TEMPS
        try:
            asmond.read_battery_items = lambda: battery
            asmond.read_hpm_port_items = lambda: []
            asmond.HID_TEMPS = fake_hid
            stats, _ = asmond.read_charge_stats()
        finally:
            asmond.read_battery_items = old_read_battery_items
            asmond.read_hpm_port_items = old_read_hpm_port_items
            asmond.HID_TEMPS = old_hid_temps

        self.assertEqual(stats.charge_pct, 38.0)
        self.assertAlmostEqual(stats.health_pct or 0.0, 4426 / 4629 * 100.0)
        self.assertEqual(stats.temperature_c, 27.9)
        self.assertEqual(fake_hid.calls, 1)

    def test_existing_ioreg_battery_temperature_stays_preferred(self) -> None:
        class UnexpectedHidTemperatureReader:
            def read_battery_temperature(self) -> float:
                raise AssertionError("HID fallback must not replace an IOKit battery temperature")

        old_read_battery_items = asmond.read_battery_items
        old_read_hpm_port_items = asmond.read_hpm_port_items
        old_hid_temps = asmond.HID_TEMPS
        try:
            asmond.read_battery_items = lambda: {"CurrentCapacity": 71, "Temperature": 2970}
            asmond.read_hpm_port_items = lambda: []
            asmond.HID_TEMPS = UnexpectedHidTemperatureReader()
            stats, _ = asmond.read_charge_stats()
        finally:
            asmond.read_battery_items = old_read_battery_items
            asmond.read_hpm_port_items = old_read_hpm_port_items
            asmond.HID_TEMPS = old_hid_temps

        self.assertEqual(stats.temperature_c, 29.7)

    def test_charge_reader_keeps_hpm_cable_telemetry_without_a_battery(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = 1 | (1 << 5)
        old_read_battery_items = asmond.read_battery_items
        old_read_hpm_port_items = asmond.read_hpm_port_items
        old_read_cable_identity_items = asmond.read_cable_identity_items
        old_read_usb_port_items = asmond.read_usb_port_items
        old_read_usb_device_items = asmond.read_usb_device_items
        try:
            asmond.read_battery_items = lambda: None
            asmond.read_hpm_port_items = lambda: [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortTypeDescription": "USB-C",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": True,
                    "TransportsActive": ["CC", "USB3", "DisplayPort"],
                }
            ]
            asmond.read_cable_identity_items = lambda: [
                {
                    "ParentPortType": 2,
                    "ParentPortNumber": 1,
                    "Metadata": {
                        "VDOs": [
                            id_header.to_bytes(4, "little"),
                            bytes(4),
                            bytes(4),
                            cable_vdo.to_bytes(4, "little"),
                        ]
                    },
                }
            ]
            asmond.read_usb_port_items = lambda: [
                {
                    "UsbIOPort": "IOService:/machine/Port-USB-C@1",
                    "locationID": 0x00200000,
                }
            ]
            asmond.read_usb_device_items = lambda: [
                {"locationID": 0x00200000, "UsbLinkSpeed": 10_000_000_000}
            ]
            battery, usb_c = asmond.read_charge_stats()
        finally:
            asmond.read_battery_items = old_read_battery_items
            asmond.read_hpm_port_items = old_read_hpm_port_items
            asmond.read_cable_identity_items = old_read_cable_identity_items
            asmond.read_usb_port_items = old_read_usb_port_items
            asmond.read_usb_device_items = old_read_usb_device_items

        self.assertFalse(asmond.battery_supported(battery))
        self.assertTrue(asmond.cable_panel_supported(usb_c))
        self.assertIsNotNone(usb_c.ports[0].cable_info)
        self.assertEqual(usb_c.ports[0].data_link_speed_bps, 10_000_000_000)

    def test_charge_reader_skips_emarker_query_without_connected_usb_c_cable(self) -> None:
        old_read_battery_items = asmond.read_battery_items
        old_read_hpm_port_items = asmond.read_hpm_port_items
        old_read_cable_identity_items = asmond.read_cable_identity_items
        try:
            asmond.read_battery_items = lambda: None
            asmond.read_hpm_port_items = lambda: [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": False,
                    "TransportsActive": ["USB3", "DisplayPort"],
                }
            ]
            asmond.read_cable_identity_items = lambda: self.fail("disconnected ports must not query E-markers")
            _, usb_c = asmond.read_charge_stats()
        finally:
            asmond.read_battery_items = old_read_battery_items
            asmond.read_hpm_port_items = old_read_hpm_port_items
            asmond.read_cable_identity_items = old_read_cable_identity_items

        self.assertIsNone(usb_c.ports[0].cable_info)

    def test_emarker_cache_refreshes_on_interval_and_connection_change(self) -> None:
        active = [{"PortType": 2, "PortNumber": 1, "ConnectionActive": True}]
        disconnected = [{"PortType": 2, "PortNumber": 1, "ConnectionActive": False}]
        cache = asmond.CableIdentityCache()
        calls = 0
        old_read = asmond.read_cable_identity_items

        def fake_read():
            nonlocal calls
            calls += 1
            return [{"read": calls}]

        try:
            asmond.read_cable_identity_items = fake_read
            self.assertEqual(asmond.cable_identity_items_for_hpm(active, cache, now=10.0), [{"read": 1}])
            self.assertEqual(asmond.cable_identity_items_for_hpm(active, cache, now=12.0), [{"read": 1}])
            self.assertEqual(asmond.cable_identity_items_for_hpm(active, cache, now=15.0), [{"read": 2}])
            self.assertEqual(asmond.cable_identity_items_for_hpm(disconnected, cache, now=16.0), [])
            self.assertEqual(asmond.cable_identity_items_for_hpm(active, cache, now=16.5), [{"read": 3}])
        finally:
            asmond.read_cable_identity_items = old_read

        self.assertEqual(calls, 3)

    def test_qmax_is_not_used_as_full_charge_capacity(self) -> None:
        stats = asmond.battery_stats_from_item(
            {
                "CurrentCapacity": 3000,
                "DesignCapacity": 5000,
                "BatteryData": {"Qmax": [4700, 4800]},
            }
        )
        self.assertIsNone(stats.max_capacity)
        self.assertIsNone(stats.health_pct)

    def test_unplugged_legacy_battery_does_not_claim_usb_c_support(self) -> None:
        charge_input = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": False,
                "IsCharging": False,
                "AdapterDetails": {"AdapterID": 0, "AdapterVoltage": 0, "Current": 0, "Watts": 0},
            }
        )
        self.assertFalse(asmond.usb_c_supported(charge_input))
        self.assertEqual(asmond.usb_c_state_text(charge_input), "unsupported")
        self.assertEqual(asmond.usb_to_report_dict(charge_input)["power"], "n/a")


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

    def test_io_failures_raise_instead_of_becoming_zero_counters(self) -> None:
        def failed_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"denied")

        with self.assertRaises(RuntimeError):
            asmond_metrics_memory.read_disk_counters(run=failed_run)
        with self.assertRaises(RuntimeError):
            asmond_metrics_memory.read_network_bytes(run=failed_run)

    def test_io_recovery_uses_last_valid_baseline(self) -> None:
        previous = asmond.IoSnapshot(timestamp=10.0, disk_read_bytes=1_000_000)
        recovered = asmond.IoSnapshot(timestamp=12.0, disk_read_bytes=1_001_000)
        stats = asmond.io_stats_from_snapshots(previous, recovered)
        self.assertEqual(stats.disk_read_bps, 500.0)


class SystemMetricsTests(unittest.TestCase):
    def test_cpu_usage_from_mach_snapshots_uses_user_system_and_nice(self) -> None:
        previous = asmond_cpu.CpuLoadSnapshot((100, 200, 300, 400))
        current = asmond_cpu.CpuLoadSnapshot((120, 230, 380, 410))
        self.assertAlmostEqual(asmond_cpu.cpu_usage_from_snapshots(previous, current) or 0.0, 42.85714, places=4)

    def test_cpu_usage_from_mach_snapshots_rejects_empty_or_wrapped_deltas(self) -> None:
        previous = asmond_cpu.CpuLoadSnapshot((100, 200, 300, 400))
        self.assertIsNone(asmond_cpu.cpu_usage_from_snapshots(previous, previous))
        self.assertIsNone(asmond_cpu.cpu_usage_from_snapshots(previous, asmond_cpu.CpuLoadSnapshot((99, 200, 300, 400))))

    def test_system_cpu_usage_overrides_only_intel_global_average(self) -> None:
        intel = asmond.MetricSample(telemetry_source="intel", cpu_usage_pct=90.0)
        apple = asmond.MetricSample(telemetry_source="apple", cpu_usage_pct=90.0)
        self.assertTrue(asmond.apply_system_cpu_usage(intel, 25.0))
        self.assertFalse(asmond.apply_system_cpu_usage(apple, 25.0))
        self.assertEqual(intel.cpu_usage_pct, 25.0)
        self.assertEqual(intel.cpu_usage_source, "mach")
        self.assertEqual(apple.cpu_usage_pct, 90.0)

    def test_history_latest_cpu_usage_can_follow_side_override(self) -> None:
        history = asmond.History(4)
        sample = asmond.MetricSample(telemetry_source="intel", cpu_usage_pct=90.0)
        history.add(sample)
        self.assertEqual(list(history.cpu_usage), [90.0])
        self.assertTrue(asmond.apply_system_cpu_usage(sample, 25.0))
        history.update_latest_cpu_usage(asmond.current_cpu_usage(sample))
        self.assertEqual(list(history.cpu_usage), [25.0])

    def test_side_poll_state_tracks_sampler_interval_and_revision(self) -> None:
        state = asmond.SideMetricsPollState()
        self.assertEqual(state.snapshot(), (False, False, 1.0, 0))
        state.update(True, False, 10.0)
        self.assertEqual(state.snapshot(), (True, False, 10.0, 1))
        state.update(True, False, 10.0)
        self.assertEqual(state.snapshot(), (True, False, 10.0, 1))
        state.update(True, True, 0.01)
        self.assertEqual(state.snapshot(), (True, True, 0.1, 2))

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
            if command[:3] == [asmond_system.SYSCTL_PATH, "-n", "hw.memsize"]:
                return subprocess.CompletedProcess(command, 0, str(4096 * 1000).encode(), b"")
            if command == [asmond_system.VM_STAT_PATH]:
                return subprocess.CompletedProcess(command, 0, vm_stat.encode(), b"")
            if command == [asmond_system.MEMORY_PRESSURE_PATH]:
                return subprocess.CompletedProcess(command, 0, pressure.encode(), b"")
            if command[:3] == [asmond_system.SYSCTL_PATH, "-n", "vm.swapusage"]:
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
            asmond.read_charge_stats = lambda *_: (asmond.BatteryStats(), asmond.UsbCStats())
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

    def test_sampler_retry_uses_bounded_exponential_backoff(self) -> None:
        retry = asmond.SamplerRetryState(base_delay_s=0.5, max_delay_s=2.0)
        self.assertEqual(retry.schedule(now=10.0), 0.5)
        self.assertFalse(retry.ready(now=10.49))
        self.assertTrue(retry.ready(now=10.5))
        self.assertEqual(retry.schedule(now=20.0), 1.0)
        self.assertEqual(retry.schedule(now=30.0), 2.0)
        self.assertEqual(retry.schedule(now=40.0), 2.0)
        retry.reset()
        self.assertFalse(retry.ready(now=100.0))

    def test_power_average_uses_elapsed_time_not_sample_count(self) -> None:
        history = asmond.History(10)
        history.add(asmond.MetricSample(cpu_power_mw=100.0), now=10.0)
        history.add(asmond.MetricSample(cpu_power_mw=300.0), now=35.0)
        history.add(asmond.MetricSample(cpu_power_mw=500.0), now=45.0)
        self.assertEqual(asmond.avg_power_window(history, history.cpu_power, window_s=30.0, now=45.0), 400.0)


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
    def test_combined_power_keeps_milliwatts_across_magnitude_and_interval_boundaries(self):
        for value in (0, 0.125, 5.18, 99, 99.999, 100, 101, 100000):
            for interval in (0.5, 1, 10):
                with self.subTest(value=value, interval=interval):
                    sample = asmond.sample_from_plist({"processor": {"combined_power": value}}, interval_s=interval)
                    self.assertEqual(sample.soc_power_mw, value)
        for raw, expected in (("5.18 W", 5180), ("5.18 mW", 5.18), ("5180 uw", 5.18)):
            with self.subTest(raw=raw):
                sample = asmond.sample_from_plist({"processor": {"combined_power": raw}})
                self.assertAlmostEqual(sample.soc_power_mw, expected)
        for value in (-1, True, float("nan"), float("inf"), "5 joules", "5 mws", "5 unknown"):
            with self.subTest(invalid=value):
                self.assertIsNone(asmond.sample_from_plist({"processor": {"combined_power": value}}).soc_power_mw)

    def test_explicit_power_units_take_precedence_and_watt_fields_keep_their_schema(self):
        import asmond_powermetrics
        for value, expected in ((5, 5000), ("5 W", 5000), ("500 mW", 500), ("500 uw", 0.5)):
            self.assertEqual(asmond_powermetrics.watts_value_to_mw(value), expected)
        self.assertEqual(asmond_powermetrics.as_mw("cpu_power_w", "500 mW"), 500)
        self.assertEqual(asmond_powermetrics.as_mw("cpu_power_mw", "5 W"), 5000)
        for value in ("5 joules", "5 mws", "5 unknown", 1e308):
            self.assertIsNone(asmond_powermetrics.watts_value_to_mw(value))

    def test_real_m2_plist_power_units_match_components_and_elapsed_energy(self):
        rows = json.loads((Path(__file__).parent / "tests/fixtures/apple_silicon_power_units.json").read_text())
        self.assertEqual(len(rows), 5)
        for row in rows:
            p = row["processor"]
            sample = asmond.sample_from_plist(row, interval_s=10)
            self.assertEqual(sample.soc_power_mw, p["combined_power"])
            self.assertAlmostEqual(sample.soc_power_mw, sum(p[name] for name in ("cpu_power", "gpu_power", "ane_power")), delta=0.01)
            self.assertAlmostEqual(sample.soc_power_mw, sample.cpu_power_mw + sample.gpu_power_mw + sample.ane_power_mw, delta=0.01)

    def test_apple_silicon_fixture_parses_structured_sample(self) -> None:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "apple_silicon_sample.plist"
        sample = asmond.sample_from_plist(plistlib.loads(fixture.read_bytes()), interval_s=0.5)

        self.assertAlmostEqual(sample.cpu_power_mw or 0.0, 500.0)
        self.assertAlmostEqual(sample.gpu_power_mw or 0.0, 200.0)
        self.assertEqual(sample.ane_power_mw, 0.0)
        self.assertAlmostEqual(sample.soc_power_mw or 0.0, 700.0)
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
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 16.48368, places=4)
        self.assertAlmostEqual(sample.gpu_usage_pct or 0.0, 0.0680779, places=6)
        self.assertAlmostEqual(sample.p_freq_mhz or 0.0, 2236.85, places=2)
        self.assertAlmostEqual(sample.gpu_freq_mhz or 0.0, 750.0)
        self.assertEqual(sample.thermal_pressure, "Nominal")
        self.assertIsNone(sample.soc_temp_c)
        self.assertFalse(sample.throttled)
        self.assertEqual(sample.throttle_reasons, [])
        self.assertEqual(
            sample.performance_limit_reasons,
            ["CPU LIMIT MAX_TURBO_LIMIT", "CPU LIMIT TURBO_ATTENUATION"],
        )
        self.assertEqual([core.label for core in sample.cores], ["C0", "C1", "C2", "C3", "C4", "C5"])
        self.assertEqual(asmond.clock_panel_rows(sample)[:2], [("CPU", "2.24 GHz"), ("GPU", "750 MHz")])

    def test_intel_macbookair_fixture_parses_smc_without_false_throttling(self) -> None:
        fixture = Path(__file__).parent / "tests" / "fixtures" / "intel_macbookair_charging_sample.plist"
        sample = asmond.sample_from_plist(plistlib.loads(fixture.read_bytes()), interval_s=1.0)

        self.assertEqual(sample.telemetry_source, "intel")
        self.assertAlmostEqual(sample.soc_power_mw or 0.0, 3521.96, places=2)
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 24.09805, places=4)
        self.assertAlmostEqual(sample.gpu_usage_pct or 0.0, 1.2749, places=4)
        self.assertAlmostEqual(sample.soc_temp_c or 0.0, 80.71, places=2)
        self.assertAlmostEqual(sample.temp_max_c or 0.0, 80.71, places=2)
        self.assertEqual(sample.fan_rpm, 1196.0)
        self.assertFalse(sample.throttled)
        self.assertEqual(sample.throttle_reasons, [])
        self.assertEqual(
            sample.performance_limit_reasons,
            ["CPU LIMIT MAX_TURBO_LIMIT", "RING LIMIT VR_ICCMAX"],
        )

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

    def test_invalid_powermetrics_blocks_are_not_reported_as_live_values(self) -> None:
        invalid_sample = asmond.sample_from_plist(
            {"invalid": True, "processor": {"cpu_energy": 1000, "combined_power": 5}},
            interval_s=1.0,
        )
        self.assertIsNone(invalid_sample.cpu_power_mw)
        self.assertIsNone(invalid_sample.soc_power_mw)
        self.assertIn("marked the sample invalid", invalid_sample.warning or "")
        self.assertGreater(invalid_sample.raw_keys, 0)
        self.assertFalse(asmond.metric_sample_has_telemetry(invalid_sample))
        invalid_sample.cpu_temp_avg_c = 42.0
        invalid_sample.temp_max_c = 43.0
        self.assertTrue(asmond.metric_sample_has_telemetry(invalid_sample))
        self.assertFalse(asmond.powermetrics_sample_is_usable(invalid_sample))

        partial = asmond.sample_from_plist(
            {
                "processor": {"cpu_energy": 1000, "gpu_energy": 200},
                "gpu": {"invalid": True, "idle_ratio": 0.0, "freq_hz": 1_000_000_000},
            },
            interval_s=1.0,
        )
        self.assertEqual(partial.cpu_power_mw, 1000.0)
        self.assertEqual(partial.gpu_power_mw, 200.0)
        self.assertIsNone(partial.gpu_usage_pct)
        self.assertIn("1 invalid block", partial.warning or "")
        self.assertTrue(asmond.metric_sample_has_telemetry(partial))
        self.assertTrue(asmond.powermetrics_sample_is_usable(partial))

        self.assertFalse(asmond.powermetrics_sample_is_usable(asmond.sample_from_plist({})))

    def test_ane_power_is_not_presented_as_measured_utilization(self) -> None:
        sample = asmond.sample_from_plist({"ane_power": 4000})

        self.assertEqual(sample.ane_power_mw, 4000.0)
        self.assertIsNone(sample.ane_usage_pct)

    def test_negative_and_nonfinite_telemetry_is_rejected(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {"cpu_energy": -10, "combined_power": -5},
                "gpu": {"idle_ratio": -1, "freq_hz": -1},
                "cpu_power": float("inf"),
            }
        )
        self.assertIsNone(sample.cpu_power_mw)
        self.assertIsNone(sample.soc_power_mw)
        self.assertIsNone(sample.gpu_usage_pct)
        self.assertIsNone(sample.gpu_freq_mhz)

    def test_elapsed_ns_controls_energy_power_conversion(self) -> None:
        sample = asmond.sample_from_plist(
            {"elapsed_ns": 2_000_000_000, "processor": {"cpu_energy": 1000}},
            interval_s=1.0,
        )
        self.assertEqual(sample.cpu_power_mw, 500.0)

    def test_static_and_intel_performance_limits_do_not_infer_throttling(self) -> None:
        static = asmond.sample_from_plist(
            {"thermal_pressure": "Nominal", "soc_temp_limit": "110 C", "plimit_hz": 3_000_000_000}
        )
        self.assertFalse(static.throttled)
        self.assertEqual(static.throttle_reasons, [])

        active = asmond.sample_from_plist(
            {
                "thermal_pressure": "Nominal",
                "processor": {
                    "packages": [
                        {"perf_limit_reasons": ["CPU LIMIT THERMAL", "CPU LIMIT TURBO_ATTENUATION"]}
                    ]
                },
            }
        )
        self.assertFalse(active.throttled)
        self.assertEqual(active.throttle_reasons, [])
        self.assertEqual(
            active.performance_limit_reasons,
            ["CPU LIMIT THERMAL", "CPU LIMIT TURBO_ATTENUATION"],
        )

        ordinary_intel_limits = asmond.sample_from_plist(
            {
                "thermal_pressure": "Nominal",
                "processor": {
                    "packages": [
                        {"perf_limit_reasons": ["CPU LIMIT MAX_TURBO_LIMIT", "RING LIMIT VR_ICCMAX"]}
                    ]
                },
            }
        )
        self.assertFalse(ordinary_intel_limits.throttled)
        self.assertEqual(ordinary_intel_limits.throttle_reasons, [])
        self.assertEqual(
            ordinary_intel_limits.performance_limit_reasons,
            ["CPU LIMIT MAX_TURBO_LIMIT", "RING LIMIT VR_ICCMAX"],
        )

    def test_only_real_pressure_or_explicit_throttle_signals_mark_throttling(self) -> None:
        moderate = asmond.sample_from_plist(
            {"thermal_pressure": "Moderate", "smc": {"cpu_thermal_level": 61}}
        )
        serious = asmond.sample_from_plist({"thermal_pressure": "Serious"})
        explicit = asmond.sample_from_plist({"thermal_pressure": "Nominal", "cpu_throttle": 1})

        self.assertFalse(moderate.throttled)
        self.assertEqual(moderate.throttle_reasons, [])
        self.assertTrue(serious.throttled)
        self.assertEqual(serious.throttle_reasons, [])
        self.assertTrue(explicit.throttled)
        self.assertEqual(explicit.throttle_reasons, ["cpu throttle"])

    def test_active_smc_prochot_marks_sample_throttled(self) -> None:
        sample = asmond.sample_from_plist(
            {"thermal_pressure": "Nominal", "smc": {"cpu_prochot": True, "cpu_thermal_level": 1}}
        )
        self.assertTrue(sample.throttled)
        self.assertTrue(any("prochot" in reason for reason in sample.throttle_reasons))

    def test_temperature_fallback_uses_tokens_and_deduplicates_aliases(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "attempt_count": 50,
                "thermal_sensors": [
                    {"name": "CPU die", "temperature": 40, "value": 80},
                ],
            }
        )
        self.assertEqual(sample.soc_temp_c, 40.0)
        self.assertEqual(sample.temp_max_c, 40.0)

    def test_cpu_id_zero_is_not_replaced_by_position(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "clusters": [
                        {"name": "P cluster", "cpus": [{"cpu": 7, "idle_ratio": 0.5}]},
                        {"name": "E cluster", "cpus": [{"cpu": 0, "idle_ratio": 0.8}]},
                    ]
                }
            }
        )
        self.assertEqual([core.label for core in sample.cores], ["P7", "E0"])

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
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 30.0)
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

    def test_intel_package_active_ratio_is_fallback_when_core_rows_are_absent(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "packages": [
                        {
                            "cores_active_ratio": 0.42,
                            "gpu_active_ratio": 0.12,
                        }
                    ]
                }
            }
        )
        self.assertAlmostEqual(sample.cpu_usage_pct or 0.0, 42.0)

    def test_generic_package_list_does_not_trigger_intel_core_rows(self) -> None:
        sample = asmond.sample_from_plist({"processor": {"packages": [{"cpus": [{"cpu": 0, "active_ratio": 0.5}]}]}})
        self.assertEqual(sample.cores, [])
        self.assertIsNone(sample.cpu_usage_pct)

    def test_apple_silicon_energy_fields_take_precedence_over_intel_watt_fields(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "processor": {
                    "combined_power": 2000.0,
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

    def test_usb_data_link_speed_requires_an_exact_unambiguous_port_match(self) -> None:
        usb_ports = [
            {
                "UsbIOPort": "IOService:/machine/Port-USB-C@2",
                "locationID": 0x01200000,
            }
        ]
        self.assertEqual(
            asmond.usb_data_link_speeds_by_port(
                usb_ports,
                [{"locationID": 0x01200000, "UsbLinkSpeed": 10_000_000_000}],
            ),
            {(2, 2): 10_000_000_000},
        )
        self.assertEqual(
            asmond.usb_data_link_speeds_by_port(
                usb_ports,
                [
                    {"locationID": 0x01200000, "UsbLinkSpeed": 10_000_000_000},
                    {"locationID": 0x01200000, "UsbLinkSpeed": 5_000_000_000},
                ],
            ),
            {},
        )
        self.assertEqual(
            asmond.usb_data_link_speeds_by_port(
                [
                    *usb_ports,
                    {
                        "UsbIOPort": "IOService:/machine/Port-USB-C@2",
                        "locationID": 0x01100000,
                    },
                ],
                [
                    {"locationID": 0x01200000, "UsbLinkSpeed": 10_000_000_000},
                    {"locationID": 0x01100000, "UsbLinkSpeed": 480_000_000},
                ],
            ),
            {(2, 2): 10_000_000_000},
        )
        self.assertEqual(
            asmond.usb_data_link_speeds_by_port(
                usb_ports,
                [{"locationID": 0x00200000, "UsbLinkSpeed": 10_000_000_000}],
            ),
            {},
        )

    def test_usb_data_link_label_prefers_verified_rate_and_separates_dp(self) -> None:
        self.assertEqual(
            asmond.usb_data_link_label(["CC", "USB3", "DisplayPort"], 10_000_000_000),
            "USB 3.2 Gen 2 · 10 Gb/s",
        )
        self.assertEqual(asmond.usb_data_link_label(["CC", "USB3"], None), "USB3")
        self.assertEqual(asmond.usb_data_link_label(["CC", "CIO"], 10_000_000_000), "Thunderbolt")

    def test_usb_c_stats_decodes_real_emarker_vdos_for_matching_hpm_port(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = 3 | (2 << 5) | (3 << 9) | (1 << 17) | (2 << 18)
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [{}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortTypeDescription": "USB-C",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": True,
                    "TransportsActive": ["CC", "USB3", "DisplayPort"],
                }
            ],
            [
                {
                    "ParentBuiltInPortType": 2,
                    "ParentBuiltInPortNumber": 1,
                    "Specification Revision": 3,
                    "Metadata": {
                        "Vendor ID": 0x1234,
                        "Product ID": 0x5678,
                        "VDOs": [
                            id_header.to_bytes(4, "little"),
                            bytes(4),
                            bytes(4),
                            cable_vdo.to_bytes(4, "little"),
                        ],
                    },
                }
            ],
            [
                {
                    "UsbIOPort": "IOService:/machine/Port-USB-C@1",
                    "locationID": 0x00200000,
                }
            ],
            [
                {"locationID": 0x00200000, "UsbLinkSpeed": 10_000_000_000},
                {"locationID": 0x01200000, "UsbLinkSpeed": 480_000_000},
            ],
        )

        port = stats.ports[0]
        self.assertTrue(port.cable_query_supported)
        self.assertEqual(port.active_transports, ["CC", "USB3", "DisplayPort"])
        self.assertEqual(port.data_link_speed_bps, 10_000_000_000)
        self.assertIsNotNone(port.cable_info)
        assert port.cable_info is not None
        self.assertEqual(port.cable_info.cable_type, "passive")
        self.assertEqual(port.cable_info.current_label, "5 A")
        self.assertEqual(port.cable_info.speed_label, "USB4 Gen 3")
        self.assertEqual(port.cable_info.max_voltage_v, 50.0)
        self.assertEqual(port.cable_info.max_power_w, 240.0)
        self.assertEqual(port.cable_info.vendor_id, 0x1234)
        self.assertEqual(port.cable_info.product_id, 0x5678)
        self.assertEqual(port.cable_info.pd_revision, "PD 3.0")
        report_port = asmond.usb_to_report_dict(stats)["ports"][0]
        self.assertEqual(report_port["active_transports"], ["CC", "USB3", "DisplayPort"])
        self.assertEqual(report_port["data_link"], "USB 3.2 Gen 2 · 10 Gb/s")
        self.assertEqual(report_port["data_link_speed_bps"], 10_000_000_000)
        self.assertEqual(report_port["dp_alt_mode"], "active")

    def test_emarker_voltage_codes_use_all_usb_pd_cable_classes(self) -> None:
        id_header = (3 << 27) | 0x1234
        for voltage_bits, expected_voltage, expected_power in (
            (0, 20.0, 100.0),
            (1, 30.0, 140.0),
            (2, 40.0, 180.0),
            (3, 50.0, 240.0),
        ):
            with self.subTest(voltage_bits=voltage_bits):
                cable_vdo = (voltage_bits << 9) | (2 << 5)
                info = asmond.cable_info_from_item(
                    {
                        "Specification Revision": 3,
                        "Metadata": {
                            "VDOs": [
                                id_header.to_bytes(4, "little"),
                                bytes(4),
                                bytes(4),
                                cable_vdo.to_bytes(4, "little"),
                            ]
                        }
                    }
                )
                self.assertIsNotNone(info)
                assert info is not None
                self.assertEqual(info.max_voltage_v, expected_voltage)
                self.assertEqual(info.max_power_w, expected_power)

    def test_pd2_emarker_does_not_decode_pd31_epr_voltage_bits(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = (3 << 9) | (2 << 5) | 3
        info = asmond.cable_info_from_item(
            {
                "Specification Revision": 2,
                "Metadata": {
                    "VDOs": [
                        id_header.to_bytes(4, "little"),
                        bytes(4),
                        bytes(4),
                        cable_vdo.to_bytes(4, "little"),
                    ]
                },
            }
        )

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.pd_revision, "PD 2.0")
        self.assertEqual(info.max_voltage_v, 20.0)
        self.assertEqual(info.max_power_w, 100.0)
        self.assertEqual(info.speed_label, "")

    def test_unknown_emarker_revision_does_not_claim_newer_vdo_fields(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = (3 << 9) | (2 << 5) | 3
        info = asmond.cable_info_from_item(
            {
                "Metadata": {
                    "VDOs": [
                        id_header.to_bytes(4, "little"),
                        bytes(4),
                        bytes(4),
                        cable_vdo.to_bytes(4, "little"),
                    ]
                },
            }
        )

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.pd_revision, "")
        self.assertIsNone(info.max_voltage_v)
        self.assertIsNone(info.max_power_w)
        self.assertEqual(info.speed_label, "")

    def test_emarker_decoder_does_not_shift_malformed_vdo_positions(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = (3 << 9) | (2 << 5)
        info = asmond.cable_info_from_item(
            {
                "Metadata": {
                    "VDOs": [
                        id_header.to_bytes(4, "little"),
                        None,
                        bytes(4),
                        cable_vdo.to_bytes(4, "little"),
                        bytes(4),
                    ]
                }
            }
        )

        self.assertIsNone(info)

    def test_usb_c_stats_never_infers_cable_rating_from_contract_current(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x100FA000,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x000641F4],
                    }
                ],
            },
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortTypeDescription": "USB-C",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": True,
                }
            ],
        )

        self.assertTrue(stats.ports[0].cable_query_supported)
        self.assertIsNone(stats.ports[0].cable_info)

    def test_cable_panel_distinguishes_emarker_data_from_unavailable_data(self) -> None:
        class Window:
            def __init__(self) -> None:
                self.strings: list[str] = []
                self.writes: list[tuple[int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return 20, 120

            def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
                self.strings.append(text)
                self.writes.append((_x, text))

        colors = {"muted": 0, "fg": 0}
        unavailable = Window()
        asmond.draw_cable_section(
            unavailable,
            1,
            2,
            6,
            90,
            asmond.UsbCStats(
                ports=[
                    asmond.UsbCPortStats(
                        "USB-C 1",
                        connected=True,
                        cable_query_supported=True,
                        active_transports=["CC", "USB3"],
                        data_link_speed_bps=10_000_000_000,
                    )
                ]
            ),
            colors,
        )
        self.assertIn("no E-marker data", unavailable.strings)
        self.assertIn("USB 3.2 Gen 2 · 10 Gb/s", unavailable.strings)
        self.assertIn("not active", unavailable.strings)

        narrow_unavailable = Window()
        asmond.draw_cable_section(
            narrow_unavailable,
            1,
            2,
            6,
            24,
            asmond.UsbCStats(
                ports=[
                    asmond.UsbCPortStats(
                        "USB-C 1",
                        connected=True,
                        cable_query_supported=True,
                        active_transports=["CC", "USB3"],
                        data_link_speed_bps=10_000_000_000,
                    )
                ]
            ),
            colors,
        )
        self.assertTrue(all(write_x + len(text) <= 2 + 24 for write_x, text in narrow_unavailable.writes))

        available = Window()
        asmond.draw_cable_section(
            available,
            1,
            2,
            6,
            90,
            asmond.UsbCStats(
                ports=[
                    asmond.UsbCPortStats(
                        "USB-C 1",
                        connected=True,
                        cable_query_supported=True,
                        active_transports=["CC", "USB3"],
                        data_link_speed_bps=10_000_000_000,
                        cable_info=asmond.CableInfo(
                            cable_type="passive",
                            current_label="5 A",
                            max_voltage_v=50.0,
                            max_power_w=240.0,
                            speed_label="USB4 Gen 3",
                            vendor_id=0x1234,
                            product_id=0x5678,
                            pd_revision="PD 3.0",
                        ),
                    )
                ]
            ),
            colors,
        )
        for value in (
            "E-marker",
            "passive",
            "5 A",
            "USB 3.2 Gen 2 · 10 Gb/s",
            "not active",
            "240.0 W · 50 V max",
            "USB4 Gen 3",
            "0x1234",
            "0x5678",
            "PD 3.0",
        ):
            self.assertIn(value, available.strings)

        dp_active = Window()
        port = asmond.UsbCPortStats(
            "USB-C 1",
            connected=True,
            cable_query_supported=True,
            active_transports=["CC", "USB3", "DisplayPort"],
            data_link_speed_bps=10_000_000_000,
            cable_info=asmond.CableInfo(cable_type="passive"),
        )
        asmond.draw_cable_section(dp_active, 1, 2, 6, 90, asmond.UsbCStats(ports=[port]), colors)
        self.assertIn("USB 3.2 Gen 2 · 10 Gb/s", dp_active.strings)
        self.assertIn("active", dp_active.strings)

        medium_width = Window()
        asmond.draw_cable_section(
            medium_width,
            1,
            2,
            6,
            58,
            asmond.UsbCStats(ports=[port]),
            colors,
        )
        self.assertIn("USB 3.2 Gen 2 · 10 Gb/s", medium_width.strings)

    def test_cable_panel_prefers_the_connected_port_over_stale_identity(self) -> None:
        stale = asmond.UsbCPortStats(
            "USB-C 1",
            connected=False,
            cable_query_supported=True,
            cable_info=asmond.CableInfo(cable_type="passive"),
        )
        connected = asmond.UsbCPortStats(
            "USB-C 2",
            connected=True,
            cable_query_supported=True,
            active_transports=["CC", "USB3"],
        )
        self.assertIs(asmond.selected_cable_port(asmond.UsbCStats(ports=[stale, connected])), connected)

    def test_cable_panel_prefers_active_charge_port_over_other_connected_cable(self) -> None:
        data_port = asmond.UsbCPortStats(
            "USB-C 1",
            connected=True,
            role="source/data",
            cable_query_supported=True,
            cable_info=asmond.CableInfo(cable_type="passive", vendor_id=0x1111),
        )
        charge_port = asmond.UsbCPortStats(
            "USB-C 2",
            connected=True,
            role="sink",
            cable_query_supported=True,
            cable_info=asmond.CableInfo(cable_type="passive", vendor_id=0x2222),
        )
        stats = asmond.UsbCStats(
            ports=[data_port, charge_port],
            active_index=1,
            external_connected=True,
        )

        self.assertIs(asmond.selected_cable_port(stats), charge_port)

    def test_disconnected_port_does_not_expose_stale_emarker_identity(self) -> None:
        id_header = (3 << 27) | 0x1234
        stats = asmond.usb_c_stats_from_item(
            {"ExternalConnected": False, "PortControllerInfo": [{}]},
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": False,
                }
            ],
            [
                {
                    "ParentPortType": 2,
                    "ParentPortNumber": 1,
                    "Metadata": {"VDOs": [id_header.to_bytes(4, "little")]},
                }
            ],
        )

        self.assertIsNone(stats.ports[0].cable_info)
        report_port = asmond.usb_to_report_dict(stats)["ports"][0]
        self.assertNotIn("cable_type", report_port)
        self.assertEqual(report_port["cable_telemetry"], "supported")
        self.assertEqual(report_port["active_transports"], [])
        self.assertEqual(report_port["data_link"], "unavailable")
        self.assertEqual(report_port["dp_alt_mode"], "unavailable")

    def test_usb_c_stats_decodes_active_contract(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "IsCharging": True,
                "BestAdapterIndex": 0,
                "PortControllerInfo": [
                    {
                        "PortType": 2, "PortNumber": 1,
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

    def test_usb_c_stats_decodes_pps_contract_with_pps_rdo_units(self) -> None:
        pps_pdo = (3 << 30) | (200 << 17) | (50 << 8) | 60
        pps_rdo = (1 << 28) | (450 << 9) | 60
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": pps_rdo,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [pps_pdo],
                    }
                ],
            }
        )

        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertAlmostEqual(stats.active_port.voltage_v or 0.0, 9.0)
        self.assertAlmostEqual(stats.active_port.current_a or 0.0, 3.0)
        self.assertAlmostEqual(stats.active_port.power_w or 0.0, 27.0)

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
                    {"PortType": 2, "PortNumber": 1, "PortControllerActiveContractRdo": 0, "PortControllerNPDOs": 0, "PortControllerPortPDO": []},
                    {"PortType": 2, "PortNumber": 2, "PortControllerActiveContractRdo": 0, "PortControllerNPDOs": 0, "PortControllerPortPDO": []},
                    {
                        "PortDescription": "MagSafe",
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                ],
                "FedDetails": [
                    {"PortType": 2, "PortNumber": 1, "FedExternalConnected": 0},
                    {"PortType": 2, "PortNumber": 2, "FedExternalConnected": 0},
                    {"PortDescription": "MagSafe", "FedExternalConnected": 1},
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
                        "PortType": 2, "PortNumber": 1,
                        "PortControllerActiveContractRdo": 0x478258A8,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0901F42C],
                    },
                    {
                        "PortType": 2, "PortNumber": 2,
                        "PortControllerActiveContractRdo": 0x478258A8,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0901F42C],
                    },
                    {
                        "PortDescription": "MagSafe",
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

    def test_usb_c_stats_aligns_reordered_hpm_ports_by_physical_number(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 0,
                "PortControllerInfo": [
                    {
                        "PortType": 2, "PortNumber": 1,
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                    {},
                ],
                "FedDetails": [{"FedExternalConnected": 1}, {"FedExternalConnected": 0}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@2",
                    "PortType": 2,
                    "PortNumber": 2,
                    "ConnectionActive": 0,
                },
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": 1,
                },
            ],
        )

        self.assertEqual([port.label for port in stats.ports], ["USB-C 1", "USB-C 2"])
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "USB-C 1")
        self.assertAlmostEqual(stats.active_port.voltage_v or 0.0, 20.0)
        self.assertAlmostEqual(stats.active_port.current_a or 0.0, 1.5)

    def test_hpm_only_preserves_sparse_physical_port_identities(self) -> None:
        usb_c_2 = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortType": 2,
                    "PortNumber": 2,
                    "ConnectionActive": True,
                    "TransportsActive": ["USB3"],
                }
            ],
        )
        magsafe = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": "Port-MagSafe 3@1",
                    "ConnectionActive": True,
                }
            ],
        )

        self.assertEqual([port.label for port in usb_c_2.ports], ["USB-C 2"])
        self.assertTrue(usb_c_2.ports[0].connected)
        self.assertEqual([port.label for port in magsafe.ports], ["MagSafe"])
        self.assertTrue(magsafe.ports[0].connected)

    def test_conflicting_hpm_identity_does_not_attach_other_port_cable_data(self) -> None:
        id_header = (3 << 27) | 0x1234
        cable_vdo = (3 << 9) | (2 << 5) | 3
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [{}],
                "FedDetails": [{"FedExternalConnected": 1}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 2,
                    "ConnectionActive": True,
                    "TransportsActive": ["USB3"],
                }
            ],
            [
                {
                    "ParentPortType": 2,
                    "ParentPortNumber": 2,
                    "Metadata": {
                        "VDOs": [
                            id_header.to_bytes(4, "little"),
                            bytes(4),
                            bytes(4),
                            cable_vdo.to_bytes(4, "little"),
                        ]
                    },
                }
            ],
        )

        self.assertIsNone(stats.active_port)
        self.assertEqual(stats.ports[0].label, "Port 1")
        self.assertIsNone(stats.ports[0].cable_info)
        self.assertFalse(stats.ports[0].cable_query_supported)
        self.assertEqual(stats.ports[0].active_transports, [])

    def test_duplicate_hpm_identity_cannot_choose_connection_by_list_order(self) -> None:
        disconnected = {
            "PortDescription": "Port-USB-C@1",
            "PortType": 2,
            "PortNumber": 1,
            "ConnectionActive": False,
        }
        connected = {
            "PortDescription": "Port-USB-C@1",
            "PortType": 2,
            "PortNumber": 1,
            "ConnectionActive": True,
            "TransportsActive": ["USB3"],
        }
        battery = {"PortControllerInfo": [{}]}

        first = asmond.usb_c_stats_from_item(battery, [disconnected, connected])
        second = asmond.usb_c_stats_from_item(battery, [connected, disconnected])

        self.assertEqual(first, second)
        self.assertIsNone(first.active_port)
        self.assertEqual([port.connected for port in first.ports], [False, False])
        self.assertEqual([port.active_transports for port in first.ports], [[], []])

    def test_invalid_hpm_port_number_cannot_expand_alignment(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {"PortControllerInfo": [{}]},
            [
                {
                    "PortType": 2,
                    "PortNumber": 2**32 - 1,
                    "ConnectionActive": True,
                }
            ],
        )
        direct = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": f"Port-USB-C@{2**32 - 1}",
                    "PortType": 2,
                    "PortNumber": 2**32 - 1,
                    "ConnectionActive": True,
                }
            ],
        )

        self.assertEqual(len(stats.ports), 1)
        self.assertFalse(stats.ports[0].connected)
        self.assertEqual([port.label for port in direct.ports], ["Port 1"])
        self.assertFalse(direct.ports[0].cable_query_supported)

    def test_overlong_iokit_port_numbers_are_rejected_without_integer_conversion(self) -> None:
        huge = "9" * 5000
        stats = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": f"Port-USB-C@{huge}",
                    "ConnectionActive": True,
                }
            ],
        )
        speeds = asmond.usb_data_link_speeds_by_port(
            [{"UsbIOPort": f"/Port-USB-C@{huge}", "locationID": 1}],
            [{"locationID": 1, "UsbLinkSpeed": 10_000_000_000}],
        )
        pdos = asmond.pdo_list_from_port(
            {f"PortControllerSrcPdo{huge}": 0x0001912C}
        )

        self.assertEqual([port.label for port in stats.ports], ["Port 1"])
        self.assertEqual(speeds, {})
        self.assertEqual(pdos, [])

    def test_dp_alt_report_uses_connected_hpm_transport_without_emarker_mapping(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "ConnectionActive": True,
                    "TransportsActive": ["DisplayPort"],
                }
            ],
        )

        report_port = asmond.usb_to_report_dict(stats)["ports"][0]
        self.assertEqual(report_port["cable_telemetry"], "unavailable")
        self.assertEqual(report_port["active_transports"], ["DisplayPort"])
        self.assertEqual(report_port["dp_alt_mode"], "active")

    def test_usb_c_stats_does_not_index_fallback_over_conflicting_hpm_identity(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 0,
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                    {},
                ],
                "FedDetails": [{"FedExternalConnected": 1}, {"FedExternalConnected": 0}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@2",
                    "PortType": 2,
                    "PortNumber": 2,
                    "ConnectionActive": 1,
                }
            ],
        )

        self.assertEqual([port.label for port in stats.ports], ["USB-C 2"])
        self.assertIsNone(stats.active_port)
        self.assertIsNone(stats.ports[0].cable_info)
        self.assertIsNone(stats.ports[0].voltage_v)

    def test_usb_c_stats_uses_controller_evidence_when_external_flag_is_absent(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "BestAdapterIndex": 0,
                "PortControllerInfo": [{}],
                "FedDetails": [{"FedExternalConnected": 1}],
            }
        )

        self.assertIsNone(stats.external_connected)
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertTrue(stats.active_port.connected)
        self.assertEqual(stats.active_port.role, "sink")
        self.assertEqual(stats.active_port.label, "Input 1 (port unknown)")

    def test_usb_c_stats_rejects_charging_without_any_input_evidence(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "IsCharging": True,
                "PortControllerInfo": [{}],
                "FedDetails": [{}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": False,
                }
            ],
        )

        self.assertFalse(stats.charging)
        self.assertIsNone(stats.active_port)
        self.assertFalse(asmond.charge_input_active(stats))
        self.assertEqual(asmond.usb_to_report_dict(stats)["input_type"], "n/a")

    def test_report_separates_adapter_rating_from_live_input_power(self) -> None:
        report = asmond.usb_to_report_dict(
            asmond.UsbCStats(
                external_connected=True,
                input_kind="magsafe",
                adapter_power_w=45.0,
            )
        )

        self.assertEqual(report["power"], "n/a")
        self.assertEqual(report["adapter_rating"], "45.0 W")

    def test_usb_c_stats_includes_fed_ports_beyond_controller_info(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 1,
                "PortControllerInfo": [{}],
                "FedDetails": [{}, {"FedExternalConnected": 1}],
            }
        )

        self.assertEqual([port.label for port in stats.ports], ["Controller 1 (port unknown)", "Input 2 (port unknown)"])
        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "Input 2 (port unknown)")
        self.assertTrue(stats.active_port.connected)
        self.assertEqual(stats.active_port.role, "sink")

    def test_usb_c_stats_preserves_sparse_pdo_positions_for_active_contract(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [
                    {
                        "PortType": 2, "PortNumber": 1,
                        "PortControllerActiveContractRdo": 0x20025896,
                        "PortControllerNPDOs": 3,
                        "PortControllerPortPDO": [0x0001912C, 0, 0x0006412C],
                    }
                ],
                "FedDetails": [{"PortType": 2, "PortNumber": 1, "FedExternalConnected": 1}],
            }
        )

        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertTrue(stats.active_port.connected)
        self.assertEqual(stats.active_port.pdo_labels, ["5V 3A", "20V 3A"])
        self.assertIsNone(stats.active_port.voltage_v)
        self.assertIsNone(stats.active_port.current_a)
        self.assertIsNone(stats.active_port.power_w)

        self.assertEqual(
            asmond.pdo_list_from_port(
                {
                    "PortControllerSrcPdoCount": 3,
                    "PortControllerSrcPdo1": 0x0001912C,
                    "PortControllerSrcPdo3": 0x0006412C,
                }
            ),
            [0x0001912C, None, 0x0006412C],
        )

    def test_usb_data_port_does_not_beat_unique_charging_port(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "BestAdapterIndex": 1,
                "PortControllerInfo": [
                    {},
                    {
                        "PortType": 2, "PortNumber": 2,
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    },
                ],
                "FedDetails": [{"FedExternalConnected": 0}, {"FedExternalConnected": 1}],
            },
            [
                {"PortDescription": "Port-USB-C@1", "ConnectionActive": 1, "TransportsActive": ["USB3"]},
                {"PortDescription": "Port-USB-C@2", "ConnectionActive": 1, "TransportsActive": ["CC"]},
            ],
        )

        self.assertIsNotNone(stats.active_port)
        assert stats.active_port is not None
        self.assertEqual(stats.active_port.label, "USB-C 2")
        self.assertEqual([port.role for port in stats.ports], ["source/data", "sink"])

    def test_hpm_data_connection_alone_is_not_a_power_input(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "ConnectionActive": 1,
                    "TransportsActive": ["USB3"],
                }
            ],
        )

        self.assertIsNone(stats.active_port)
        self.assertFalse(asmond.charge_input_active(stats))
        self.assertEqual(stats.ports[0].role, "source/data")
        self.assertEqual(asmond.usb_to_report_dict(stats)["input_type"], "n/a")

    def test_global_external_power_does_not_turn_unrelated_usb_data_port_into_sink(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {"ExternalConnected": True, "PortControllerInfo": [{}], "FedDetails": [{}]},
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "ConnectionActive": 1,
                    "TransportsActive": ["USB3"],
                }
            ],
        )

        self.assertIsNone(stats.active_port)
        self.assertFalse(asmond.charge_input_active(stats))
        self.assertEqual(stats.ports[0].role, "source/data")
        report = asmond.usb_to_report_dict(stats)
        self.assertEqual(report["active_port"], "n/a")
        self.assertEqual(report["input_type"], "n/a")
        self.assertEqual(report["voltage"], "n/a")

    def test_disconnected_hpm_port_rejects_stale_contract_without_global_state(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "PortControllerInfo": [
                    {
                        "PortControllerActiveContractRdo": 0x10025896,
                        "PortControllerNPDOs": 1,
                        "PortControllerPortPDO": [0x0006412C],
                    }
                ],
                "FedDetails": [{"FedExternalConnected": 1}],
            },
            [{"PortDescription": "Port-USB-C@1", "ConnectionActive": 0}],
        )

        self.assertIsNone(stats.active_port)
        self.assertFalse(asmond.charge_input_active(stats))
        self.assertEqual(stats.ports[0].role, "idle")
        self.assertEqual(asmond.usb_to_report_dict(stats)["input_type"], "n/a")

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
        self.assertIsNone(stats.system_voltage_v)
        self.assertIsNone(stats.system_current_a)
        self.assertIsNone(stats.system_power_w)
        report = asmond.usb_to_report_dict(stats)
        self.assertEqual(report["state"], "no input")
        self.assertEqual(report["input_type"], "n/a")
        self.assertEqual(report["voltage"], "n/a")
        self.assertEqual(report["current"], "n/a")
        self.assertEqual(report["power"], "n/a")

    def test_disconnected_hpm_port_ignores_stale_charging_flag(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": False,
                "IsCharging": True,
                "PortControllerInfo": [{}],
            },
            [
                {
                    "PortDescription": "USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": 0,
                    "TransportsActive": ["USB3"],
                }
            ],
            [],
            [{"UsbIOPort": "IOService:/machine/Port-USB-C@1", "locationID": 0x00200000}],
            [{"locationID": 0x00200000, "UsbLinkSpeed": 10_000_000_000}],
        )

        self.assertTrue(asmond.usb_c_supported(stats))
        self.assertFalse(stats.charging)
        self.assertEqual(asmond.usb_c_state_text(stats), "no input")
        report = asmond.usb_to_report_dict(stats)
        self.assertEqual(report["state"], "no input")
        self.assertEqual(report["active_port"], "n/a")
        self.assertEqual(report["ports"][0]["data_link"], "unavailable")
        self.assertEqual(report["ports"][0]["data_link_speed_bps"], "n/a")
        self.assertEqual(report["ports"][0]["dp_alt_mode"], "unavailable")

    def test_connected_data_port_does_not_override_explicitly_disconnected_power(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": False,
                "IsCharging": True,
                "PortControllerInfo": [{}],
            },
            [{"PortDescription": "USB-C@1", "ConnectionActive": 1}],
        )

        self.assertFalse(stats.charging)
        self.assertIsNone(stats.active_port)
        self.assertFalse(asmond.charge_input_active(stats))
        self.assertEqual(asmond.usb_c_state_text(stats), "no input")
        self.assertEqual([(port.connected, port.role) for port in stats.ports], [(True, "source/data")])

    def test_stale_system_telemetry_alone_does_not_claim_charge_input_support(self) -> None:
        stats = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": False,
                "PowerTelemetryData": {
                    "SystemVoltageIn": 20_000,
                    "SystemCurrentIn": 3_000,
                    "SystemPowerIn": 60_000,
                },
            }
        )
        self.assertFalse(asmond.usb_c_supported(stats))
        self.assertEqual(asmond.usb_to_report_dict(stats)["state"], "unsupported")


class AlertTests(unittest.TestCase):
    def test_alert_thresholds(self) -> None:
        alerts = asmond.build_alerts(
            asmond.MetricSample(throttled=True, temp_max_c=90.0),
            asmond.MemoryStats(swap_used_bytes=2 * 1024**3),
            asmond.BatteryStats(power_mw=-20000.0),
        )
        self.assertIn("THROTTLE", alerts)
        self.assertIn("TEMP 90°C", alerts)
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

    def test_zero_swap_threshold_disables_swap_alert(self) -> None:
        args = argparse.Namespace(alert_temp_c=85.0, alert_swap_gib=0.0, alert_battery_drain_w=15.0)
        alerts = asmond.build_alerts(
            asmond.MetricSample(),
            asmond.MemoryStats(swap_used_bytes=1024**3),
            asmond.BatteryStats(),
            args,
        )
        self.assertNotIn("SWAP", " ".join(alerts))


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
                    "combined_power": 5000.0,
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
        self.assertFalse(asmond.usb_c_supported(asmond.UsbCStats(charging=False)))
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
                        "PortType": 2, "PortNumber": 1,
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
        cable_usb_c = asmond.UsbCStats(
            ports=[asmond.UsbCPortStats("USB-C 1", connected=False, cable_query_supported=True)]
        )
        self.assertEqual(asmond.effective_charge_panel("battery", battery, usb_c), "battery")
        self.assertEqual(asmond.effective_charge_panel("usb", battery, usb_c), "usb")
        self.assertEqual(asmond.effective_charge_panel("cable", battery, cable_usb_c), "cable")
        self.assertEqual(asmond.effective_charge_panel("cable", battery, usb_c), "usb")
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
            samplers = command[command.index("--samplers") + 1].split(",")
            self.assertIn("smc", samplers)
            self.assertNotIn("ane_power", command)
            self.assertNotIn("battery", command)
        finally:
            asmond.platform.machine = old_machine
            asmond.powermetrics_needs_sudo = old_needs_sudo

    def test_physical_machine_detects_rosetta(self) -> None:
        def translated_run(command, **kwargs):
            self.assertEqual(command[0], asmond_system.SYSCTL_PATH)
            return subprocess.CompletedProcess(command, 0, b"1\n", b"")

        self.assertEqual(asmond_system.physical_machine("x86_64", run=translated_run), ("arm64", True))
        self.assertEqual(asmond_system.physical_machine("arm64", run=translated_run), ("arm64", False))

    def test_sudo_refresh_uses_trusted_path_and_environment(self) -> None:
        old_run = asmond.subprocess.run
        old_needs_sudo = asmond.powermetrics_needs_sudo
        calls: list[tuple[list[str], dict]] = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.powermetrics_needs_sudo = lambda: True
            self.assertEqual(asmond.refresh_sudo_credentials(prompt=False), (True, ""))
        finally:
            asmond.subprocess.run = old_run
            asmond.powermetrics_needs_sudo = old_needs_sudo
        self.assertEqual(calls[0][0], [asmond.SUDO_PATH, "-n", "-v"])
        self.assertEqual(calls[0][1]["env"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_powermetrics_access_message_uses_stderr_only(self) -> None:
        old_refresh = asmond.refresh_sudo_credentials
        old_needs_sudo = asmond.powermetrics_needs_sudo
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            asmond.powermetrics_needs_sudo = lambda: True
            asmond.refresh_sudo_credentials = lambda prompt=True: (True, "")
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                asmond.ensure_powermetrics_access(argparse.Namespace(mock=False))
        finally:
            asmond.refresh_sudo_credentials = old_refresh
            asmond.powermetrics_needs_sudo = old_needs_sudo
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unprivileged", stderr.getvalue())

    def test_doctor_rejects_non_plist_powermetrics_output(self) -> None:
        old_run = asmond.subprocess.run
        old_refresh = asmond.refresh_sudo_credentials
        old_needs_sudo = asmond.powermetrics_needs_sudo

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, b"not a plist", b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.refresh_sudo_credentials = lambda prompt=False: (True, "")
            asmond.powermetrics_needs_sudo = lambda: True
            rows = asmond.diagnostic_rows(argparse.Namespace(mock=False, live=True, interval=1.0))
        finally:
            asmond.subprocess.run = old_run
            asmond.refresh_sudo_credentials = old_refresh
            asmond.powermetrics_needs_sudo = old_needs_sudo
        sample_row = next(row for row in rows if row[0] == "powermetrics sample")
        self.assertEqual(sample_row[1], "fail")
        self.assertIn("plist invalid", sample_row[2])

    def test_powermetrics_stream_reports_unexpected_eof(self) -> None:
        old_popen = asmond.subprocess.Popen
        old_refresh = asmond.refresh_sudo_credentials

        class EmptyPipe:
            def read(self, _size=-1):
                return b""

            def readline(self):
                return b""

        class EndedProcess:
            stdout = EmptyPipe()
            stderr = EmptyPipe()

            def poll(self):
                return 7

        try:
            asmond.refresh_sudo_credentials = lambda prompt=False: (True, "")
            asmond.subprocess.Popen = lambda *args, **kwargs: EndedProcess()
            samples = list(asmond.PowerMetricsStream(1000).samples())
        finally:
            asmond.subprocess.Popen = old_popen
            asmond.refresh_sudo_credentials = old_refresh

        self.assertEqual(len(samples), 1)
        self.assertIn("exit 7", samples[0].warning or "")

    def test_powermetrics_stream_stop_during_sudo_refresh_prevents_spawn(self) -> None:
        old_popen = asmond.subprocess.Popen
        old_refresh = asmond.refresh_sudo_credentials
        stream = asmond.PowerMetricsStream(1000)
        popen_calls = 0

        def refresh_and_stop(prompt=False):
            stream.stop()
            return True, ""

        def fake_popen(*args, **kwargs):
            nonlocal popen_calls
            popen_calls += 1
            raise AssertionError("powermetrics must not start after stop")

        try:
            asmond.refresh_sudo_credentials = refresh_and_stop
            asmond.subprocess.Popen = fake_popen
            self.assertEqual(list(stream.samples()), [])
        finally:
            asmond.subprocess.Popen = old_popen
            asmond.refresh_sudo_credentials = old_refresh

        self.assertEqual(popen_calls, 0)

    def test_powermetrics_stream_stop_racing_popen_reaps_new_process(self) -> None:
        old_popen = asmond.subprocess.Popen
        old_refresh = asmond.refresh_sudo_credentials
        old_machine = asmond.physical_machine
        stream = asmond.PowerMetricsStream(1000)

        class RunningProcess:
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.running = True
                self.terminate_calls = 0
                self.wait_calls = 0

            def poll(self):
                return None if self.running else 0

            def terminate(self):
                self.terminate_calls += 1
                self.running = False

            def wait(self, timeout=None):
                self.wait_calls += 1
                return 0

            def kill(self):
                self.running = False

        process = RunningProcess()

        def popen_and_stop(*args, **kwargs):
            stream.stop()
            return process

        try:
            asmond.refresh_sudo_credentials = lambda prompt=False: (True, "")
            asmond.physical_machine = lambda: ("x86_64", False)
            asmond.subprocess.Popen = popen_and_stop
            self.assertEqual(list(stream.samples()), [])
        finally:
            asmond.subprocess.Popen = old_popen
            asmond.refresh_sudo_credentials = old_refresh
            asmond.physical_machine = old_machine

        self.assertIs(stream.proc, process)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)
        self.assertFalse(process.running)

    def test_powermetrics_stream_does_not_enrich_top_level_invalid_sample(self) -> None:
        old_popen = asmond.subprocess.Popen
        old_refresh = asmond.refresh_sudo_credentials
        old_apply = asmond.apply_hid_temperatures
        payload = plistlib.dumps({"invalid": True, "processor": {"cpu_energy": 1000}}) + b"\0"
        apply_calls = 0

        class PayloadPipe:
            def __init__(self) -> None:
                self.remaining = payload

            def read(self, _size=-1):
                value, self.remaining = self.remaining, b""
                return value

            def readline(self):
                return b""

        class EndedProcess:
            stdout = PayloadPipe()
            stderr = PayloadPipe()

            def poll(self):
                return 0

        def fake_apply(sample):
            nonlocal apply_calls
            apply_calls += 1
            sample.cpu_temp_avg_c = 42.0

        try:
            asmond.refresh_sudo_credentials = lambda prompt=False: (True, "")
            asmond.subprocess.Popen = lambda *args, **kwargs: EndedProcess()
            asmond.apply_hid_temperatures = fake_apply
            samples = list(asmond.PowerMetricsStream(1000).samples())
        finally:
            asmond.subprocess.Popen = old_popen
            asmond.refresh_sudo_credentials = old_refresh
            asmond.apply_hid_temperatures = old_apply

        self.assertEqual(apply_calls, 0)
        self.assertGreaterEqual(len(samples), 1)
        self.assertEqual(samples[0].warning, asmond.INVALID_SAMPLE_WARNING)
        self.assertIsNone(samples[0].cpu_temp_avg_c)
        self.assertFalse(asmond.powermetrics_sample_is_usable(samples[0]))

    def test_mock_report_contains_anonymized_core_sections(self) -> None:
        args = argparse.Namespace(mock=True, live=False, interval=1.0)
        data = asmond.report_data(args)
        self.assertEqual(data["app"], "Asmond")
        self.assertIn("diagnostics", data)
        self.assertIn("snapshot", data)
        self.assertEqual(data["snapshot"]["usb_c"]["active_port"], "MagSafe")
        self.assertEqual(data["snapshot"]["usb_c"]["input_type"], "MagSafe")
        self.assertNotIn(str(Path.home()), data["settings_path"])

    def test_mock_core_labels_match_apple_global_cpu_ids(self) -> None:
        stream = asmond.MockStream(100)
        sample = next(stream.samples())

        self.assertEqual(
            [core.label for core in sample.cores],
            ["P6", "P7", "P8", "P9", "E0", "E1", "E2", "E3", "E4", "E5"],
        )

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

    def test_live_report_anonymizes_powermetrics_warning_in_snapshot_and_diagnostics(self) -> None:
        old_home = asmond.real_user_home
        try:
            asmond.real_user_home = lambda: Path("/Users/alice")
            sample = asmond.MetricSample(warning="failed at /Users/alice/Private/job\x1b")
            snapshot = asmond.sample_to_report_dict(sample)
            rows = asmond.diagnostic_rows_for_report(
                argparse.Namespace(mock=False, live=True, interval=1.0),
                sample,
            )
        finally:
            asmond.real_user_home = old_home

        self.assertEqual(snapshot["warning"], "failed at ~/Private/job?")
        sample_row = next(row for row in rows if row[0] == "powermetrics sample")
        self.assertEqual(sample_row, ("powermetrics sample", "fail", "failed at ~/Private/job?"))

    def test_live_report_distinguishes_partial_from_unusable_powermetrics_sample(self) -> None:
        partial = asmond.sample_from_plist(
            {
                "processor": {"cpu_energy": 1000},
                "gpu": {"invalid": True},
            },
            interval_s=1.0,
        )
        invalid = asmond.sample_from_plist(
            {"invalid": True, "processor": {"cpu_energy": 1000}},
            interval_s=1.0,
        )
        args = argparse.Namespace(mock=False, live=True, interval=1.0)

        partial_row = next(
            row
            for row in asmond.diagnostic_rows_for_report(args, partial)
            if row[0] == "powermetrics sample"
        )
        invalid_row = next(
            row
            for row in asmond.diagnostic_rows_for_report(args, invalid)
            if row[0] == "powermetrics sample"
        )

        self.assertEqual(partial_row[1], "warn")
        self.assertIn("ignored 1 invalid block", partial_row[2])
        self.assertEqual(invalid_row[1], "fail")

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
            asmond.read_charge_stats = lambda *_: (asmond.BatteryStats(), asmond.UsbCStats())
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

    def test_anonymize_text_paths_handles_embedded_home_and_controls(self) -> None:
        old_home = asmond.real_user_home
        try:
            asmond.real_user_home = lambda: Path("/Users/alice")
            text = asmond.anonymize_text_paths("failed at /Users/alice/Library/x\nnext")
            self.assertEqual(text, "failed at ~/Library/x?next")
            self.assertEqual(asmond.anonymize_text_paths("/Users/alice2/x"), "/Users/alice2/x")
        finally:
            asmond.real_user_home = old_home

    def test_terminal_text_replaces_control_characters(self) -> None:
        self.assertEqual(asmond.sanitize_terminal_text("ok\x1b[31m\n"), "ok?[31m?")
        self.assertEqual(
            asmond.sanitize_terminal_text("x\u009b31m\u009dtitle\u009c"),
            "x?31m?title?",
        )
        self.assertEqual(asmond.sanitize_terminal_text("a\u2028b\u2029c"), "a?b?c")
        self.assertEqual(asmond.sanitize_terminal_text("left\u202eright"), "left?right")

    def test_probe_text_keeps_degree_in_utf8_and_falls_back_under_ascii(self) -> None:
        sample = asmond.MetricSample(soc_temp_c=40.0, temp_max_c=42.0)
        self.assertEqual(asmond.encoding_safe_text("40.0°C · CPU", "utf-8"), "40.0°C · CPU")

        raw = io.BytesIO()
        ascii_stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with contextlib.redirect_stdout(ascii_stream):
            asmond.print_sample(sample)
        ascii_stream.flush()
        output = raw.getvalue().decode("ascii")

        self.assertIn("Temp avg:     40.0 C", output)
        self.assertIn("Temp max:     42.0 C", output)
        self.assertNotIn("?C", output)

    def test_live_report_reuses_collected_powermetrics_sample(self) -> None:
        calls: list[list[str]] = []
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats
        payload = plistlib.dumps({"processor": {"cpu_power": 123}, "thermal_pressure": "Nominal"})

        def fake_run(command, **kwargs):
            if asmond.POWERMETRICS_PATH not in command:
                return old_run(command, **kwargs)
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, payload, b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda *_: (asmond.BatteryStats(), asmond.UsbCStats())
            args = argparse.Namespace(mock=False, live=True, interval=1.0)
            data = asmond.report_data(args)
            self.assertEqual(len(calls), 1)
            self.assertEqual(data["snapshot"]["powermetrics"]["cpu_power"], "123 mW")
            self.assertEqual(data["diagnostics"][-1]["name"], "powermetrics sample")
            self.assertEqual(data["diagnostics"][-1]["status"], "ok")
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure
            asmond.read_memory_stats = old_memory
            asmond.read_charge_stats = old_charge

    def test_live_intel_report_uses_mach_cpu_source(self) -> None:
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats
        old_cpu = asmond.read_cpu_load_snapshot
        payload = plistlib.dumps(
            {
                "processor": {
                    "package_watts": 6.0,
                    "cpu_watts": 3.0,
                    "packages": [
                        {
                            "cores_active_ratio": 0.90,
                            "cores": [
                                {"core": 0, "c_state_ratio": 0.1},
                                {"core": 1, "c_state_ratio": 0.1},
                            ],
                        }
                    ],
                }
            }
        )
        snapshots = iter(
            [
                asmond.CpuLoadSnapshot((100, 100, 100, 0)),
                asmond.CpuLoadSnapshot((110, 120, 170, 0)),
            ]
        )

        def fake_run(command, **kwargs):
            if asmond.POWERMETRICS_PATH not in command:
                return old_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, payload, b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda *_: (asmond.BatteryStats(), asmond.UsbCStats())
            asmond.read_cpu_load_snapshot = lambda: next(snapshots)
            args = argparse.Namespace(mock=False, live=True, interval=1.0)
            data = asmond.report_data(args)
            self.assertEqual(data["snapshot"]["powermetrics"]["cpu_usage"], "30%")
            self.assertEqual(data["snapshot"]["powermetrics"]["cpu_usage_source"], "mach")
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure
            asmond.read_memory_stats = old_memory
            asmond.read_charge_stats = old_charge
            asmond.read_cpu_load_snapshot = old_cpu

    def test_live_report_timeout_returns_warning_sample(self) -> None:
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_memory = asmond.read_memory_stats
        old_charge = asmond.read_charge_stats

        def fake_run(command, **kwargs):
            if asmond.POWERMETRICS_PATH not in command:
                return old_run(command, **kwargs)
            raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout"))

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.read_memory_stats = lambda: asmond.MemoryStats()
            asmond.read_charge_stats = lambda *_: (asmond.BatteryStats(), asmond.UsbCStats())
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

    def test_intel_probe_uses_mach_cpu_source_and_intel_labels(self) -> None:
        old_run = asmond.subprocess.run
        old_ensure = asmond.ensure_powermetrics_access
        old_cpu = asmond.read_cpu_load_snapshot
        payload = plistlib.dumps(
            {
                "processor": {
                    "package_watts": 6.0,
                    "cpu_watts": 3.0,
                    "igpu_watts": 0.1,
                    "freq_hz": 2_230_000_000,
                    "packages": [{"cores_active_ratio": 0.9}],
                },
                "smc": {"cpu_die": 79.2, "fan": 1200},
            }
        )
        snapshots = iter(
            [
                asmond.CpuLoadSnapshot((100, 100, 100, 0)),
                asmond.CpuLoadSnapshot((110, 120, 170, 0)),
            ]
        )

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, payload, b"")

        try:
            asmond.subprocess.run = fake_run
            asmond.ensure_powermetrics_access = lambda args: None
            asmond.read_cpu_load_snapshot = lambda: next(snapshots)
            args = argparse.Namespace(mock=False, interval=1.0, raw=False)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(asmond.SMC_TEMPS, "read", return_value=asmond_smc.SMCTemperatureSample()):
                self.assertEqual(asmond.run_probe(args), 0)
        finally:
            asmond.subprocess.run = old_run
            asmond.ensure_powermetrics_access = old_ensure
            asmond.read_cpu_load_snapshot = old_cpu

        text = output.getvalue()
        self.assertIn("Package:      6.00 W", text)
        self.assertIn("CPU usage:    30%  2.23 GHz", text)
        self.assertIn("Temp avg:     79.2°C", text)
        self.assertIn("Fan:          1200 rpm", text)
        self.assertNotIn("\nP usage:", text)
        self.assertNotIn("\nE usage:", text)

    def test_mock_flag_is_accepted_after_subcommands(self) -> None:
        for argv in (["probe", "--mock"], ["doctor", "--mock"], ["report", "--mock"]):
            with self.subTest(argv=argv):
                args = asmond.build_parser().parse_args(argv)
                self.assertTrue(args.mock)

    def test_probe_raw_help_says_values_are_not_anonymized(self) -> None:
        parser = asmond.build_parser()
        probe_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        help_text = probe_action.choices["probe"].format_help()
        normalized_help = " ".join(help_text.split())
        self.assertIn("keys and values", normalized_help)
        self.assertIn("not anonymized", normalized_help)

    def test_mock_live_report_labels_demo_sample(self) -> None:
        args = argparse.Namespace(mock=True, live=True, interval=1.0)
        data = asmond.report_data(args)
        self.assertEqual(data["diagnostics"][-1], {"name": "mock sample", "status": "ok", "note": "generated demo snapshot"})

    def test_print_sample_uses_effective_total_power(self) -> None:
        sample = asmond.MetricSample(telemetry_source="apple", cpu_power_mw=100, gpu_power_mw=50, fan_rpm=1196)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asmond.print_sample(sample)
        self.assertIn("SoC/Total:    150 mW", output.getvalue())
        self.assertIn("Fan:          1196 rpm", output.getvalue())

    def test_probe_and_report_keep_performance_limits_separate_from_throttling(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "thermal_pressure": "Nominal",
                "processor": {
                    "package_watts": 5.0,
                    "packages": [
                        {"perf_limit_reasons": ["CPU LIMIT THERMAL", "CPU LIMIT TURBO_ATTENUATION"]}
                    ],
                },
            }
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asmond.print_sample(sample)

        text = output.getvalue()
        self.assertIn("Thermal:      Nominal", text)
        self.assertIn("Throttled:    False", text)
        self.assertIn("Perf reasons: CPU LIMIT THERMAL, CPU LIMIT TURBO_ATTENUATION", text)
        self.assertNotIn("Throttle:     CPU LIMIT", text)

        report = asmond.sample_to_report_dict(sample)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertFalse(report["throttled"])
        self.assertEqual(report["throttle_reasons"], [])
        self.assertEqual(
            report["performance_limit_reasons"],
            ["CPU LIMIT THERMAL", "CPU LIMIT TURBO_ATTENUATION"],
        )


class PackagingTests(unittest.TestCase):
    def test_intel_collector_redacts_process_task_names_and_pids(self) -> None:
        script_path = Path(__file__).parent / "tools" / "collect-intel-mac-research.sh"
        script = script_path.read_text(encoding="utf-8")
        function = script.split("sanitize_text() {", 1)[1].split("\n}\n\nsanitize_plist()", 1)[0]
        sanitizer = function.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        task_text = (
            "proc_pidpath 735 failed(0)\n"
            "mdsync 82135 410.38 11.58 0.00 0.00 11.80 3.93 0.00 15.62\n"
            "ALL_TASKS -2 500.00 20.00 1.00 2.00 3.00 4.00 5.00 6.00\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            target = Path(tmp) / "target.txt"
            source.write_text(task_text, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-", str(source), str(target), "process-tasks"],
                input=sanitizer,
                text=True,
                check=False,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            redacted = target.read_text(encoding="utf-8")

        self.assertIn("proc_pidpath <pid> failed(0)", redacted)
        self.assertIn("process_001 1001 410.38", redacted)
        self.assertIn("all_tasks -2 500.00", redacted)
        self.assertNotIn("mdsync", redacted)
        self.assertNotIn("82135", redacted)
        self.assertNotIn("735", redacted)

    def test_intel_collector_redacts_boot_arguments_in_default_text_mode(self) -> None:
        script_path = Path(__file__).parent / "tools" / "collect-intel-mac-research.sh"
        script = script_path.read_text(encoding="utf-8")
        function = script.split("sanitize_text() {", 1)[1].split("\n}\n\nsanitize_plist()", 1)[0]
        sanitizer = function.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            target = Path(tmp) / "target.txt"
            source.write_text(
                "Boot arguments: keepsyms=1 api_token=private-value\n"
                "proc_pidpath 735 failed(0)\n"
                "OS version: 23J30\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "-", str(source), str(target), "text"],
                input=sanitizer,
                text=True,
                check=False,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            redacted = target.read_text(encoding="utf-8")

        self.assertEqual(
            redacted,
            "Boot arguments: <redacted>\nproc_pidpath <pid> failed(0)\nOS version: 23J30\n",
        )
        self.assertNotIn("private-value", redacted)
        self.assertNotIn("735", redacted)

    def test_intel_collector_tui_harness_supports_timed_soak(self) -> None:
        script_path = Path(__file__).parent / "tools" / "collect-intel-mac-research.sh"
        script = script_path.read_text(encoding="utf-8")
        function = script.split("run_tui_smoke() {", 1)[1].split("\n}\n\ncat >", 1)[0]
        harness = function.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            fake_tui = target / "fake_tui.py"
            status_path = target / "status.txt"
            fake_tui.write_text(
                """
import select
import sys
import time
import tty

tty.setcbreak(sys.stdin.fileno())
counter = 0
while True:
    print(f"last sample 12:00:{counter % 60:02d}", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
    if ready and sys.stdin.read(1) == "q":
        break
    counter += 1
    time.sleep(0.01)
""".lstrip(),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "-", str(status_path), "1", sys.executable, str(fake_tui)],
                input=harness,
                text=True,
                check=False,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            values = dict(
                line.split("=", 1)
                for line in status_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )

        self.assertEqual(values["exit_code"], "0")
        self.assertEqual(values["quit_sent"], "1")
        self.assertEqual(values["requested_seconds"], "1")
        self.assertEqual(values["traceback_seen"], "0")
        self.assertEqual(values["sudo_refresh_failed_seen"], "0")
        self.assertEqual(values["sampler_restart_seen"], "0")
        self.assertEqual(values["stale_seen"], "0")
        self.assertEqual(values["activity_windows_expected"], "1")
        self.assertEqual(values["activity_windows_seen"], "1")
        self.assertEqual(values["activity_windows_missed"], "0")
        self.assertGreaterEqual(int(values["sample_timestamps_seen"]), 2)
        self.assertGreater(int(values["bytes_seen"]), 0)

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
        self.assertEqual(proc.stdout, f"Asmond {asmond.VERSION}\n")

    def test_manual_version_matches_runtime(self) -> None:
        manual_header = (Path(__file__).parent / "man" / "asmond.1").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f'"Asmond {asmond.VERSION}"', manual_header)


class SettingsTests(unittest.TestCase):
    def test_custom_grid_profiles_round_trip_with_classic_legacy_copy(self) -> None:
        classic = {"upper_left": {"panel": "battery", "detail": "normal"}}
        balanced = {"cell_1": {"panel": "cable", "detail": "compact"}}
        columns = {"cell_6": {"panel": "process", "detail": "detail"}}
        args = argparse.Namespace(
            theme="classic", interval=1.0, layout="custom", show_io=False,
            custom_grid="balanced", custom_layout=balanced,
            custom_layouts={"classic": classic, "columns": columns},
            custom_slot="cell_1", custom_name="My desk",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(settings_path, args, asmond.settings_schema()))
            raw = json.loads(settings_path.read_text())
            self.assertEqual(raw["custom_layout"], classic)
            self.assertEqual(raw["custom_layouts"], {"classic": classic, "balanced": balanced, "columns": columns})
            restored = asmond_settings.load_settings(settings_path, asmond.settings_schema())
            self.assertEqual(restored["custom_grid"], "balanced")
            self.assertEqual(restored["custom_layout"], balanced)
            self.assertEqual(restored["custom_layouts"], raw["custom_layouts"])
            self.assertEqual(restored["custom_slot"], "cell_1")
            with mock.patch.object(asmond, "SETTINGS_PATH", settings_path):
                parsed = asmond.build_parser().parse_args(["--mock", "--layout", "custom"])
            self.assertEqual(parsed.custom_grid, "balanced")
            self.assertEqual(parsed.custom_layout, balanced)
            self.assertEqual(parsed.custom_name, "My desk")

    def test_legacy_custom_layout_migrates_to_classic_without_losing_assignments(self) -> None:
        classic = {"right_middle": {"panel": "process", "detail": "detail"}}
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "settings.json"
            settings_path.write_text(json.dumps({"layout": "custom", "custom_layout": classic, "custom_name": "Old desk"}))
            with mock.patch.object(asmond, "SETTINGS_PATH", settings_path):
                args = asmond.build_parser().parse_args(["--mock"])
            self.assertEqual(args.custom_grid, "classic")
            self.assertEqual(args.custom_layout, classic)
            self.assertIsNone(asmond_settings.save_settings(settings_path, args, asmond.settings_schema()))
            restored = asmond_settings.load_settings(settings_path, asmond.settings_schema())
            self.assertEqual(restored["custom_layouts"]["classic"], classic)
            self.assertEqual(restored["custom_name"], "Old desk")

    def test_malformed_grid_profiles_fall_back_without_exposing_invalid_slots(self) -> None:
        legacy = {"upper_left": {"panel": "ram", "detail": "compact"}}
        invalid = {
            "custom_grid": ["columns"], "custom_layout": legacy, "custom_slot": "cell_9",
            "custom_layouts": {
                "classic": [],
                "balanced": {"cell_1": {"panel": "battery", "detail": "invalid"}, "cell_6": {"panel": "process"}},
                "unknown": {"cell_1": {"panel": "ram"}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "settings.json"
            settings_path.write_text(json.dumps(invalid))
            restored = asmond_settings.load_settings(settings_path, asmond.settings_schema())
        self.assertEqual(restored["custom_grid"], "classic")
        self.assertEqual(restored["custom_layout"], legacy)
        self.assertEqual(restored["custom_layouts"]["balanced"], {"cell_1": {"panel": "battery", "detail": "normal"}})
        self.assertNotIn("custom_slot", restored)
        self.assertNotIn("unknown", restored["custom_layouts"])

    def test_independent_charge_blocks_and_legacy_slots_round_trip(self) -> None:
        custom = {
            "upper_left": {"panel": "battery", "detail": "normal"},
            "upper_right": {"panel": "power_input", "detail": "detail"},
            "right_lower": {"panel": "cable", "detail": "compact"},
            "right_middle": {"panel": "charge", "detail": "normal"},
        }
        args = argparse.Namespace(
            theme="classic", interval=1.0, layout="custom", show_io=False,
            custom_layout=custom, custom_name="Charge desk", custom_slot="upper_right",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(settings_path, args, asmond.settings_schema()))
            restored = asmond_settings.load_settings(settings_path, asmond.settings_schema())
            self.assertEqual(restored["custom_layout"], custom)
            self.assertEqual(restored["custom_name"], "Charge desk")
            self.assertEqual(restored["custom_slot"], "upper_right")

    def test_rss_process_sort_survives_settings_round_trip(self) -> None:
        args = argparse.Namespace(theme="classic", interval=1.0, layout="full", show_io=False, process_sort="rss")
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(settings_path, args, asmond.settings_schema()))
            self.assertEqual(asmond_settings.load_settings(settings_path, asmond.settings_schema())["process_sort"], "rss")

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
                        "rounded_borders": True,
                        "diagnostics_in_cycle": True,
                        "upper_power_mode": "gpu",
                        "lower_power_mode": "ane",
                        "upper_io_mode": "disk_write",
                        "lower_io_mode": "net_out",
                        "load_view": "graph",
                        "process_panel": "right",
                        "process_sort": "ram",
                        "charge_panel": "usb",
                        "graph_view": "power",
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
            "88.0°C",
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

    def test_root_ui_never_writes_settings(self) -> None:
        old_root = asmond.is_root_process
        old_save = asmond._save_settings
        calls: list[Path] = []
        try:
            asmond.is_root_process = lambda: True
            asmond._save_settings = lambda path, args, schema: calls.append(path)
            error = asmond.save_settings(argparse.Namespace())
        finally:
            asmond.is_root_process = old_root
            asmond._save_settings = old_save
        self.assertIn("runs as root", error or "")
        self.assertEqual(calls, [])

    def test_extreme_or_nonfinite_settings_are_ignored_without_blocking_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "settings.json"
            path.write_text(
                '{"interval": 9999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999999, "alert_temp_c": NaN, "alert_swap_gib": true}',
                encoding="utf-8",
            )
            settings = asmond_settings.load_settings(path, asmond.settings_schema())
        self.assertNotIn("interval", settings)
        self.assertNotIn("alert_temp_c", settings)
        self.assertNotIn("alert_swap_gib", settings)
        self.assertIn("interval", settings["__warning__"])

    def test_save_settings_rejects_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "target"
            target.mkdir()
            link = root / "Asmond"
            link.symlink_to(target, target_is_directory=True)
            args = argparse.Namespace(
                theme="classic",
                interval=1.0,
                layout="full",
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
                custom_name="",
            )
            error = asmond_settings.save_settings(link / "settings.json", args, asmond.settings_schema())
            self.assertIsNotNone(error)
            self.assertFalse((target / "settings.json").exists())

    def test_save_settings_rejects_non_regular_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "settings.json"
            path.mkdir()
            args = argparse.Namespace(
                theme="classic",
                interval=1.0,
                layout="full",
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
                custom_name="",
            )
            error = asmond_settings.save_settings(path, args, asmond.settings_schema())
            self.assertIn("regular file", error or "")


class LayoutTests(unittest.TestCase):
    class RecordingWindow:
        def __init__(self, height: int = 100, width: int = 200) -> None:
            self.height = height
            self.width = width
            self.encoding = "utf-8"
            self.entries: list[tuple[int, int, str]] = []
            self.erase_calls = 0
            self.refresh_calls = 0

        def getmaxyx(self) -> tuple[int, int]:
            return self.height, self.width

        def attron(self, _attr: int) -> None:
            pass

        def attroff(self, _attr: int) -> None:
            pass

        def erase(self) -> None:
            self.erase_calls += 1
            self.entries.clear()

        def refresh(self) -> None:
            self.refresh_calls += 1

        def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
            self.entries.append((y, x, text))

        def rendered_row(self, y: int) -> str:
            cells = [" "] * self.width
            for entry_y, x, text in self.entries:
                if entry_y != y:
                    continue
                cells[x : x + len(text)] = text
            return "".join(cells).rstrip()

        def rendered(self) -> str:
            return "\n".join(self.rendered_row(y) for y in range(self.height))

    @staticmethod
    def dashboard_colors() -> dict[str, int]:
        colors = {name: 0 for name in ("accent", "fg", "muted", "good", "warn", "bad", "dim", "bold")}
        colors.update({f"graph_{index}": 0 for index in range(len(asmond.GRAPH_GRADIENT_COLORS))})
        return colors

    @staticmethod
    def dashboard_args(layout: str = "full", *, show_io: bool = True, custom_layout=None) -> argparse.Namespace:
        return argparse.Namespace(
            interval=1.0,
            theme="apple",
            layout=layout,
            show_io=show_io,
            mock=True,
            custom_layout=custom_layout or {},
            custom_name="",
            custom_slot="upper_left",
            alert_temp_c=85.0,
            alert_swap_gib=1.0,
            alert_battery_drain_w=15.0,
            allow_root_kill=False,
        )

    @staticmethod
    def dashboard_data():
        sample = next(asmond.MockStream(100).samples())
        sample.telemetry_source = "apple"
        sample.cpu_temp_avg_c = 43.2
        sample.cpu_temp_max_c = 46.1
        sample.cpu_temp_max_label = "E5"
        sample.gpu_temp_avg_c = 38.4
        sample.gpu_temp_max_c = 40.2
        sample.pmu_temp_avg_c = 31.0
        sample.power_supply_temp_c = 30.5
        sample.airport_temp_c = 31.2
        sample.trackpad_temp_c = 27.2
        sample.trackpad_actuator_temp_c = 26.1
        sample.temperature_source = "AppleSMC"
        sample.temp_max_source = "CPU@E5"
        sample.warning = "partial mock warning"
        history = asmond.History(128)
        for index in range(48):
            history.add(sample, now=float(index))
            history.add_io(asmond.mock_io_stats(float(index)))
        return (
            sample,
            history,
            asmond.mock_memory_stats(4.0),
            asmond.mock_battery_stats(4.0),
            asmond.mock_usb_c_stats(4.0),
            asmond.mock_io_stats(4.0),
            asmond.mock_processes(4.0),
        )

    def draw_test_dashboard(
        self,
        window,
        args,
        *,
        process_panel: str = "hidden",
        charge_panel: str = "battery",
        help_visible: bool = False,
        menu_visible: bool = False,
        tailor_visible: bool = False,
        tailor_menu_visible: bool = False,
    ) -> None:
        sample, history, memory, battery, usb_c, io_stats, processes = self.dashboard_data()
        asmond.draw_dashboard(
            window,
            sample,
            history,
            memory,
            battery,
            usb_c,
            io_stats,
            processes,
            self.dashboard_colors(),
            args,
            "live",
            "soc",
            "cpu",
            "disk_read",
            "net_in",
            "rows",
            process_panel,
            "cpu",
            charge_panel,
            1,
            None,
            ("sensor retry",),
            help_visible,
            menu_visible,
            0,
            tailor_visible,
            tailor_menu_visible,
            0,
            "LLM view",
            False,
        )

    def test_complete_full_dashboard_renders_live_sections(self) -> None:
        window = self.RecordingWindow(61, 181)

        self.draw_test_dashboard(window, self.dashboard_args())

        rendered = window.rendered()
        for text in (
            "POWER GRAPH",
            "POWER",
            "THERMALS",
            "CPU / GPU LOAD",
            "CLOCKS",
            "RAM",
            "BATTERY",
            "CPU@E5",
            "30s avg",
            "partial mock warning",
        ):
            self.assertIn(text, rendered)
        self.assertEqual(window.erase_calls, 1)
        self.assertEqual(window.refresh_calls, 1)

    def test_compact_dashboard_draws_the_reserved_power_graph_and_keeps_lower_panels(self) -> None:
        window = self.RecordingWindow(32, 100)
        self.draw_test_dashboard(window, self.dashboard_args("compact"))
        layout = asmond.dashboard_layout(32, 100, "compact", True, "hidden")
        graph = layout.slot("top").rect
        self.assertEqual(graph.h, 7)
        self.assertIn("POWER GRAPH", window.rendered_row(graph.y))
        self.assertTrue(any(0x2800 <= ord(char) <= 0x28FF for y in range(graph.y + 1, graph.y + graph.h - 1) for char in window.rendered_row(y)))
        self.assertEqual(layout.slot("upper_left").rect.h, 5)
        for text in ("CPU / GPU LOAD", "CLOCKS", "RAM", "BATTERY"):
            self.assertIn(text, window.rendered())
        self.assertEqual(layout.slot("lower_left").rect.y, 14)

    def test_new_custom_grids_render_all_assigned_areas(self) -> None:
        for grid, width, expected in (
            ("balanced", 120, ("POWER", "THERMALS", "CPU / GPU LOAD", "RAM")),
            ("columns", 181, ("POWER", "THERMALS", "RAM", "CPU / GPU LOAD", "CLOCKS", "BATTERY")),
            ("columns", 100, ("POWER", "THERMALS", "RAM", "CPU / GPU LOAD", "CLOCKS", "BATTERY")),
        ):
            with self.subTest(grid=grid, width=width):
                args = self.dashboard_args("custom", show_io=False)
                args.custom_grid = grid
                args.custom_slot = "cell_1"
                window = self.RecordingWindow(61, width)
                self.draw_test_dashboard(window, args)
                layout = asmond.dashboard_layout(61, width, "custom", False, "hidden", custom_grid=grid)
                self.assertEqual(len(layout.panels), len(expected) + 1)
                for index, title in enumerate(expected, 1):
                    rect = layout.slot(f"cell_{index}").rect
                    self.assertIn(title, window.rendered_row(rect.y)[rect.x:rect.x + rect.w])
                self.assertEqual(window.refresh_calls, 1)

    def test_narrow_grid_thermal_blocks_keep_complete_temperatures_and_sources(self) -> None:
        sample = self.dashboard_data()[0]
        for width in (48, 60, 72):
            with self.subTest(width=width):
                window = self.RecordingWindow(24, width)
                placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 24, width), "normal")
                asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), self.dashboard_colors())
                rendered = window.rendered()
                for value in ("31.0°C", "30.5°C", "31.2°C", "27.2°C", "26.1°C"):
                    self.assertIn(value, rendered)
                self.assertIn("E5", rendered)

    def test_global_grid_menu_describes_current_raster_and_narrow_fallback(self) -> None:
        for grid, width, shape in (("classic", 100, "7 slots"), ("balanced", 120, "2 columns · 2 rows"), ("columns", 181, "3 columns · 2 rows"), ("columns", 100, "2 columns · 3 rows")):
            with self.subTest(grid=grid, width=width):
                args = self.dashboard_args("custom")
                args.custom_grid = grid
                window = self.RecordingWindow(40, width)
                layout = asmond.dashboard_layout(40, width, "custom", False, "hidden", custom_grid=grid)
                asmond.draw_tailor_overlay(window, layout, self.dashboard_colors(), args, True, 0, "", False)
                self.assertIn("Layout grid", window.rendered())
                self.assertIn(asmond_layout.CUSTOM_GRID_SPECS[grid].title, window.rendered())
                self.assertIn(shape, window.rendered())

    def test_grid_profiles_restore_separate_slot_assignments(self) -> None:
        classic = {"upper_left": {"panel": "battery", "detail": "normal"}}
        profiles, active = asmond_layout.switch_custom_grid("classic", classic, {}, "balanced")
        self.assertEqual(active, {})
        balanced = asmond_layout.update_custom_slot(active, "cell_1", "cable", "compact", "balanced")
        profiles, active = asmond_layout.switch_custom_grid("balanced", balanced, profiles, "columns")
        columns = asmond_layout.update_custom_slot(active, "cell_1", "process", grid="columns")
        profiles, restored = asmond_layout.switch_custom_grid("columns", columns, profiles, "classic")
        self.assertEqual(restored, classic)
        profiles, restored = asmond_layout.switch_custom_grid("classic", restored, profiles, "balanced")
        self.assertEqual(restored, balanced)
        self.assertEqual(profiles["columns"], columns)
        self.assertEqual(classic, {"upper_left": {"panel": "battery", "detail": "normal"}})

    def test_grid_slot_numbers_and_polling_follow_the_selected_grid(self) -> None:
        self.assertEqual(asmond_layout.custom_grid_slot_ids("classic"), asmond.CUSTOM_SLOT_IDS)
        for grid, count in (("balanced", 4), ("columns", 6)):
            self.assertEqual(asmond.tailor_slot_number("cell_1", grid), 1)
            self.assertEqual(asmond.tailor_slot_for_key(ord(str(count)), grid), f"cell_{count}")
            self.assertIsNone(asmond.tailor_slot_for_key(ord(str(count + 1)), grid))
            args = self.dashboard_args("custom", show_io=True)
            args.custom_grid = grid
            self.assertFalse(asmond.layout_uses_io(args))
            self.assertFalse(asmond.layout_uses_process(args, "hidden"))
            args.custom_layout = {"cell_1": {"panel": "io", "detail": "normal"}, "cell_2": {"panel": "process", "detail": "detail"}}
            self.assertTrue(asmond.layout_uses_io(args))
            self.assertTrue(asmond.layout_uses_process(args, "hidden"))

    def test_new_grid_geometry_keeps_all_cells_bounded_and_non_overlapping(self) -> None:
        for grid, count in (("balanced", 4), ("columns", 6)):
            for height in (24, 32, 48, 61):
                for width in (72, 100, 143, 144, 181):
                    with self.subTest(grid=grid, height=height, width=width):
                        layout = asmond.dashboard_layout(height, width, "custom", False, "hidden", custom_grid=grid)
                        self.assertEqual(len(layout.panels), count + 1)
                        for placement in layout.panels:
                            rect = placement.rect
                            self.assertGreaterEqual(rect.x, 0)
                            self.assertGreaterEqual(rect.y, 2)
                            self.assertGreaterEqual(rect.h, 3)
                            self.assertLessEqual(rect.x + rect.w, width)
                            self.assertLessEqual(rect.y + rect.h, height - 1)
                        for index, placement in enumerate(layout.panels):
                            left = placement.rect
                            for other in layout.panels[index + 1:]:
                                right = other.rect
                                self.assertFalse(left.x < right.x + right.w and right.x < left.x + left.w and left.y < right.y + right.h and right.y < left.y + left.h)

    def test_full_dashboard_process_panel_renders_real_process_rows(self) -> None:
        window = self.RecordingWindow(61, 181)

        self.draw_test_dashboard(
            window,
            self.dashboard_args(show_io=False),
            process_panel="right",
            charge_panel="usb",
        )

        rendered = window.rendered()
        self.assertIn("PROCESSES", rendered)
        self.assertIn("WindowServer", rendered)
        self.assertIn("CPU%", rendered)
        self.assertIn("POWER INPUT", rendered)

    def test_process_sort_highlight_follows_visible_column_and_gpu_fallback(self) -> None:
        class AttributeWindow(self.RecordingWindow):
            def __init__(self) -> None:
                super().__init__()
                self.attributes: dict[tuple[int, str], int] = {}

            def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
                super().addstr(y, x, text, attr)
                self.attributes[y, text] = attr

        processes = [asmond.ProcessInfo(12, 3.0, 4.0, 500, "demo")]
        colors = self.dashboard_colors()
        colors["warn"] = 1
        for sort_key, expected in (("pid", "PID"), ("cpu", "CPU%"), ("ram", "RAM%"), ("rss", "RSS"), ("name", "COMMAND"), ("gpu", "CPU%")):
            with self.subTest(sort_key=sort_key):
                window = AttributeWindow()
                asmond.draw_process_section(window, 0, 0, 15, 90, processes, sort_key, 0, None, colors)
                self.assertNotIn((1, "GPU%"), window.attributes)
                highlighted = [
                    label
                    for label in ("PID", "CPU%", "RAM%", "RSS", "COMMAND")
                    if window.attributes[1, label] == colors["warn"] | asmond.curses.A_BOLD
                ]
                self.assertEqual(highlighted, [expected])
                if sort_key == "gpu":
                    self.assertIn("sort cpu", window.rendered_row(0))

        processes[0].gpu_pct = 0.0
        window = AttributeWindow()
        asmond.draw_process_section(window, 0, 0, 15, 90, processes, "gpu", 0, None, colors)
        self.assertEqual(window.attributes[1, "GPU%"], colors["warn"] | asmond.curses.A_BOLD)

    def test_focus_dashboard_renders_three_charge_views_when_wide(self) -> None:
        window = self.RecordingWindow(48, 180)

        self.draw_test_dashboard(window, self.dashboard_args("focus", show_io=False), charge_panel="cable")

        rendered = window.rendered()
        self.assertIn("BATTERY", rendered)
        self.assertIn("POWER INPUT", rendered)
        self.assertIn("CABLE", rendered)
        self.assertIn("USB4 Gen 3", rendered)
        self.assertIn("USB 3.2 Gen 2 · 10 Gb/s", rendered)

    def test_focus_dashboard_renders_narrow_stacked_charge_views(self) -> None:
        for charge_panel, expected in (("usb", "POWER INPUT"), ("cable", "CABLE")):
            with self.subTest(charge_panel=charge_panel):
                window = self.RecordingWindow(48, 90)
                self.draw_test_dashboard(
                    window,
                    self.dashboard_args("focus", show_io=False),
                    charge_panel=charge_panel,
                )
                rendered = window.rendered()
                self.assertIn("BATTERY", rendered)
                self.assertIn(expected, rendered)

    def test_custom_dashboard_renders_tailor_editor_and_menu(self) -> None:
        custom_layout = {"right_middle": {"panel": "process", "detail": "detail"}}
        args = self.dashboard_args("custom", show_io=True, custom_layout=custom_layout)
        args.custom_name = "LLM view"
        window = self.RecordingWindow(61, 181)

        self.draw_test_dashboard(
            window,
            args,
            tailor_visible=True,
            tailor_menu_visible=True,
        )

        rendered = window.rendered()
        self.assertIn("TAILOR", rendered)
        self.assertIn("Custom panel", rendered)
        self.assertIn("LLM view", rendered)
        self.assertIn("PROCESSES", rendered)

    def test_custom_dashboard_keeps_independent_charge_sources(self) -> None:
        custom = {
            "upper_left": {"panel": "battery", "detail": "normal"},
            "upper_right": {"panel": "power_input", "detail": "normal"},
            "right_lower": {"panel": "cable", "detail": "normal"},
        }
        args = self.dashboard_args("custom", show_io=False, custom_layout=custom)
        for charge_mode in ("battery", "usb", "cable"):
            with self.subTest(charge_mode=charge_mode):
                window = self.RecordingWindow(61, 181)
                self.draw_test_dashboard(window, args, charge_panel=charge_mode)
                layout = asmond.dashboard_layout(61, 181, "custom", False, "hidden", custom, charge_mode)
                for slot_id, title, value in (
                    ("upper_left", "BATTERY", "Health"),
                    ("upper_right", "POWER INPUT", "MagSafe"),
                    ("right_lower", "CABLE", "USB4 Gen 3"),
                ):
                    rect = layout.slot(slot_id).rect
                    content = "\n".join(window.rendered_row(y)[rect.x:rect.x + rect.w] for y in range(rect.y, rect.y + rect.h))
                    self.assertIn(title, content)
                    self.assertIn(value, content)

    def test_fixed_charge_blocks_show_missing_source_without_fallback(self) -> None:
        for panel_id, title, message in (
            ("battery", "BATTERY", "no battery telemetry"),
            ("power_input", "POWER INPUT", "no charge-input telemetry"),
            ("cable", "CABLE", "no public cable telemetry"),
        ):
            with self.subTest(panel_id=panel_id):
                window = self.RecordingWindow()
                placement = asmond.PanelPlacement(panel_id, asmond.Rect(0, 0, 8, 90), "normal")
                asmond.draw_charge_panel(
                    window, placement, "usb", asmond.BatteryStats(), asmond.UsbCStats(), self.dashboard_colors(),
                )
                self.assertIn(title, window.rendered_row(0))
                self.assertIn(message, window.rendered_row(1))

    def test_every_selectable_tailor_block_renders_its_registered_content(self) -> None:
        sample, history, memory, battery, usb_c, io_stats, processes = self.dashboard_data()
        for panel_id in asmond.CUSTOM_PANEL_IDS:
            with self.subTest(panel_id=panel_id):
                window = self.RecordingWindow(25, 100)
                spec = asmond.PANEL_SPECS[panel_id]
                placement = asmond.PanelPlacement(panel_id, asmond.Rect(0, 0, 24, 100), spec.default_detail)
                asmond.draw_dashboard_panel(
                    window, placement, sample, history, memory, battery, usb_c, io_stats,
                    processes, self.dashboard_colors(), self.dashboard_args("custom"),
                    "soc", "cpu", "disk_read", "net_in", "rows", "cpu", "battery", 0, None, False,
                )
                title = "BATTERY" if panel_id == "charge" else spec.title.upper()
                self.assertIn(title, window.rendered_row(0))
                content = "\n".join(window.rendered_row(y).strip("│ ") for y in range(1, 23))
                self.assertTrue(content.strip())
        self.assertNotIn("power_graph", asmond.CUSTOM_PANEL_IDS)

    def test_tailor_editor_uses_friendly_names_and_reports_hidden_and_small_slots(self) -> None:
        for slot_id, panel_id, expected in (
            ("right_bottom", "io", ("Disk / Net", "normal (fixed)", "hidden at this size", "Slot hidden")),
            ("upper_left", "process", ("Processes", "detail (fixed)", "40x7", "Small slot")),
        ):
            with self.subTest(slot_id=slot_id):
                # Leave the optional bottom slot unconfigured so it is genuinely hidden.
                custom = {} if slot_id == "right_bottom" else {slot_id: {"panel": panel_id, "detail": "detail"}}
                args = self.dashboard_args("custom", show_io=False, custom_layout=custom)
                args.custom_slot = slot_id
                layout = asmond.dashboard_layout(40, 80, "custom", False, "hidden", custom)
                window = self.RecordingWindow(40, 80)
                asmond.draw_tailor_overlay(window, layout, self.dashboard_colors(), args, True, 0, "", False)
                for text in expected:
                    self.assertIn(text, window.rendered())

    def test_dashboard_waiting_and_small_terminal_paths_refresh(self) -> None:
        sample, history, memory, battery, usb_c, io_stats, processes = self.dashboard_data()
        colors = self.dashboard_colors()
        args = self.dashboard_args()
        small = self.RecordingWindow(20, 70)
        waiting = self.RecordingWindow(40, 100)

        asmond.draw_dashboard(
            small,
            sample,
            history,
            memory,
            battery,
            usb_c,
            io_stats,
            processes,
            colors,
            args,
            "live",
            "soc",
            "cpu",
            "disk_read",
            "net_in",
            "rows",
            "hidden",
            "cpu",
            "battery",
            0,
            None,
            (),
            False,
            False,
            0,
            False,
            False,
            0,
            "",
            False,
        )
        asmond.draw_dashboard(
            waiting,
            None,
            history,
            memory,
            battery,
            usb_c,
            io_stats,
            processes,
            colors,
            args,
            "starting",
            "soc",
            "cpu",
            "disk_read",
            "net_in",
            "rows",
            "hidden",
            "cpu",
            "battery",
            0,
            None,
            (),
            True,
            True,
            0,
            False,
            False,
            0,
            "",
            False,
        )

        self.assertIn("terminal too small", small.rendered())
        self.assertIn("waiting for first powermetrics sample", [text for _y, _x, text in waiting.entries])
        self.assertTrue(any("HELP" in text for _y, _x, text in waiting.entries))
        self.assertIn("MENU", waiting.rendered())
        self.assertEqual(small.refresh_calls, 1)
        self.assertEqual(waiting.refresh_calls, 1)

    def test_power_input_panel_does_not_guess_usb_c_without_correlated_input(self) -> None:
        usb_c = asmond.usb_c_stats_from_item(
            {
                "ExternalConnected": True,
                "PortControllerInfo": [{}],
                "FedDetails": [{}],
            },
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "ConnectionActive": 1,
                    "TransportsActive": ["USB3"],
                }
            ],
        )
        self.assertIsNone(usb_c.active_port)
        self.assertFalse(asmond.charge_input_active(usb_c))

        window = self.RecordingWindow()
        colors = {"good": 0, "muted": 0, "fg": 0, "warn": 0}
        asmond.draw_usb_c_section(window, 0, 0, 8, 50, usb_c, colors)

        self.assertIn("Input", window.rendered_row(1))
        self.assertIn("n/a", window.rendered_row(1))
        self.assertNotIn("USB-C", window.rendered_row(1))

    def test_power_input_panel_uses_the_same_unknown_state_as_report(self) -> None:
        usb_c = asmond.usb_c_stats_from_item(
            {},
            [
                {
                    "PortDescription": "Port-USB-C@1",
                    "PortType": 2,
                    "PortNumber": 1,
                    "ConnectionActive": False,
                }
            ],
        )
        self.assertEqual(asmond.usb_to_report_dict(usb_c)["state"], "unknown")

        window = self.RecordingWindow()
        colors = {"good": 0, "muted": 0, "fg": 0, "warn": 0}
        asmond.draw_usb_c_section(window, 0, 0, 8, 50, usb_c, colors)

        self.assertIn("State", window.rendered_row(0))
        self.assertIn("unknown", window.rendered_row(0))

    def test_box_uses_one_complete_unicode_border_or_one_complete_acs_fallback(self) -> None:
        class BoxWindow:
            def __init__(self) -> None:
                self.characters: list[tuple[int, int, object]] = []
                self.strings: list[tuple[int, int, str]] = []
                self.encoding = "utf-8"

            def getmaxyx(self) -> tuple[int, int]:
                return 20, 40

            def attron(self, _attr: int) -> None:
                pass

            def attroff(self, _attr: int) -> None:
                pass

            def hline(self, *_args) -> None:
                pass

            def vline(self, *_args) -> None:
                pass

            def addch(self, y: int, x: int, char: object) -> None:
                self.characters.append((y, x, char))

            def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
                self.strings.append((y, x, text))

        window = BoxWindow()
        acs_names = ("ACS_HLINE", "ACS_VLINE", "ACS_ULCORNER", "ACS_URCORNER", "ACS_LLCORNER", "ACS_LRCORNER")
        patches = [mock.patch.object(asmond.curses, name, index, create=True) for index, name in enumerate(acs_names, 1)]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            asmond.draw_box(window, 1, 2, 5, 10, "TEST", 0)

        self.assertEqual(window.characters, [])
        self.assertIn((1, 2, "╭────────╮"), window.strings)
        self.assertIn((5, 2, "╰────────╯"), window.strings)
        self.assertEqual(sum(text == "│" for _y, _x, text in window.strings), 6)

        square = BoxWindow()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            unicode_border = stack.enter_context(mock.patch.object(asmond, "draw_unicode_box_border"))
            asmond.draw_box(square, 1, 2, 5, 10, "TEST", 0, rounded=False)
        unicode_border.assert_not_called()
        self.assertEqual(len(square.characters), 4)
        self.assertEqual(square.strings, [(1, 4, " TEST ")])

        class FallbackBoxWindow(BoxWindow):
            encoding = "ascii"

            def __init__(self) -> None:
                super().__init__()
                self.encoding = "ascii"

            def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
                super().addstr(y, x, text, _attr)

        fallback = FallbackBoxWindow()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            asmond.draw_box(fallback, 1, 2, 5, 10, "TEST", 0)

        self.assertEqual(len(fallback.characters), 4)
        self.assertFalse(any(any(glyph in text for glyph in asmond.ROUNDED_BOX_GLYPHS) for _y, _x, text in fallback.strings))

    def test_clock_panel_rows_keep_apple_silicon_cluster_labels(self) -> None:
        sample = asmond.MetricSample(
            telemetry_source="apple",
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

    def test_thermals_panel_displays_raw_performance_limits_without_false_throttling(self) -> None:
        sample = asmond.sample_from_plist(
            {
                "thermal_pressure": "Nominal",
                "processor": {
                    "package_watts": 5.0,
                    "packages": [
                        {"perf_limit_reasons": ["CPU LIMIT THERMAL", "CPU LIMIT TURBO_ATTENUATION"]}
                    ],
                },
            }
        )
        window = self.RecordingWindow()
        placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 7, 100), "normal")
        colors = {"bad": 0, "good": 0, "warn": 0, "muted": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), colors)
        finally:
            asmond.draw_box = old_draw_box

        rendered = "\n".join(window.rendered_row(y) for y in range(7))
        self.assertIn("Pressure", rendered)
        self.assertIn("Nominal", rendered)
        self.assertIn("Throttled", rendered)
        self.assertIn("no", rendered)
        self.assertIn("Perf reason", rendered)
        self.assertIn("CPU LIMIT THERMAL, CPU LIMIT TURBO_ATTENUATION", rendered)
        self.assertNotIn("Throttle    CPU LIMIT", rendered)

    def test_thermals_panel_shows_verified_hotspot_and_physical_sensors(self) -> None:
        sample = asmond.MetricSample(
            telemetry_source="apple",
            cpu_temp_avg_c=41.5,
            cpu_temp_max_c=45.5,
            cpu_temp_max_label="E5",
            gpu_temp_avg_c=38.2,
            gpu_temp_max_c=42.0,
            pmu_temp_avg_c=31.8,
            pmu_temp_max_c=35.3,
            airport_temp_c=31.6,
            power_supply_temp_c=31.5,
            trackpad_temp_c=27.0,
            trackpad_actuator_temp_c=25.8,
            soc_temp_c=41.5,
            temp_max_c=45.5,
            thermal_pressure="Nominal",
            throttled=False,
        )
        window = self.RecordingWindow()
        placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 8, 50), "normal")
        colors = {"bad": 0, "good": 0, "warn": 0, "muted": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), colors)
        finally:
            asmond.draw_box = old_draw_box

        rendered = "\n".join(window.rendered_row(y) for y in range(8))
        self.assertIn("Pressure", rendered)
        self.assertIn("Nominal", rendered)
        self.assertIn("Throttled", rendered)
        self.assertIn("Hot", rendered)
        self.assertIn("45.5°C · E5", rendered)
        self.assertIn("PMU avg", rendered)
        self.assertIn("31.8°C", rendered)
        self.assertIn("Power supply", rendered)
        self.assertIn("Airport", rendered)
        self.assertIn("Trackpad", rendered)
        self.assertIn("Trackpad act", rendered)
        self.assertNotIn("Temp avg", rendered)
        self.assertNotIn("Batt temp", rendered)
        sensor_label = next(entry for entry in window.entries if entry[2] == "Power supply")
        sensor_value = next(entry for entry in window.entries if entry[2] == "31.5°C")
        self.assertGreaterEqual(sensor_value[1] - (sensor_label[1] + len(sensor_label[2])), 2)
        hot_label = next(entry for entry in window.entries if entry[2] == "Hot")
        hot_value = next(entry for entry in window.entries if entry[2] == "45.5°C · E5")
        self.assertGreaterEqual(hot_value[1] - (hot_label[1] + len(hot_label[2])), 2)

        wide_window = self.RecordingWindow()
        wide_placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 8, 60), "normal")
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_thermals_panel(wide_window, wide_placement, sample, asmond.BatteryStats(), colors)
        finally:
            asmond.draw_box = old_draw_box
        wide_rendered = "\n".join(wide_window.rendered_row(y) for y in range(8))
        self.assertIn("45.5°C · CPU@E5", wide_rendered)

    def test_hotspot_uses_hottest_verified_sensor_and_source(self) -> None:
        sample = asmond.MetricSample(
            cpu_temp_max_c=45.5,
            gpu_temp_max_c=42.0,
            pmu_temp_max_c=47.2,
            power_supply_temp_c=31.5,
        )

        self.assertEqual(asmond.thermal_hotspot(sample), (47.2, "PMU"))

    def test_hotspot_identifies_a_verified_cpu_core(self) -> None:
        sample = asmond.MetricSample(cpu_temp_max_c=45.5, cpu_temp_max_label="E5")

        self.assertEqual(asmond.thermal_hotspot(sample), (45.5, "CPU@E5"))

    def test_power_panel_places_cpu_and_gpu_average_temperatures_in_own_column(self) -> None:
        sample = asmond.MetricSample(
            telemetry_source="apple",
            soc_power_mw=5000.0,
            cpu_power_mw=2500.0,
            gpu_power_mw=800.0,
            ane_power_mw=0.0,
            cpu_temp_avg_c=43.3,
            gpu_temp_avg_c=39.1,
        )
        window = self.RecordingWindow()
        colors = {"accent": 0, "muted": 0, "fg": 0, "warn": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_power_section(
                window,
                0,
                0,
                7,
                50,
                sample,
                asmond.History(8),
                asmond.BatteryStats(),
                1.0,
                colors,
            )
        finally:
            asmond.draw_box = old_draw_box

        rendered = "\n".join(window.rendered_row(y) for y in range(7))
        self.assertIn("temp avg", rendered)
        self.assertIn("CPU", rendered)
        self.assertIn("43.3°C", rendered)
        self.assertIn("GPU", rendered)
        self.assertIn("39.1°C", rendered)
        temp_header = next(entry for entry in window.entries if entry[2] == "temp avg")
        average_header = next(entry for entry in window.entries if entry[2] == "30s avg")
        peak_header = next(entry for entry in window.entries if entry[2] == "peak")
        self.assertLess(average_header[1], peak_header[1])
        self.assertLess(peak_header[1], temp_header[1])
        self.assertGreaterEqual(peak_header[1] - (average_header[1] + len(average_header[2])), 3)
        self.assertGreaterEqual(temp_header[1] - (peak_header[1] + len(peak_header[2])), 3)
        self.assertIsNone(asmond.power_row_temperature(sample, "soc"))
        self.assertIsNone(asmond.power_row_temperature(sample, "ane"))
        self.assertIsNone(
            asmond.power_row_temperature(
                asmond.MetricSample(telemetry_source="intel", soc_temp_c=80.0),
                "cpu",
            )
        )

    def test_power_panel_spreads_summary_columns_when_space_is_available(self) -> None:
        sample = asmond.MetricSample(
            telemetry_source="apple",
            soc_power_mw=5000.0,
            cpu_power_mw=2500.0,
            gpu_power_mw=800.0,
            cpu_temp_avg_c=43.3,
            gpu_temp_avg_c=39.1,
        )
        window = self.RecordingWindow()
        colors = {"accent": 0, "muted": 0, "fg": 0, "warn": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_power_section(
                window,
                0,
                0,
                7,
                90,
                sample,
                asmond.History(8),
                asmond.BatteryStats(),
                1.0,
                colors,
            )
        finally:
            asmond.draw_box = old_draw_box

        headers = {text: x for _, x, text in window.entries if text in {"temp avg", "30s avg", "peak"}}
        average_gap = headers["peak"] - (headers["30s avg"] + len("30s avg"))
        temperature_gap = headers["temp avg"] - (headers["peak"] + len("peak"))
        self.assertGreaterEqual(average_gap, 7)
        self.assertGreaterEqual(temperature_gap, 7)

    def test_graph_gradient_uses_smooth_palette_and_legacy_fallback(self) -> None:
        gradient_colors = {"dim": 1, "good": 2, "warn": 3, "bad": 4}
        gradient_colors.update({f"graph_{index}": 100 + index for index in range(11)})

        self.assertEqual(asmond.graph_gradient_attr(None, gradient_colors, warn_at=0.55, bad_at=0.85), 1)
        self.assertEqual(asmond.graph_gradient_attr(0.01, gradient_colors, warn_at=0.55, bad_at=0.85), 100)
        self.assertEqual(asmond.graph_gradient_attr(0.50, gradient_colors, warn_at=0.55, bad_at=0.85), 105)
        self.assertEqual(asmond.graph_gradient_attr(1.00, gradient_colors, warn_at=0.55, bad_at=0.85), 110)
        self.assertEqual(asmond.power_graph_attr(50.0, 100.0, gradient_colors), 105)
        self.assertEqual(asmond.load_graph_attr(50.0, gradient_colors), 105)
        self.assertEqual(asmond.io_graph_attr(50.0, 100.0, gradient_colors), 105)

        basic_colors = {"dim": 1, "good": 2, "warn": 3, "bad": 4}
        self.assertEqual(asmond.power_graph_attr(54.0, 100.0, basic_colors), 2)
        self.assertEqual(asmond.power_graph_attr(55.0, 100.0, basic_colors), 3)
        self.assertEqual(asmond.power_graph_attr(85.0, 100.0, basic_colors), 4)
        self.assertEqual(asmond.load_graph_attr(59.0, basic_colors), 2)
        self.assertEqual(asmond.load_graph_attr(60.0, basic_colors), 3)
        self.assertEqual(asmond.load_graph_attr(90.0, basic_colors), 4)

    def test_color_initialization_keeps_mono_theme_monochrome(self) -> None:
        initialized: list[tuple[int, int, int]] = []

        def init_pair(pair: int, foreground: int, background: int) -> None:
            initialized.append((pair, foreground, background))

        with (
            mock.patch.object(asmond.curses, "has_colors", return_value=True),
            mock.patch.object(asmond.curses, "start_color"),
            mock.patch.object(asmond.curses, "use_default_colors"),
            mock.patch.object(asmond.curses, "init_pair", side_effect=init_pair),
            mock.patch.object(asmond.curses, "color_pair", side_effect=lambda pair: pair * 100),
            mock.patch.object(asmond.curses, "COLORS", 256, create=True),
            mock.patch.object(asmond.curses, "COLOR_PAIRS", 256, create=True),
        ):
            classic = asmond.init_colors("classic")
            classic_calls = tuple(initialized)
            initialized.clear()
            mono = asmond.init_colors("mono")
            mono_calls = tuple(initialized)

        self.assertEqual(len([key for key in classic if key.startswith("graph_")]), 11)
        self.assertFalse([key for key in mono if key.startswith("graph_")])
        self.assertEqual(len(classic_calls), 7 + 11)
        self.assertEqual(len(mono_calls), 7)

    def test_battery_panel_uses_two_columns_and_owns_battery_temperature(self) -> None:
        battery = asmond.BatteryStats(
            charge_pct=78.0,
            charging=True,
            power_mw=12000.0,
            time_remaining_min=85,
            health_pct=91.0,
            cycle_count=120,
            temperature_c=29.7,
            max_capacity=4500,
            design_capacity=5000,
        )
        window = self.RecordingWindow()
        colors = {"muted": 0, "fg": 0, "warn": 0, "good": 0}

        asmond.draw_battery_section(window, 0, 0, 6, 46, battery, colors)

        rendered = "\n".join(window.rendered_row(y) for y in range(6))
        self.assertIn("Charge 78%", rendered)
        self.assertIn("Health 91%", rendered)
        self.assertIn("Temp   29.7°C", rendered)
        self.assertIn("Design 4500/5000 mAh", rendered)

        narrow_window = self.RecordingWindow()
        asmond.draw_battery_section(narrow_window, 0, 0, 6, 35, battery, colors)
        narrow_rendered = "\n".join(narrow_window.rendered_row(y) for y in range(6))
        self.assertIn("Temp", narrow_rendered)
        self.assertIn("29.7°C", narrow_rendered)
        for row_y in {entry_y for entry_y, _, _ in narrow_window.entries}:
            entries = sorted(
                (entry_x, entry_x + len(text) - 1)
                for entry_y, entry_x, text in narrow_window.entries
                if entry_y == row_y
            )
            self.assertTrue(all(left[1] < right[0] for left, right in zip(entries, entries[1:])))

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
        sample = asmond.MetricSample(
            telemetry_source="apple",
            soc_power_mw=1200.0,
            cpu_power_mw=800.0,
            gpu_power_mw=200.0,
        )

        label = asmond.power_graph_live_label(sample, history)

        self.assertEqual(label, "SoC 1.20 W  CPU 800 mW  GPU 200 mW")
        self.assertNotIn("ANE", label)

    def test_power_graph_live_label_uses_package_on_intel(self) -> None:
        history = asmond.History(8)
        sample = asmond.MetricSample(telemetry_source="intel", soc_power_mw=6100.0, cpu_power_mw=2900.0)

        label = asmond.power_graph_live_label(sample, history)

        self.assertEqual(label, "Package 6.10 W  CPU 2.90 W")
        self.assertEqual(asmond.selected_power_label("soc", sample), "Package")

    def test_intel_host_keeps_package_label_before_or_between_full_samples(self) -> None:
        partial = asmond.sample_from_plist({"smc": {"cpu_die": 79.2, "fan": 1200}})
        self.assertIsNone(partial.telemetry_source)
        old_host_machine = asmond.HOST_MACHINE
        try:
            asmond.HOST_MACHINE = "x86_64"
            self.assertTrue(asmond.is_intel_sample(partial))
            self.assertEqual(asmond.total_power_label(), "Package")
            self.assertEqual(asmond.total_power_label(partial), "Package")
            self.assertEqual(asmond.selected_power_label("soc", partial), "Package")
            self.assertTrue(asmond.apply_system_cpu_usage(partial, 37.5))
            self.assertEqual(partial.cpu_usage_pct, 37.5)
            self.assertEqual(partial.cpu_usage_source, "mach")
            self.assertEqual(asmond.clock_panel_rows(partial)[0][0], "CPU")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                asmond.print_sample(partial)
            self.assertIn("Package:", output.getvalue())
            self.assertNotIn("SoC/Total:", output.getvalue())
            self.assertNotIn("P usage:", output.getvalue())

            explicit_apple = asmond.MetricSample(telemetry_source="apple")
            self.assertFalse(asmond.is_intel_sample(explicit_apple))
            self.assertEqual(asmond.total_power_label(explicit_apple), "SoC")
        finally:
            asmond.HOST_MACHINE = old_host_machine

    def test_intel_package_label_propagates_to_menu_and_graph_hotkeys(self) -> None:
        sample = asmond.MetricSample(telemetry_source="intel", soc_power_mw=6100.0)
        self.assertEqual(
            asmond.menu_value_text(
                "upper_power",
                argparse.Namespace(),
                "soc",
                "cpu",
                "disk_read",
                "net_in",
                "rows",
                "hidden",
                "cpu",
                "battery",
                sample,
            ),
            "Package",
        )

        window = self.RecordingWindow()
        colors = {"fg": 0, "muted": 0, "warn": 0}
        asmond.draw_power_mode_legend(window, 0, 100, "soc", "soc", ("soc",), sample, colors)
        legend = window.rendered_row(0)
        self.assertIn("upper S Package", legend)
        self.assertIn("lower s Package", legend)

    def test_intel_package_value_does_not_overlap_power_panel_label(self) -> None:
        sample = asmond.MetricSample(telemetry_source="intel", soc_power_mw=6100.0, cpu_power_mw=2900.0)
        history = asmond.History(8)
        colors = {"accent": 0, "muted": 0, "fg": 0, "warn": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            for detail, height, width in (("compact", 5, 40), ("normal", 7, 50)):
                with self.subTest(detail=detail):
                    window = self.RecordingWindow()
                    asmond.draw_power_section(
                        window,
                        0,
                        0,
                        height,
                        width,
                        sample,
                        history,
                        asmond.BatteryStats(),
                        1.0,
                        colors,
                        detail,
                    )
                    label_y, label_x, label = next(entry for entry in window.entries if entry[2] == "Package")
                    value_y, value_x, _value = next(entry for entry in window.entries if entry[2] == "6.10 W")
                    self.assertEqual(value_y, label_y)
                    self.assertGreaterEqual(value_x, label_x + len(label) + 1)
        finally:
            asmond.draw_box = old_draw_box

    def test_power_panel_hides_summaries_when_narrow_values_would_overlap(self) -> None:
        sample = asmond.MetricSample(telemetry_source="intel", soc_power_mw=123_450.0, cpu_power_mw=99_990.0)
        window = self.RecordingWindow()
        colors = {"accent": 0, "muted": 0, "fg": 0, "warn": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_power_section(
                window,
                0,
                0,
                7,
                34,
                sample,
                asmond.History(8),
                asmond.BatteryStats(),
                1.0,
                colors,
            )
        finally:
            asmond.draw_box = old_draw_box

        self.assertNotIn("30s avg", [text for _, _, text in window.entries])
        self.assertNotIn("peak", [text for _, _, text in window.entries])
        for y in {entry_y for entry_y, _, _ in window.entries}:
            entries = sorted((x, x + len(text) - 1) for entry_y, x, text in window.entries if entry_y == y)
            self.assertTrue(all(left[1] < right[0] for left, right in zip(entries, entries[1:])))

    def test_compact_intel_power_panel_keeps_unit_at_minimum_two_column_width(self) -> None:
        sample = asmond.MetricSample(telemetry_source="intel", soc_power_mw=100_000.0, cpu_power_mw=99_990.0)
        window = self.RecordingWindow()
        colors = {"accent": 0, "muted": 0, "fg": 0, "warn": 0}
        old_draw_box = asmond.draw_box
        try:
            asmond.draw_box = lambda *_args, **_kwargs: None
            asmond.draw_power_section(
                window,
                0,
                0,
                5,
                38,
                sample,
                asmond.History(8),
                asmond.BatteryStats(),
                1.0,
                colors,
                "compact",
            )
        finally:
            asmond.draw_box = old_draw_box

        self.assertIn("100.00 W", [text for _, _, text in window.entries])

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

    def test_full_layout_gives_unused_battery_height_to_ram(self) -> None:
        battery_layout = asmond.dashboard_layout(
            61,
            181,
            "full",
            True,
            "hidden",
            charge_panel="battery",
        )
        usb_layout = asmond.dashboard_layout(
            61,
            181,
            "full",
            True,
            "hidden",
            charge_panel="usb",
        )

        self.assertEqual(battery_layout.slot("right_middle").rect, asmond.Rect(26, 90, 28, 91))
        self.assertEqual(battery_layout.slot("right_lower").rect, asmond.Rect(54, 90, 6, 91))
        self.assertEqual(battery_layout.slot("right_lower").detail, "normal")
        self.assertEqual(usb_layout.slot("right_middle").rect, asmond.Rect(26, 90, 26, 91))
        self.assertEqual(usb_layout.slot("right_lower").rect, asmond.Rect(52, 90, 8, 91))

    def test_six_line_battery_panel_renders_all_four_rows(self) -> None:
        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden", charge_panel="battery")
        placement = layout.slot("right_lower")
        battery = asmond.BatteryStats(
            charge_pct=15.0,
            charging=False,
            power_mw=-3790.0,
            time_remaining_min=189,
            health_pct=96.0,
            cycle_count=69,
            temperature_c=23.8,
            max_capacity=4466,
            design_capacity=4629,
        )
        window = self.RecordingWindow()
        colors = {"muted": 0, "fg": 0, "warn": 0, "good": 0}

        asmond.draw_battery_section(
            window,
            placement.rect.y + 1,
            placement.rect.x + 2,
            placement.rect.h - 2,
            placement.rect.w - 4,
            battery,
            colors,
            placement.detail,
        )

        rendered = "\n".join(window.rendered_row(y) for y in range(placement.rect.y, placement.rect.y + placement.rect.h))
        for label in ("Charge", "State", "Power", "Time", "Health", "Cycles", "Temp", "Design"):
            self.assertIn(label, rendered)

    def test_full_layout_keeps_input_panel_tall_after_battery_fallback(self) -> None:
        effective = asmond.effective_charge_panel(
            "battery",
            asmond.BatteryStats(),
            asmond.UsbCStats(system_voltage_v=20.0),
        )

        layout = asmond.dashboard_layout(61, 181, "full", True, "hidden", charge_panel=effective)

        self.assertEqual(effective, "usb")
        self.assertEqual(layout.slot("right_lower").rect.h, asmond.CHARGE_PANEL_H)

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
        self.assertEqual(asmond.TAILOR_MENU_ITEMS[:6], ("profile", "grid", "graph", "slot", "panel", "detail"))

    def test_tailor_menu_stays_at_top_and_switches_sides(self) -> None:
        left_anchor = asmond.Rect(20, 0, 10, 80)
        right_anchor = asmond.Rect(20, 100, 10, 80)
        height = len(asmond.TAILOR_MENU_ITEMS) + 7
        self.assertEqual(asmond.tailor_menu_rect(60, 180, left_anchor), asmond.Rect(2, 126, height, 52))
        self.assertEqual(asmond.tailor_menu_rect(60, 180, right_anchor), asmond.Rect(2, 2, height, 52))

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

    def test_tailor_updates_one_slot_without_mutating_other_saved_assignments(self) -> None:
        custom = {
            "upper_left": {"panel": "ram", "detail": "detail"},
            "right_middle": {"panel": "process", "detail": "detail"},
        }
        updated = asmond_layout.update_custom_slot(custom, "upper_left", panel_id="battery")
        self.assertEqual(updated["upper_left"], {"panel": "battery", "detail": "normal"})
        self.assertEqual(updated["right_middle"], custom["right_middle"])
        self.assertEqual(custom["upper_left"], {"panel": "ram", "detail": "detail"})
        self.assertEqual(asmond_layout.update_custom_slot(custom, "missing", panel_id="cable"), custom)
        self.assertEqual(asmond_layout.update_custom_slot(updated, "upper_left", detail="detail"), updated)
        compact = asmond_layout.update_custom_slot(updated, "upper_left", detail="compact")
        self.assertEqual(compact["upper_left"]["detail"], "compact")
        self.assertEqual(asmond_layout.update_custom_slot(custom, "upper_left", panel_id="missing"), custom)

    def test_custom_battery_height_returns_spare_rows_to_the_panel_above(self) -> None:
        custom = {"right_lower": {"panel": "battery", "detail": "normal"}}
        battery_layout = asmond.dashboard_layout(61, 181, "custom", False, "hidden", custom)
        legacy_layout = asmond.dashboard_layout(61, 181, "custom", False, "hidden")
        self.assertEqual(battery_layout.slot("right_lower").rect.h, 6)
        self.assertEqual(battery_layout.slot("right_middle").rect.h, legacy_layout.slot("right_middle").rect.h + 2)
        self.assertEqual(battery_layout.slot("right_lower").detail, "normal")

    def test_tailor_size_hint_accounts_for_single_column_charge_content(self) -> None:
        self.assertEqual(asmond_layout.panel_size_hint("battery", "normal", 90), (30, 6))
        self.assertEqual(asmond_layout.panel_size_hint("battery", "normal", 40), (30, 10))
        self.assertEqual(asmond_layout.panel_size_hint("battery", "compact", 40), (30, 8))
        self.assertEqual(asmond_layout.panel_size_hint("cable", "normal", 90), (30, 8))
        self.assertEqual(asmond_layout.panel_size_hint("cable", "normal", 40), (30, 13))
        self.assertEqual(asmond_layout.panel_size_hint("cable", "compact", 40), (30, 9))
        self.assertEqual(asmond_layout.panel_size_hint("power_input", "detail", 90), (30, 10))

    def test_custom_panels_stay_within_grid_at_supported_terminal_sizes(self) -> None:
        for height, width in ((24, 72), (35, 100), (48, 120), (61, 181)):
            for slot_id in asmond.CUSTOM_SLOT_IDS:
                for panel_id in asmond.CUSTOM_PANEL_IDS:
                    custom = {slot_id: {"panel": panel_id, "detail": asmond.default_detail_for_panel(panel_id)}}
                    layout = asmond.dashboard_layout(height, width, "custom", False, "hidden", custom)
                    with self.subTest(size=(height, width), slot=slot_id, panel=panel_id):
                        for placement in layout.panels:
                            rect = placement.rect
                            self.assertGreaterEqual(rect.x, 0)
                            self.assertGreaterEqual(rect.y, 0)
                            self.assertGreater(rect.w, 0)
                            self.assertGreater(rect.h, 0)
                            self.assertLessEqual(rect.x + rect.w, width)
                            self.assertLessEqual(rect.y + rect.h, height - 1)
                        rects = [placement.rect for placement in layout.panels]
                        for index, left in enumerate(rects):
                            for right in rects[index + 1:]:
                                self.assertFalse(
                                    left.x < right.x + right.w and right.x < left.x + left.w
                                    and left.y < right.y + right.h and right.y < left.y + left.h
                                )

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
    def test_process_sort_cycle_matches_columns_and_skips_missing_gpu(self) -> None:
        processes = [asmond.ProcessInfo(1, 1.0, 10.0, 100, "a")]
        for gpu, expected in (
            (None, ("pid", "cpu", "ram", "rss", "name")),
            (0.0, ("pid", "cpu", "gpu", "ram", "rss", "name")),
        ):
            with self.subTest(gpu=gpu):
                processes[0].gpu_pct = gpu
                self.assertEqual(asmond.process_sort_modes(processes), expected)
                for index, current in enumerate(expected):
                    self.assertEqual(asmond.cycle_process_sort(processes, current, 1), expected[(index + 1) % len(expected)])
                    self.assertEqual(asmond.cycle_process_sort(processes, current, -1), expected[index - 1])
        self.assertNotIn("gpu", asmond.process_sort_modes([]))
        self.assertEqual(asmond.cycle_process_sort([], "gpu", 1), "ram")
        self.assertEqual(asmond.cycle_process_sort([], "gpu", -1), "cpu")

    def test_rss_sort_uses_resident_bytes_rather_than_rounded_ram_percent(self) -> None:
        processes = [
            asmond.ProcessInfo(1, 90.0, 0.1, 500, "a"),
            asmond.ProcessInfo(2, 1.0, 0.1, 900, "b"),
            asmond.ProcessInfo(3, 2.0, 0.1, 100, "c"),
        ]
        self.assertEqual([item.pid for item in asmond.sorted_processes(processes, "rss")], [2, 1, 3])

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

    def test_parse_process_line_with_stable_start_time(self) -> None:
        process = asmond.parse_process_line(
            "123 alice 1 Fri Jul 17 10:20:30 2026 02:03 12.5 3.4 45678 /usr/bin/python3 worker.py"
        )
        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.start_time, "Fri Jul 17 10:20:30 2026")
        self.assertEqual(process.full_command, "/usr/bin/python3 worker.py")

    def test_process_parser_rejects_nonfinite_values(self) -> None:
        self.assertIsNone(asmond.parse_process_line("123 nan 1.0 4096 /usr/bin/a"))
        self.assertIsNone(asmond.parse_process_line("123 1.0 inf 4096 /usr/bin/a"))
        self.assertIsNone(asmond.parse_process_line("123 1.0 1.0 nan /usr/bin/a"))

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

    def test_process_gpu_ignores_invalid_samples_and_entries(self) -> None:
        self.assertEqual(
            asmond.process_gpu_pcts_from_plist({"invalid": True, "tasks": [{"pid": 123, "gpu_percent": 99}]}, 1.0),
            {},
        )
        self.assertEqual(
            asmond.process_gpu_pcts_from_plist(
                {"tasks": [{"pid": 123, "invalid": True, "gpu_percent": 99}, {"pid": 456, "gpu_percent": 12}]},
                1.0,
            ),
            {456: 12.0},
        )

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

    def test_process_gpu_missing_sudo_ticket_backs_off(self) -> None:
        state = asmond.ProcessGpuProbeState()
        values = asmond_process.read_process_gpu_pcts(
            probe_state=state,
            run=lambda command, **kwargs: self.fail("powermetrics should not run"),
            refresh_sudo=lambda prompt=False: (False, "not cached"),
        )
        self.assertEqual(values, {})
        self.assertFalse(state.can_probe())

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

    def test_process_identity_detects_pid_reuse_by_start_time(self) -> None:
        old = asmond.ProcessInfo(123, 1.0, 1.0, 100, "python3", 1, "02:03", "/usr/bin/python3", "alice", start_time="Fri Jul 17 10:00:00 2026")
        reused = asmond.ProcessInfo(123, 1.0, 1.0, 100, "python3", 1, "00:01", "/usr/bin/python3", "alice", start_time="Fri Jul 17 11:00:00 2026")
        self.assertFalse(asmond.process_identity_matches(old, reused))

    def test_merge_process_gpu_pcts(self) -> None:
        processes = [asmond.ProcessInfo(123, 1.0, 2.0, 4096, "a"), asmond.ProcessInfo(456, 3.0, 4.0, 8192, "b")]
        asmond.merge_process_gpu_pcts(processes, {456: 33.0})
        self.assertIsNone(processes[0].gpu_pct)
        self.assertEqual(processes[1].gpu_pct, 33.0)


class ProcessRunLoopSafetyTests(unittest.TestCase):
    @staticmethod
    def process_rows(*, gpu: bool = False) -> list[asmond.ProcessInfo]:
        return [
            asmond.ProcessInfo(
                12001, 80.0, 1.0, 4096, "cpu-worker", 2, "01:00",
                "/usr/bin/cpu-worker", "demo", 1.0 if gpu else None,
                "Tue Oct 20 10:00:00 2026",
            ),
            asmond.ProcessInfo(
                12002, 10.0, 2.0, 8192, "gpu-worker", 2, "01:00",
                "/usr/bin/gpu-worker", "demo", 90.0 if gpu else None,
                "Tue Oct 20 10:00:00 2026",
            ),
        ]

    def run_process_loop(self, frames: list[dict], *, preferred: str = "gpu", with_sample: bool = True, initial: dict | None = None, persist_path: Path | None = None) -> dict:
        """Drive the real UI loop with deterministic, separately delivered workers."""
        args = asmond.build_parser().parse_args(["--mock", "--layout", "custom"])
        args.process_sort = preferred
        args.process_panel = "hidden"
        args.charge_panel = "battery"
        args.rounded_borders = True
        args.diagnostics_in_cycle = True
        args.custom_grid = "columns"
        args.custom_layout = {"cell_1": {"panel": "process", "detail": "detail"}}
        args.custom_layouts = {}
        args.custom_slot = "cell_1"
        args.custom_name = "Work"
        args.custom_profiles = {}
        args.custom_profile = "default"
        if initial:
            vars(args).update(initial)
        rendered = []
        saved = []
        states = []
        inspected = []
        inspector_palettes = []
        channels = {}
        identities = {process.pid: process for process in self.process_rows(gpu=True)}
        sample = next(asmond.MockStream(100).samples())

        class SingleSampleStream:
            def __init__(self, _interval):
                pass

            def samples(self):
                if with_sample:
                    yield sample

            def stop(self):
                pass

        class ScriptedWindow:
            def __init__(self):
                self.index = 0
                self.size = (61, 180)

            def nodelay(self, _value):
                pass

            def timeout(self, _value):
                pass

            def refresh(self):
                pass

            def getmaxyx(self):
                frame = frames[self.index]
                self.size = frame.get("size", self.size)
                if "update" in frame:
                    channels["side_queue"].put(frame["update"])
                return self.size

            def getch(self):
                key = frames[self.index].get("key", -1)
                self.index += 1
                return key

        class ControlledThread:
            def __init__(self, target=None, args=(), **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                # No real worker, privilege escalation or asynchronous races.
                if self.target is asmond.side_metrics_worker:
                    channels["side_queue"] = self.args[0]
                    channels["poll_state"] = self.args[2]
                elif self.target.__name__ == "pump":
                    self.target(*self.args)

            def is_alive(self):
                return False

            def join(self, **_kwargs):
                pass

        def record_dashboard(*values):
            rendered.append({
                "sort": values[17], "selected": values[19], "pending": values[20],
                "poll_processes": channels["poll_state"].snapshot()[1],
            })
            states.append(json.loads(json.dumps({
                "tailor": values[25], "menu_selected": values[27],
                "settings_selected": values[24],
                "status": values[10],
                "layout": args.layout, "profile": args.custom_profile,
                "name": args.custom_name, "grid": args.custom_grid,
                "show_graph": args.custom_show_graph,
                "slot": args.custom_slot, "panels": args.custom_layout,
                "profiles": args.custom_profiles, "confirm": args.tailor_confirm,
                "charge_panel": values[18], "diagnostic_index": args.diagnostic_index,
                "diagnostic_scroll": args.diagnostic_scroll,
                "cable_port_label": args.cable_port_label,
                "rounded_borders": args.rounded_borders,
                "diagnostics_in_cycle": args.diagnostics_in_cycle,
            })))

        def save_for_test(namespace):
            saved.append(namespace.process_sort)
            if persist_path is not None:
                return asmond_settings.save_settings(persist_path, namespace, asmond.settings_schema())
            return None

        def record_inspector(_win, palette, rows, selected, **_kwargs):
            inspected.append((rows, selected))
            inspector_palettes.append(dict(palette))

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(asmond.threading, "Thread", ControlledThread))
            stack.enter_context(mock.patch.object(asmond, "MockStream", SingleSampleStream))
            stack.enter_context(mock.patch.object(asmond, "draw_dashboard", side_effect=record_dashboard))
            stack.enter_context(mock.patch.object(
                asmond, "draw_data_sources_overlay",
                side_effect=record_inspector,
            ))
            stack.enter_context(mock.patch.object(asmond, "init_colors", return_value={}))
            stack.enter_context(mock.patch.object(asmond.curses, "curs_set"))
            stack.enter_context(mock.patch.object(asmond.curses, "set_escdelay"))
            stack.enter_context(mock.patch.object(asmond, "is_root_process", return_value=False))
            stack.enter_context(mock.patch.object(asmond, "save_settings", side_effect=save_for_test))
            read_process = stack.enter_context(mock.patch.object(
                asmond, "read_process_for_kill", side_effect=identities.get,
            ))
            kill = stack.enter_context(mock.patch.object(asmond.os, "kill"))
            asmond.run_curses(ScriptedWindow(), args)
        return {"rendered": rendered, "saved": saved, "states": states, "args": args, "inspected": inspected, "inspector_palettes": inspector_palettes, "read": read_process, "kill": kill}

    def test_gpu_sort_preference_survives_startup_worker_delay_and_temporary_loss(self) -> None:
        result = self.run_process_loop([
            {"key": ord("t")},
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows())},
            {"update": asmond.SideMetricsUpdate(process_gpu_pcts={12001: 1.0, 12002: 90.0})},
            {"update": asmond.SideMetricsUpdate(process_gpu_pcts={})},
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows())},
            {"update": asmond.SideMetricsUpdate(process_gpu_pcts={12001: 1.0, 12002: 90.0})},
            {"key": ord("q")},
        ])
        self.assertEqual([frame["sort"] for frame in result["rendered"]], ["cpu", "cpu", "gpu", "gpu", "cpu", "gpu", "gpu"])
        self.assertEqual(result["saved"], ["gpu", "gpu"])
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_effective_sort_change_resets_cursor_and_pending_term(self) -> None:
        result = self.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": asmond.curses.KEY_DOWN},
            {"key": ord("k")},
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows(), process_gpu_pcts={}), "key": ord("k")},
            {"key": ord("q")},
        ])
        self.assertEqual(result["rendered"][1]["selected"], 1)
        self.assertEqual(result["rendered"][2], {
            "sort": "cpu", "selected": 0, "pending": None, "poll_processes": True,
        })
        self.assertEqual(result["rendered"][3]["pending"], 12001)
        self.assertEqual(result["read"].call_count, 2)
        self.assertEqual(result["saved"], ["gpu"])
        result["kill"].assert_not_called()

    def test_columns_gpu_table_too_narrow_blocks_term_but_keeps_polling(self) -> None:
        result = self.run_process_loop([
            {"size": (61, 150), "update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": ord("k")},
            {"key": ord("k")},
            {"key": ord("q")},
        ])
        self.assertTrue(all(frame["poll_processes"] for frame in result["rendered"]))
        self.assertTrue(all(frame["pending"] is None for frame in result["rendered"]))
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_resize_hidden_process_table_cancels_pending_term_before_return(self) -> None:
        result = self.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": ord("k")},
            {"size": (61, 150)},
            {"size": (61, 180), "key": ord("k")},
            {"key": ord("q")},
        ])
        self.assertIsNone(result["rendered"][1]["pending"])
        self.assertIsNone(result["rendered"][2]["pending"])
        self.assertEqual(result["rendered"][3]["pending"], 12002)
        self.assertEqual(result["read"].call_count, 2)
        result["kill"].assert_not_called()

    def test_columns_table_below_cpu_width_disables_polling_and_term(self) -> None:
        result = self.run_process_loop([
            {"size": (61, 90), "update": asmond.SideMetricsUpdate(processes=self.process_rows()), "key": ord("k")},
            {"key": ord("k")},
            {"key": ord("q")},
        ])
        self.assertTrue(all(not frame["poll_processes"] for frame in result["rendered"]))
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_term_targets_same_effective_order_as_rendered_gpu_or_cpu_table(self) -> None:
        for gpu, expected_sort, expected_pid in ((True, "gpu", 12002), (False, "cpu", 12001)):
            with self.subTest(gpu=gpu):
                result = self.run_process_loop([
                    {"update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=gpu)), "key": ord("k")},
                    {"key": ord("k")},
                    {"key": ord("q")},
                ])
                self.assertTrue(all(frame["sort"] == expected_sort for frame in result["rendered"]))
                result["kill"].assert_called_once_with(expected_pid, asmond.signal.SIGTERM)
                self.assertEqual(result["saved"], ["gpu"])

    def test_sort_arrow_starts_from_visible_fallback_column(self) -> None:
        result = self.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows()), "key": asmond.curses.KEY_RIGHT},
            {"key": ord("q")},
        ])
        self.assertEqual([frame["sort"] for frame in result["rendered"]], ["cpu", "ram"])
        self.assertEqual(result["saved"], ["ram", "ram"])
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_waiting_dashboard_does_not_allow_invisible_process_term(self) -> None:
        result = self.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": ord("k")},
            {"key": ord("k")},
            {"key": ord("q")},
        ], with_sample=False)
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_terminal_too_small_blocks_term_even_when_process_geometry_fits(self) -> None:
        result = self.run_process_loop([
            {"size": (23, 180), "update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": ord("k")},
            {"key": ord("k")},
            {"key": ord("q")},
        ])
        self.assertTrue(all(frame["poll_processes"] for frame in result["rendered"]))
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_modal_overlays_cancel_pending_term_and_block_hidden_actions(self) -> None:
        for toggle in ("h", "m", "T"):
            with self.subTest(toggle=toggle):
                result = self.run_process_loop([
                    {"update": asmond.SideMetricsUpdate(processes=self.process_rows(gpu=True)), "key": ord("k")},
                    {"key": ord(toggle)},
                    {"key": ord("k")},
                    {"key": ord(toggle)},
                    {"key": ord("k")},
                    {"key": ord("q")},
                ])
                self.assertEqual(result["rendered"][1]["pending"], 12002)
                self.assertIsNone(result["rendered"][2]["pending"])
                self.assertIsNone(result["rendered"][4]["pending"])
                self.assertEqual(result["rendered"][5]["pending"], 12002)
                self.assertEqual(result["read"].call_count, 2)
                result["kill"].assert_not_called()


class CustomProfileTests(unittest.TestCase):
    @staticmethod
    def profile_args():
        return argparse.Namespace(
            theme="classic", interval=1.0, layout="custom", show_io=False,
            custom_grid="balanced", custom_slot="cell_1", custom_name="Work",
            custom_layout={"cell_1": {"panel": "battery", "detail": "normal"}},
            custom_layouts={"classic": {"upper_left": {"panel": "ram", "detail": "detail"}}},
            custom_profiles={}, custom_profile="default",
        )

    def test_profiles_round_trip_multiple_assignments_on_the_same_grid(self):
        args = self.profile_args()
        asmond_layout.sync_custom_profile(args)
        self.assertTrue(asmond_layout.duplicate_custom_profile(args))
        copy_id = args.custom_profile
        args.custom_name = "Gaming"
        args.custom_layout = asmond.update_custom_slot(args.custom_layout, "cell_1", "load", grid="balanced")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(path, args, asmond.settings_schema()))
            raw = json.loads(path.read_text())
            self.assertEqual(raw["custom_layout"]["upper_left"]["panel"], "ram")
            with mock.patch.object(asmond, "SETTINGS_PATH", path):
                restored = asmond.build_parser().parse_args(["--mock"])
            self.assertEqual(restored.custom_profile, copy_id)
            self.assertEqual(restored.custom_name, "Gaming")
            self.assertEqual(restored.custom_layout["cell_1"]["panel"], "load")
            asmond_layout.activate_custom_profile(restored, "default")
            self.assertEqual(restored.custom_name, "Work")
            self.assertEqual(restored.custom_grid, "balanced")
            self.assertEqual(restored.custom_layout["cell_1"]["panel"], "battery")
            asmond_layout.activate_custom_profile(restored, copy_id)
            self.assertEqual(restored.custom_layout["cell_1"]["panel"], "load")
            self.assertEqual(restored.custom_layouts["classic"], args.custom_layouts["classic"])

    def test_duplicate_is_deep_independent_and_uses_unique_names_and_bounded_ids(self):
        args = self.profile_args()
        asmond_layout.sync_custom_profile(args)
        self.assertTrue(asmond_layout.duplicate_custom_profile(args))
        args.custom_layout["cell_1"]["panel"] = "cable"
        args.custom_layouts["classic"]["upper_left"]["panel"] = "process"
        original = args.custom_profiles["default"]
        self.assertEqual(original["layouts"]["balanced"]["cell_1"]["panel"], "battery")
        self.assertEqual(original["layouts"]["classic"]["upper_left"]["panel"], "ram")
        asmond_layout.activate_custom_profile(args, "default")
        for _ in range(asmond_layout.MAX_CUSTOM_PROFILES - 2):
            self.assertTrue(asmond_layout.duplicate_custom_profile(args))
        before = json.dumps(vars(args), sort_keys=True)
        self.assertFalse(asmond_layout.duplicate_custom_profile(args))
        self.assertEqual(json.dumps(vars(args), sort_keys=True), before)
        self.assertEqual(len({p["name"].casefold() for p in args.custom_profiles.values()}), asmond_layout.MAX_CUSTOM_PROFILES)

    def test_deleting_profile_preserves_other_assignments_and_protects_the_last(self):
        args = self.profile_args()
        asmond_layout.sync_custom_profile(args)
        self.assertFalse(asmond_layout.delete_custom_profile(args))
        self.assertTrue(asmond_layout.duplicate_custom_profile(args))
        copied = args.custom_profile
        args.custom_layout["cell_1"]["panel"] = "cable"
        args.custom_show_graph = False
        self.assertTrue(asmond_layout.delete_custom_profile(args))
        self.assertNotIn(copied, args.custom_profiles)
        self.assertEqual(args.custom_profile, "default")
        self.assertEqual(args.custom_layout["cell_1"]["panel"], "battery")
        self.assertTrue(args.custom_show_graph)
        before = json.dumps(vars(args), sort_keys=True)
        self.assertFalse(asmond_layout.delete_custom_profile(args))
        self.assertEqual(json.dumps(vars(args), sort_keys=True), before)

    def test_graph_visibility_is_independent_per_profile_and_survives_settings(self):
        args = self.profile_args()
        asmond_layout.sync_custom_profile(args)
        asmond_layout.duplicate_custom_profile(args)
        copied = args.custom_profile
        args.custom_show_graph = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(path, args, asmond.settings_schema()))
            with mock.patch.object(asmond, "SETTINGS_PATH", path):
                restored = asmond.build_parser().parse_args(["--mock"])
            self.assertFalse(restored.custom_show_graph)
            asmond_layout.activate_custom_profile(restored, "default")
            self.assertTrue(restored.custom_show_graph)
            asmond_layout.activate_custom_profile(restored, copied)
            self.assertFalse(restored.custom_show_graph)

    def test_grids_without_graph_reclaim_space_and_leave_builtin_layouts_unchanged(self):
        for grid in asmond.CUSTOM_GRIDS:
            for height, width in ((24, 72), (40, 100), (61, 181)):
                with self.subTest(grid=grid, height=height, width=width):
                    shown = asmond.dashboard_layout(height, width, "custom", False, "hidden", custom_grid=grid)
                    hidden = asmond.dashboard_layout(height, width, "custom", False, "hidden", custom_grid=grid, custom_show_graph=False)
                    self.assertIsNotNone(shown.panel("power_graph"))
                    self.assertIsNone(hidden.panel("power_graph"))
                    first = hidden.slot(asmond.custom_grid_slot_ids(grid)[0])
                    self.assertEqual(first.rect.y, 2)
                    self.assertGreaterEqual(sum(p.rect.h * p.rect.w for p in hidden.panels), sum(p.rect.h * p.rect.w for p in shown.panels if p.slot != "top"))
                    for placement in hidden.panels:
                        self.assertGreaterEqual(placement.rect.y, 2)
                        self.assertLessEqual(placement.rect.y + placement.rect.h, height - 1)
                        self.assertLessEqual(placement.rect.x + placement.rect.w, width)
        for name in ("full", "compact", "focus"):
            shown = asmond.dashboard_layout(61, 181, name, False, "hidden")
            hidden = asmond.dashboard_layout(61, 181, name, False, "hidden", custom_show_graph=False)
            self.assertEqual(shown, hidden)

    def test_profiles_sanitize_malformed_fields_and_choose_valid_active_profile(self):
        value = {
            "bad/key": {"name": "Invalid"}, "bad": [],
            "reserved": {"name": "FULL"},
            "ok": {"name": " Desk\x1b \n work ", "grid": ["columns"], "layouts": {"classic": {"upper_left": {"panel": "battery"}}}},
            "other": {"name": "Other", "grid": "balanced", "layouts": {"balanced": {"cell_9": {"panel": "ram"}}}},
        }
        clean = asmond_layout.sanitize_custom_profiles(value)
        self.assertEqual(tuple(clean), ("ok", "other"))
        self.assertEqual(clean["ok"]["name"], "Desk work")
        self.assertEqual(clean["ok"]["grid"], "classic")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"custom_profiles": value, "custom_profile": ["invalid"], "custom_slot": "cell_6"}))
            restored = asmond_settings.load_settings(path, asmond.settings_schema())
        self.assertEqual(restored["custom_profile"], "ok")
        self.assertEqual(restored["custom_slot"], "upper_left")
        self.assertEqual(restored["custom_layout"]["upper_left"]["panel"], "battery")

    def test_invalid_profile_records_keep_legacy_assignments_for_migration(self):
        legacy = {"upper_left": {"panel": "battery", "detail": "compact"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"custom_layout": legacy, "custom_name": "Old desk", "custom_profiles": {"bad": {"name": "full"}}}))
            with mock.patch.object(asmond, "SETTINGS_PATH", path):
                args = asmond.build_parser().parse_args(["--mock"])
            self.assertIsNone(asmond_settings.save_settings(path, args, asmond.settings_schema()))
            restored = asmond_settings.load_settings(path, asmond.settings_schema())
        self.assertEqual(restored["custom_name"], "Old desk")
        self.assertEqual(restored["custom_profiles"]["default"]["layouts"]["classic"], legacy)

    def test_slot_transfer_resolves_defaults_without_mutating_source_or_other_grids(self):
        layout = {"cell_1": {"panel": "battery", "detail": "compact"}}
        original = json.loads(json.dumps(layout))
        copied = asmond_layout.transfer_custom_slot(layout, "cell_1", "cell_2", "balanced")
        self.assertEqual(copied["cell_2"], layout["cell_1"])
        swapped = asmond_layout.transfer_custom_slot(layout, "cell_1", "cell_2", "balanced", swap=True)
        self.assertEqual(swapped["cell_1"], {"panel": "thermals", "detail": "normal"})
        self.assertEqual(swapped["cell_2"], original["cell_1"])
        self.assertEqual(layout, original)
        self.assertEqual(asmond_layout.transfer_custom_slot(layout, "cell_1", "bad", "balanced"), original)

    @staticmethod
    def keys_for(item):
        return [asmond.curses.KEY_DOWN] * asmond.TAILOR_MENU_ITEMS.index(item)

    def run_keys(self, keys, **kwargs):
        return ProcessRunLoopSafetyTests().run_process_loop([{"key": key} for key in keys], **kwargs)

    def test_tailor_opens_directly_and_numbers_select_fields_without_a_grid_submenu(self):
        result = self.run_keys(
            [ord("T"), ord("g"), ord("1"), 27, ord("q")],
            initial={"layout": "full"},
        )
        self.assertTrue(result["states"][1]["tailor"])
        self.assertEqual(result["states"][1]["layout"], "custom")
        self.assertEqual([s["menu_selected"] for s in result["states"][1:4]], [0, 0, asmond.TAILOR_MENU_ITEMS.index("panel")])
        self.assertFalse(result["states"][4]["tailor"])
        result["kill"].assert_not_called()

    def test_numeric_shortcuts_select_only_fields_in_the_active_grid(self):
        result = self.run_keys([ord("T"), ord("4"), ord("7"), 27, ord("q")], initial={"custom_grid": "balanced", "custom_layout": {}})
        self.assertEqual(result["states"][2]["slot"], "cell_4")
        self.assertEqual(result["states"][3]["slot"], "cell_4")
        self.assertEqual(result["states"][2]["menu_selected"], asmond.TAILOR_MENU_ITEMS.index("panel"))

    def test_single_profile_row_explains_how_to_create_another_and_graph_row_toggles(self):
        result = self.run_keys([ord("T"), asmond.curses.KEY_RIGHT, 27, ord("q")])
        self.assertIn("Duplicate profile", result["states"][2]["status"])
        self.assertIn("1/1", asmond.tailor_menu_value_text("profile", result["args"]))
        keys = [ord("T")] + self.keys_for("graph") + [10, 27, ord("q")]
        result = self.run_keys(keys)
        self.assertFalse(result["args"].custom_show_graph)
        self.assertFalse(result["args"].custom_profiles["default"]["show_graph"])

    def test_profile_delete_requires_confirmation_and_keeps_an_existing_profile(self):
        args = self.profile_args()
        asmond_layout.sync_custom_profile(args)
        asmond_layout.duplicate_custom_profile(args)
        for confirm in (False, True):
            with self.subTest(confirm=confirm):
                keys = [ord("T")] + self.keys_for("delete") + [10, 10 if confirm else 27, 27, ord("q")]
                result = self.run_keys(keys, initial=json.loads(json.dumps(vars(args))))
                self.assertEqual(len(result["args"].custom_profiles), 1 if confirm else 2)
                self.assertEqual(result["args"].custom_profile, "default" if confirm else "profile_01")
        result = self.run_keys([ord("T")] + self.keys_for("delete") + [10, 27, ord("q")])
        self.assertEqual(len(result["args"].custom_profiles), 1)

    def test_tailor_arrow_menu_duplicates_renames_and_restores_profiles_after_restart(self):
        keys = [ord("T")] + self.keys_for("duplicate") + [10, asmond.curses.KEY_UP, 10]
        keys += [127] * len("Work copy") + list(map(ord, "Gaming")) + [10]
        keys += [asmond.curses.KEY_UP] * asmond.TAILOR_MENU_ITEMS.index("name")
        keys += [asmond.curses.KEY_LEFT, 27, ord("q")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            result = self.run_keys(keys, persist_path=path)
            restored = asmond_settings.load_settings(path, asmond.settings_schema())
        self.assertEqual(restored["custom_profile"], "default")
        self.assertEqual(restored["custom_name"], "Work")
        self.assertEqual(restored["custom_profiles"]["profile_01"]["name"], "Gaming")
        self.assertEqual(result["args"].custom_layout["cell_1"]["panel"], "process")
        result["kill"].assert_not_called()

    def test_tailor_copy_and_reset_require_confirmation_and_only_touch_current_grid(self):
        before = {"cell_1": {"panel": "battery", "detail": "compact"}}
        classic = {"upper_left": {"panel": "cable", "detail": "normal"}}
        for operation in ("copy", "reset"):
            for confirm in (False, True):
                with self.subTest(operation=operation, confirm=confirm):
                    keys = [ord("T")] + self.keys_for(operation) + [10, 10 if confirm else 27, 27, ord("q")]
                    result = self.run_keys(keys, initial={"custom_grid": "balanced", "custom_layout": before, "custom_layouts": {"classic": classic}})
                    self.assertEqual(result["states"][-3]["confirm"], operation)
                    actual = result["args"].custom_layout
                    if not confirm:
                        self.assertEqual(actual, before)
                    elif operation == "reset":
                        self.assertEqual(actual, {})
                    else:
                        self.assertEqual(actual["cell_2"], before["cell_1"])
                    self.assertEqual(result["args"].custom_layouts["classic"], classic)
                    result["kill"].assert_not_called()

    def test_profile_rename_rejects_duplicate_name_and_escape_cancels_edit(self):
        keys = [ord("T")] + self.keys_for("duplicate") + [10, asmond.curses.KEY_UP, 10]
        keys += [127] * len("Work copy") + list(map(ord, "Work")) + [10]
        keys += [27, 27, ord("q")]
        result = self.run_keys(keys)
        self.assertEqual(result["args"].custom_name, "Work copy")
        self.assertEqual(result["args"].custom_profiles["default"]["name"], "Work")

    def test_tailor_does_not_hide_failed_save_for_panel_edits_or_reset(self):
        for item in ("panel", "reset"):
            with self.subTest(item=item), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "settings.json"
                path.mkdir()
                keys = [ord("T")] + self.keys_for(item)
                keys += [asmond.curses.KEY_RIGHT] if item == "panel" else [10, 10]
                keys += [27, ord("q")]
                result = self.run_keys(keys, persist_path=path)
                self.assertTrue(result["states"][-1]["status"].startswith("settings not saved:"))

    def test_tailor_swap_is_keyboard_accessible_and_preserves_details(self):
        result = self.run_keys(
            [ord("T")] + self.keys_for("swap") + [10, 27, ord("q")],
            initial={"custom_grid": "balanced", "custom_layout": {"cell_1": {"panel": "battery", "detail": "compact"}}},
        )
        self.assertEqual(result["args"].custom_layout["cell_1"]["panel"], "thermals")
        self.assertEqual(result["args"].custom_layout["cell_2"], {"panel": "battery", "detail": "compact"})

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "isolated settings need an unprivileged POSIX PTY")
    def test_real_pty_tailor_profiles_restart_and_inspector_without_extra_shortcuts(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        root = Path(__file__).parent
        with tempfile.TemporaryDirectory(prefix="asmond-tailor-pty-") as tmp:
            settings_path = Path(tmp) / "settings.json"
            child = None
            master = None
            output = bytearray()

            def drain(seconds=0.05):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], min(0.02, max(0, deadline - time.monotonic())))
                    if ready:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output.extend(chunk)

            def wait_for(predicate):
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    drain()
                    if predicate():
                        return
                    if child.poll() is not None:
                        break
                self.fail("PTY condition timed out: " + output[-1500:].decode("utf-8", "replace"))

            def settings():
                return json.loads(settings_path.read_text()) if settings_path.exists() else {}

            def send(keys):
                os.write(master, keys)
                drain(0.08)

            def start(*options):
                nonlocal master, child
                master, slave = pty.openpty()
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 61, 181, 0, 0))
                env = dict(os.environ, TERM="xterm-256color", LANG="en_US.UTF-8", ASMOND_SETTINGS_DIR=tmp)
                child = subprocess.Popen(
                    [sys.executable, str(root / "asmond.py"), "--mock", "-i", "0.1", *options],
                    stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env,
                )
                os.close(slave)
                wait_for(lambda: b"THERMALS" in output)

            def stop():
                send(b"q")
                deadline = time.monotonic() + 5.0
                while child.poll() is None and time.monotonic() < deadline:
                    drain()
                self.assertEqual(child.poll(), 0, output[-1500:].decode("utf-8", "replace"))
                os.close(master)

            down, up, right, left = b"\x1bOB", b"\x1bOA", b"\x1bOC", b"\x1bOD"
            try:
                start("--layout", "full")
                send(b"T")
                wait_for(lambda: b"TAILOR" in output)
                send(down + right)
                wait_for(lambda: settings().get("custom_grid") == "balanced")
                send(down * (asmond.TAILOR_MENU_ITEMS.index("panel") - asmond.TAILOR_MENU_ITEMS.index("grid")) + right)
                wait_for(lambda: settings().get("custom_layouts", {}).get("balanced", {}).get("cell_1", {}).get("panel") == "thermals")
                send(down * (asmond.TAILOR_MENU_ITEMS.index("duplicate") - asmond.TAILOR_MENU_ITEMS.index("panel")) + b"\r")
                wait_for(lambda: settings().get("custom_profile") == "profile_01")
                send(up + b"\r" + b"\x7f" * len("Default copy") + b"PTY profile\r")
                wait_for(lambda: settings().get("custom_name") == "PTY profile")
                send(up * 2 + right)
                wait_for(lambda: settings().get("custom_layouts", {}).get("balanced", {}).get("cell_1", {}).get("panel") == "load")
                send(up * asmond.TAILOR_MENU_ITEMS.index("panel") + left)
                wait_for(lambda: settings().get("custom_profile") == "default")
                self.assertEqual(settings()["custom_layouts"]["balanced"]["cell_1"]["panel"], "thermals")
                send(down * asmond.TAILOR_MENU_ITEMS.index("graph") + right)
                wait_for(lambda: settings().get("custom_show_graph") is False)
                send(b"2")
                wait_for(lambda: settings().get("custom_slot") == "cell_2")
                send(b"1")
                wait_for(lambda: settings().get("custom_slot") == "cell_1")
                send(b"\x1b")
                sources_index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == "sources")
                send(b"m" + down * sources_index + b"\r")
                wait_for(lambda: b"DATA SOURCES" in output)
                send(down * 35)
                send(b"\x1b")
                send(b"\x1b")
                stop()
                output.clear()
                start()
                send(b"T" + right)
                wait_for(lambda: settings().get("custom_profile") == "profile_01")
                self.assertEqual(settings()["custom_name"], "PTY profile")
                self.assertEqual(settings()["custom_layouts"]["balanced"]["cell_1"]["panel"], "load")
                self.assertTrue(settings()["custom_show_graph"])
                send(down * asmond.TAILOR_MENU_ITEMS.index("delete") + b"\r\r")
                wait_for(lambda: len(settings().get("custom_profiles", {})) == 1)
                self.assertEqual(settings()["custom_profile"], "default")
                self.assertFalse(settings()["custom_show_graph"])
                self.assertEqual(settings()["custom_layouts"]["balanced"]["cell_1"]["panel"], "thermals")
                send(b"\x1b")
                stop()
                self.assertNotIn(b"Traceback", output)
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    drain(0.2)
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                if master is not None:
                    with contextlib.suppress(OSError):
                        os.close(master)


class DataSourceInspectorTests(unittest.TestCase):
    def test_inspector_keeps_zero_values_missing_values_and_derived_sources_distinct(self):
        args = argparse.Namespace(mock=False, inspector_polling=(False, False))
        sample = asmond.MetricSample(
            telemetry_source="apple",
            cpu_power_mw=1000.0, gpu_power_mw=0.0, cpu_usage_pct=12.5,
            cpu_usage_source="mach", temperature_source="AppleSMC + IOHID",
            cpu_temp_avg_c=43.2, raw_keys=321, memory_bandwidth_gbps={"dram": 2.5},
        )
        rows = asmond.data_source_rows(args, sample, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        fields = {label: (value, source) for label, value, source in rows}
        self.assertEqual(fields["GPU power"][0], "0 mW")
        self.assertEqual(fields["ANE power"][0], "n/a")
        self.assertIn("sum of available", fields["SoC"][1])
        self.assertEqual(fields["CPU avg load"][1], "mach")
        self.assertEqual(fields["Temp readers"][0], "AppleSMC + IOHID")
        self.assertIn("not recorded", fields["CPU temp avg"][1])
        self.assertEqual(fields["Process rows"][0], "n/a")
        self.assertEqual(fields["Disk read"][0], "n/a")
        self.assertEqual(fields["Raw keys"][0], "321")
        self.assertIn("not sensor count", fields["Raw keys"][1])
        self.assertTrue(any(label.startswith("Bandwidth") and "2.50" in value for label, value, _ in rows))
        sample.soc_power_mw = 1500.0
        rows = asmond.data_source_rows(args, sample, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        self.assertIn("powermetrics", next(source for label, _, source in rows if label == "SoC"))

    def test_inspector_uses_the_sample_platform_independently_of_test_host(self):
        args = argparse.Namespace(mock=False)
        for source, label, other in (("apple", "SoC", "Package"), ("intel", "Package", "SoC")):
            with self.subTest(source=source), mock.patch.object(asmond, "HOST_MACHINE", "x86_64" if source == "apple" else "arm64"):
                rows = asmond.data_source_rows(
                    args, asmond.MetricSample(telemetry_source=source, soc_power_mw=1200),
                    asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [],
                )
                labels = [name for name, _, _ in rows]
                self.assertIn(label, labels)
                self.assertNotIn(other, labels)

    def test_inspector_mock_provenance_never_claims_live_hardware_and_does_not_probe(self):
        args = argparse.Namespace(mock=True)
        with mock.patch.object(asmond.subprocess, "run", side_effect=AssertionError("inspector must not probe")):
            rows = asmond.data_source_rows(
                args, next(asmond.MockStream(100).samples()), asmond.mock_memory_stats(4),
                asmond.mock_battery_stats(4), asmond.mock_usb_c_stats(4),
                asmond.mock_io_stats(4), asmond.mock_processes(4), ("ioreg:TimeoutError",),
            )
        self.assertTrue(rows)
        self.assertTrue(all(source == "mock (generated data)" for _, _, source in rows))

    def test_inspector_empty_snapshot_and_real_zero_process_count_are_distinct(self):
        args = argparse.Namespace(mock=False, inspector_polling=(False, True), inspector_process_received=True)
        rows = asmond.data_source_rows(args, None, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        self.assertIn(("Main sampler", "awaiting first sample", "powermetrics"), rows)
        self.assertEqual(next(value for label, value, _ in rows if label == "Process rows"), "0")
        args.inspector_polling = (False, False)
        rows = asmond.data_source_rows(args, None, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        self.assertEqual(next(value for label, value, _ in rows if label == "Process rows"), "n/a")

    def test_inspector_renderer_scrolls_all_rows_and_stays_inside_terminal(self):
        fixtures = LayoutTests()
        rows = [(f"Reading {i}", f"{i}.0 W", "Long source description: " + "x" * 90) for i in range(80)]
        for height, width in ((24, 72), (40, 100), (61, 181)):
            for selected in (0, 35, 79):
                with self.subTest(height=height, width=width, selected=selected):
                    window = fixtures.RecordingWindow(height, width)
                    asmond.draw_data_sources_overlay(window, fixtures.dashboard_colors(), rows, selected)
                    rendered = window.rendered()
                    self.assertIn("DATA SOURCES", rendered)
                    self.assertIn(f"{selected + 1}/80 Reading {selected}", rendered)
                    for y, x, text in window.entries:
                        self.assertGreaterEqual(y, 0)
                        self.assertLess(y, height)
                        self.assertGreaterEqual(x, 0)
                        self.assertLessEqual(x + len(text), width)

    def test_menu_inspector_is_read_only_scrollable_and_esc_returns_to_menu(self):
        sources_index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == "sources")
        keys = [ord("m")] + [asmond.curses.KEY_DOWN] * sources_index
        keys += [10, asmond.curses.KEY_DOWN, ord("k"), ord("k"), 27, 27, ord("q")]
        result = ProcessRunLoopSafetyTests().run_process_loop([{"key": key} for key in keys])
        self.assertEqual([selected for _, selected in result["inspected"]], [0, 1, 1, 1])
        result["read"].assert_not_called()
        result["kill"].assert_not_called()
        # Opening/scrolling the inspector does not persist or mutate settings.
        self.assertEqual(result["saved"], ["gpu"])


class MenuNavigationTests(unittest.TestCase):
    def test_selection_wraps_both_ends_and_handles_empty_and_single_row_lists(self):
        for count in (0, 1, 2, 17):
            with self.subTest(count=count):
                self.assertEqual(asmond.cycle_index(0, -1, count), max(0, count - 1))
                self.assertEqual(asmond.cycle_index(max(0, count - 1), 1, count), 0)

    def test_settings_menu_and_tailor_wrap_in_both_directions(self):
        for toggle, field, count in (
            ("m", "settings_selected", len(asmond.MENU_ITEMS)),
            ("T", "menu_selected", len(asmond.TAILOR_MENU_ITEMS)),
        ):
            with self.subTest(toggle=toggle):
                keys = [ord(toggle), asmond.curses.KEY_UP, asmond.curses.KEY_DOWN]
                keys += [asmond.curses.KEY_DOWN] * count + [asmond.curses.KEY_UP, 27, ord("q")]
                result = ProcessRunLoopSafetyTests().run_process_loop([{"key": key} for key in keys])
                selected = [state[field] for state in result["states"]]
                self.assertEqual(selected[1:4], [0, count - 1, 0])
                self.assertEqual(selected[-4:-2], [count - 1, 0])
                self.assertEqual(selected[-2], count - 1)
                result["read"].assert_not_called()
                result["kill"].assert_not_called()

    def test_inspector_wraps_at_current_last_row_without_changing_settings(self):
        keys = [ord("m"), asmond.curses.KEY_UP, 10]
        keys += [asmond.curses.KEY_UP, asmond.curses.KEY_DOWN, 27, 27, ord("q")]
        result = ProcessRunLoopSafetyTests().run_process_loop([{"key": key} for key in keys])
        selected = [index for _, index in result["inspected"]]
        last = len(result["inspected"][0][0]) - 1
        self.assertEqual(selected, [0, last, 0])
        self.assertEqual(result["saved"], ["gpu"])
        result["kill"].assert_not_called()

    def test_process_list_wraps_and_discards_pending_term(self):
        harness = ProcessRunLoopSafetyTests()
        result = harness.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=harness.process_rows(gpu=True)), "key": ord("k")},
            {"key": asmond.curses.KEY_UP},
            {"key": asmond.curses.KEY_DOWN},
            {"key": asmond.curses.KEY_DOWN},
            {"key": asmond.curses.KEY_DOWN},
            {"key": ord("q")},
        ])
        self.assertEqual([row["selected"] for row in result["rendered"]], [0, 0, 1, 0, 1, 0])
        self.assertIsNotNone(result["rendered"][1]["pending"])
        self.assertTrue(all(row["pending"] is None for row in result["rendered"][2:]))
        result["kill"].assert_not_called()


class ReleaseUiTests(unittest.TestCase):
    def test_balanced_small_ram_explains_missing_content_and_warns_in_tailor(self) -> None:
        fixtures = LayoutTests()
        args = fixtures.dashboard_args("custom", show_io=False)
        args.custom_grid = "balanced"
        args.custom_slot = "cell_4"
        layout = asmond.dashboard_layout(
            24, 100, "custom", False, "hidden", custom_grid="balanced"
        )
        placement = layout.slot("cell_4")
        self.assertEqual(placement.rect.h, 7)
        self.assertEqual(asmond.panel_size_hint("ram", "detail", 50), (38, 9))

        window = fixtures.RecordingWindow(24, 100)
        fixtures.draw_test_dashboard(window, args)
        rect = placement.rect
        ram_content = "\n".join(
            window.rendered_row(y)[rect.x:rect.x + rect.w]
            for y in range(rect.y, rect.y + rect.h)
        )
        self.assertIn("enlarge panel (min 34x9)", ram_content)

        editor = fixtures.RecordingWindow(24, 100)
        asmond.draw_tailor_overlay(
            editor, layout, fixtures.dashboard_colors(), args,
            True, 0, "", False,
        )
        self.assertIn("Small slot; needs 9 rows", editor.rendered())

    def test_ram_minimum_renderable_height_keeps_real_content(self) -> None:
        fixtures = LayoutTests()
        memory = asmond.mock_memory_stats(4.0)
        sample = next(asmond.MockStream(100).samples())
        sample.memory_bandwidth_gbps = {}
        for detail in ("compact", "detail"):
            with self.subTest(detail=detail):
                window = fixtures.RecordingWindow(12, 80)
                placement = asmond.PanelPlacement(
                    "ram", asmond.Rect(1, 1, 9, 38), detail
                )
                asmond.draw_ram_panel(
                    window, placement, memory, sample, fixtures.dashboard_colors()
                )
                self.assertIn("RAM phys", window.rendered())
                self.assertIn("Used", window.rendered())
                self.assertIn("Swap", window.rendered())
                self.assertNotIn("enlarge panel", window.rendered())

    def test_process_table_size_boundaries_match_live_column_visibility(self) -> None:
        processes = [asmond.ProcessInfo(12, 3.0, 4.0, 500, "demo")]
        for gpu_pct, minimum_width in ((None, 47), (0.0, 54), (2.5, 54)):
            processes[0].gpu_pct = gpu_pct
            with self.subTest(gpu_pct=gpu_pct):
                self.assertFalse(asmond.process_table_fits(
                    asmond.Rect(1, 2, 5, minimum_width - 1), processes
                ))
                self.assertFalse(asmond.process_table_fits(
                    asmond.Rect(1, 2, 4, minimum_width), processes
                ))
                self.assertTrue(asmond.process_table_fits(
                    asmond.Rect(1, 2, 5, minimum_width), processes
                ))
        self.assertTrue(asmond.process_table_fits(asmond.Rect(1, 2, 5, 47)))

    def test_narrow_process_panel_shows_hint_instead_of_invisible_rows(self) -> None:
        fixtures = LayoutTests()
        processes = [asmond.ProcessInfo(12, 3.0, 4.0, 500, "demo")]
        for gpu_pct, minimum_width in ((None, 47), (0.0, 54)):
            processes[0].gpu_pct = gpu_pct
            for width in (minimum_width - 1, minimum_width):
                with self.subTest(gpu_pct=gpu_pct, width=width):
                    window = fixtures.RecordingWindow(12, 80)
                    asmond.draw_process_section(
                        window, 1, 1, 5, width, processes,
                        "cpu", 0, None, fixtures.dashboard_colors(),
                    )
                    if width < minimum_width:
                        self.assertIn("enlarge panel to show rows", window.rendered())
                        self.assertNotIn("demo", window.rendered())
                        self.assertNotIn("COMMAND", window.rendered())
                    else:
                        self.assertNotIn("enlarge panel", window.rendered())
                        self.assertIn("demo", window.rendered())
                        self.assertIn("COMMAND", window.rendered())


class PortDiagnosticTests(unittest.TestCase):
    def test_cycle_chip_is_visible_without_a_b_in_title_and_never_overlaps(self):
        colors = LayoutTests.dashboard_colors()
        for mode, title, target in (("battery", "BATTERY", "Power Input"), ("usb", "POWER INPUT", "Cable"), ("cable", "CABLE", "Port diag"), ("diagnostics", "PORT DIAGNOSTICS", "Battery")):
            for width in (30, 40, 90):
                win = LayoutTests.RecordingWindow(8, width)
                asmond.draw_charge_box(win, asmond.Rect(0, 0, 8, width), title, mode, colors)
                rendered = win.rendered_row(0)
                self.assertIn(title, rendered)
                self.assertIn("b ›", rendered)
                self.assertGreater(rendered.index("b ›"), rendered.index(title) + len(title))
                if width >= 40:
                    self.assertIn(target, rendered)
                self.assertTrue(all(x + len(text) <= width for _y, x, text in win.entries))

    @staticmethod
    def hpm(number=1, **extra):
        return {"IORegistryEntryName": "Port-USB-C", "PortType": 2, "PortNumber": number, "ConnectionActive": False, **extra}

    def test_live_shape_separates_ports_and_unmapped_controller_counters(self):
        usb = asmond.usb_c_stats_from_item({"PortControllerInfo": [{"PortControllerAttachCount": 0}]}, [self.hpm(LDCM_LiquidDetected=False, LDCM_StateDescription="Idle")])
        self.assertEqual([p.label for p in usb.diagnostics], ["USB-C 1", "Controller 1 (port unknown)"])
        self.assertIs(usb.diagnostics[0].liquid_detected, False)
        self.assertEqual(usb.diagnostics[0].counters, {})
        self.assertEqual(usb.diagnostics[1].counters, {"Attach": 0})
        self.assertFalse(usb.diagnostics[1].mapped)

    def test_missing_malformed_liquid_is_not_false(self):
        for value in (None, "false", "true", 2, [], {}):
            port = asmond_ports.diagnostic_snapshot("p", "p", {"LDCM_LiquidDetected": value})
            self.assertIsNone(port.liquid_detected)
            self.assertEqual(asmond_ports.liquid_label(port), "unavailable")
        for value, expected in ((True, "LIQUID REPORTED"), (False, "not reported"), (0, "not reported"), (1, "LIQUID REPORTED")):
            self.assertEqual(asmond_ports.liquid_label(asmond_ports.diagnostic_snapshot("p", "p", {"LDCM_LiquidDetected": value})), expected)

    def test_counter_validation_preserves_zero_not_missing(self):
        for value in (-1, True, 1.0, "4", float("inf"), 2**65):
            self.assertEqual(asmond_ports.diagnostic_snapshot("p", "p", {"PortControllerAttachCount": value}).counters, {})
        self.assertEqual(asmond_ports.diagnostic_snapshot("p", "p", {"PortControllerAttachCount": 0}).counters, {"Attach": 0})

    def test_conflicting_and_duplicate_hpm_identities_never_get_diagnostics(self):
        for items in ([self.hpm(), self.hpm()], [self.hpm(PortDescription="USB-C 2")], [self.hpm(number=999)]):
            usb = asmond.usb_c_stats_from_item({}, items)
            self.assertEqual(usb.diagnostics, [])

    def test_explicit_controller_identity_is_required_and_unique(self):
        entries = [{"PortType": 2, "PortNumber": 2, "PortControllerAttachCount": 8}]
        ports = asmond_ports.port_diagnostics([("USB-C 2", self.hpm(2))], entries)
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0].counters, {"Attach": 8})
        duplicate = asmond_ports.port_diagnostics([("USB-C 2", self.hpm(2))], entries * 2)
        self.assertEqual(duplicate[0].counters, {})
        self.assertTrue(all(not p.mapped for p in duplicate[1:]))

    def test_tracker_logs_observations_deltas_and_counter_reset(self):
        tracker = asmond_ports.PortDiagnosticTracker()
        def read(count, connected, liquid):
            return asmond_ports.diagnostic_snapshot("USB-C 1", "USB-C 1", {"PortControllerAttachCount": count, "ConnectionActive": connected, "LDCM_LiquidDetected": liquid})
        first = tracker.update([read(5, False, False)], 100)[0]
        second = tracker.update([read(7, True, True)], 101)[0]
        self.assertEqual(second.deltas, {"Attach": 2})
        self.assertEqual(len(first.events), 1)  # Published snapshots stay immutable.
        self.assertTrue(any("Connection: connected" in e for _, e in second.events))
        self.assertTrue(any("Liquid reported: yes" in e for _, e in second.events))
        reset = tracker.update([read(0, True, False)], 102)[0]
        self.assertEqual(reset.deltas, {})
        self.assertIn("counter reset", reset.events[-1][1])
        tracker.update([], 103)
        self.assertEqual(len(tracker.update([read(10, True, False)], 104)[0].events), 1)

    def test_unmapped_snapshots_never_generate_deltas_or_claim_events(self):
        tracker = asmond_ports.PortDiagnosticTracker()
        for value in (10, 20, 0):
            port = asmond_ports.diagnostic_snapshot("controller:0", "Controller 1", {"PortControllerAttachCount": value}, mapped=False)
            result = tracker.update([port])[0]
            self.assertEqual(result.deltas, {})
            self.assertEqual(result.events, [])

    def test_tracker_memory_is_bounded(self):
        tracker = asmond_ports.PortDiagnosticTracker()
        for value in range(100):
            port = asmond_ports.diagnostic_snapshot("p", "p", {"PortControllerAttachCount": value})
            result = tracker.update([port], value)[0]
        self.assertEqual(len(result.events), 64)
        self.assertEqual(result.deltas["Attach"], 99)
        tracker.update([])
        self.assertFalse(tracker.logs)

    def test_raw_trace_is_bounded_and_not_speculatively_decoded(self):
        port = asmond_ports.diagnostic_snapshot("p", "p", {"PortControllerEvtBuffer": bytes(range(256)) * 2})
        self.assertEqual(len(port.raw_trace), 256)
        rows = asmond.port_diagnostic_rows(asmond.UsbCStats(diagnostics=[port]), 0)
        self.assertTrue(any("not decoded" in source for _, _, source in rows))
        self.assertEqual(asmond.port_diagnostic_rows(asmond.UsbCStats(), 0), [])

    def test_diagnostics_panel_is_selectable_and_does_not_fallback_on_intel(self):
        self.assertIn("diagnostics", asmond.CHARGE_PANEL_MODES)
        self.assertIn("port_diagnostics", asmond.CUSTOM_PANEL_IDS)
        self.assertEqual(asmond.effective_charge_panel("diagnostics", asmond.BatteryStats(charge_pct=90), asmond.UsbCStats()), "diagnostics")
        for width, height in ((30, 5), (50, 8), (90, 20)):
            window = LayoutTests.RecordingWindow(height, width)
            rect = asmond.Rect(0, 0, height, width)
            asmond.draw_charge_panel(window, asmond.PanelPlacement("port_diagnostics", rect, "normal"), "battery", asmond.BatteryStats(), asmond.mock_usb_c_stats(4), LayoutTests.dashboard_colors())
            self.assertIn("PORT DIAGNOSTICS", window.rendered())
            self.assertTrue(all(0 <= y < height and 0 <= x and x + len(text) <= width for y, x, text in window.entries))

    def test_real_loop_direct_diagnostics_arrows_wrap_escape_preserve_process_actions(self):
        runner = ProcessRunLoopSafetyTests()
        ports = asmond.mock_usb_c_stats(4)
        result = runner.run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=ports), "key": 10},
            {"key": asmond.curses.KEY_LEFT},
            {"key": asmond.curses.KEY_UP},
            {"key": ord("k")},
            {"key": 27},
            {"key": ord("q")},
        ], initial={"charge_panel": "diagnostics"})
        states = result["states"]
        self.assertFalse(result["inspected"])  # No Enter layer or popup.
        self.assertEqual(states[1]["diagnostic_index"], 0)
        self.assertEqual(states[2]["diagnostic_index"], len(ports.diagnostics) - 1)
        expected_rows = asmond.port_diagnostic_rows(ports, len(ports.diagnostics) - 1)
        self.assertEqual(states[3]["diagnostic_scroll"], len(expected_rows) - 1)
        self.assertEqual(states[5]["charge_panel"], "cable")
        result["read"].assert_not_called()
        result["kill"].assert_not_called()

    def test_real_loop_selection_follows_identity_after_ioreg_reorder(self):
        first = asmond.mock_usb_c_stats(4)
        second = asmond.mock_usb_c_stats(4)
        second.diagnostics.reverse()
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=first), "key": 10},
            {"key": asmond.curses.KEY_RIGHT},
            {"update": asmond.SideMetricsUpdate(usb_c=second)},
            {"key": 27}, {"key": ord("q")},
        ], initial={"charge_panel": "diagnostics"})
        self.assertEqual(first.diagnostics[result["states"][2]["diagnostic_index"]].key, "USB-C 2")
        self.assertEqual(second.diagnostics[result["states"][3]["diagnostic_index"]].key, "USB-C 2")

    def test_liquid_warning_visible_without_diagnostic_panel_even_while_waiting(self):
        fixtures = LayoutTests()
        sample, history, memory, battery, usb, io, processes = fixtures.dashboard_data()
        usb.diagnostics[0].liquid_detected = True
        for current in (sample, None):
            window = fixtures.RecordingWindow(40, 100)
            asmond.draw_dashboard(
                window, current, history, memory, battery, usb, io, processes,
                fixtures.dashboard_colors(), fixtures.dashboard_args(), "live",
                "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery",
                0, None, (), False, False, 0, False, False, 0, "", False,
            )
            self.assertIn("LIQUID REPORTED: USB-C 1", window.rendered_row(1))

    def test_focus_layout_displays_diagnostics_at_all_supported_widths(self):
        fixtures = LayoutTests()
        for width in (72, 100, 181):
            window = fixtures.RecordingWindow(61, width)
            fixtures.draw_test_dashboard(window, fixtures.dashboard_args("focus"), charge_panel="diagnostics")
            self.assertIn("PORT DIAGNOSTICS", window.rendered())

    def test_menu_opens_diagnostics_without_sudo_or_new_probe(self):
        index = next(i for i, item in enumerate(asmond.MENU_ITEMS) if item[0] == "port_diagnostics")
        frames = [{"key": ord("m")}] + [{"key": asmond.curses.KEY_DOWN}] * index + [{"key": 10}, {"key": 27}, {"key": 27}, {"key": ord("q")}]
        with mock.patch.object(asmond, "read_charge_stats", side_effect=AssertionError("inspector must not probe")):
            result = ProcessRunLoopSafetyTests().run_process_loop(frames)
        self.assertFalse(result["inspected"])
        self.assertTrue(any(row["charge_panel"] == "diagnostics" for row in result["states"]))

    def test_direct_view_takes_focus_cancels_term_and_restores_previous_panel(self):
        runner = ProcessRunLoopSafetyTests()
        rows = runner.process_rows(gpu=True)
        result = runner.run_process_loop([
            {"update": asmond.SideMetricsUpdate(processes=rows, usb_c=asmond.mock_usb_c_stats(4)), "key": ord("k")},
            {"key": ord("b")},
            {"key": ord("k")}, {"key": ord("k")}, {"key": ord("p")},
            {"key": ord("m")}, {"key": asmond.curses.KEY_RIGHT}, {"key": 27},
            {"key": ord("h")}, {"key": asmond.curses.KEY_RIGHT}, {"key": 27},
            {"key": ord("T")}, {"key": 27},
            {"key": 27}, {"key": ord("q")},
        ], initial={"charge_panel": "cable"})
        states = result["states"]
        self.assertIsNotNone(result["rendered"][1]["pending"])
        for i in range(2, 14):
            self.assertEqual(states[i]["charge_panel"], "diagnostics")
            self.assertIsNone(result["rendered"][i]["pending"])
            self.assertTrue(result["rendered"][i]["poll_processes"])
            self.assertEqual(states[i]["diagnostic_index"], 0)
        self.assertEqual(states[14]["charge_panel"], "cable")
        self.assertEqual(result["args"].process_panel, "hidden")
        self.assertFalse(result["inspected"])
        result["kill"].assert_not_called()

    def test_b_continues_to_battery_and_q_quits_without_escape_layer(self):
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"key": ord("b")}, {"key": ord("q")},
        ], initial={"charge_panel": "diagnostics"})
        self.assertEqual(result["states"][1]["charge_panel"], "battery")
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"key": ord("q")},
        ], initial={"charge_panel": "diagnostics"})
        self.assertEqual(len(result["states"]), 1)

    def test_compact_diagnostics_geometry_and_render_preserve_other_panels(self):
        fixtures = LayoutTests()
        for layout_name in ("full", "compact", "focus", "custom"):
            for height in (24, 32, 61):
                for width in (72, 100, 181):
                    with self.subTest(layout=layout_name, height=height, width=width):
                        layout = asmond.diagnostic_dashboard_layout(height, width, layout_name)
                        diagnostic = layout.slot("diagnostics")
                        self.assertLessEqual(diagnostic.rect.h, asmond.CHARGE_PANEL_H)
                        self.assertLessEqual(diagnostic.rect.w, width if width < 100 else (width + 1) // 2)
                        self.assertLessEqual(diagnostic.rect.y + diagnostic.rect.h, height - 1)
                        self.assertIsNotNone(layout.panel("power"))
                        self.assertIsNotNone(layout.panel("thermals"))
                        base = asmond.dashboard_layout(height, width, layout_name, False, "hidden", charge_panel="usb")
                        charge = base.panel("charge")
                        if charge is not None and layout_name != "focus" and charge.rect.h >= 5:
                            self.assertEqual(diagnostic.rect, charge.rect)
                        for placement in layout.panels:
                            r = placement.rect
                            self.assertGreaterEqual(r.h, 5 if placement.slot == "diagnostics" else 3)
                            self.assertLessEqual(r.y + r.h, height - 1)
                        args = fixtures.dashboard_args(layout_name)
                        window = fixtures.RecordingWindow(height, width)
                        fixtures.draw_test_dashboard(window, args, process_panel="right", charge_panel="diagnostics")
                        rendered = window.rendered()
                        self.assertIn("PORT DIAGNOSTICS", rendered)
                        self.assertNotIn("Enter", rendered)
                        self.assertTrue(all(0 <= y < height and 0 <= x and x + len(text) <= width for y, x, text in window.entries))
        hidden_graph = asmond.diagnostic_dashboard_layout(24, 72, "custom", False)
        self.assertIsNone(hidden_graph.panel("power_graph"))
        self.assertEqual(hidden_graph.slot("upper_left").rect.y, 2)

    def test_compact_panel_port_is_only_in_header_and_all_rows_are_scrollable(self):
        usb = asmond.mock_usb_c_stats(4)
        rows = asmond.port_diagnostic_rows(usb, 0)
        self.assertFalse(any(label == "Port" for label, _value, _source in rows))
        for width in (36, 60, 90):
            for selected in range(len(rows)):
                window = LayoutTests.RecordingWindow(8, width)
                args = argparse.Namespace(mock=True, diagnostic_index=0, diagnostic_scroll=selected)
                asmond.draw_port_diagnostic_view(window, asmond.Rect(0, 0, 8, width), usb, args, LayoutTests.dashboard_colors())
                rendered = window.rendered()
                self.assertEqual(rendered.count("USB-C 1"), 1)
                self.assertTrue(any(text.startswith("› ") for _, _, text in window.entries))
                self.assertTrue(all(0 <= y < 8 and 0 <= x and x + len(text) <= width for y, x, text in window.entries))

    def test_missing_diagnostic_fields_are_hidden_but_zero_and_false_are_data(self):
        port = asmond_ports.diagnostic_snapshot("USB-C 1", "USB-C 1", {"LDCM_StateDescription": " ", "LDCM_MeasurementStatusDescription": "unknown", "PortControllerEvtBuffer": bytes(48)})
        usb = asmond.UsbCStats(diagnostics=[port], sampled_at=time.monotonic())
        self.assertEqual(asmond.port_diagnostic_rows(usb, 0), [])
        port.connected = False
        port.liquid_detected = False
        port.counters = {"Attach": 0}
        rows = {name: value for name, value, _source in asmond.port_diagnostic_rows(usb, 0)}
        self.assertEqual(rows["Connection"], "disconnected")
        self.assertEqual(rows["Liquid"], "not reported")
        self.assertEqual(rows["Attach"], "0")
        self.assertNotIn("Liquid state", rows)
        self.assertNotIn("Measurement", rows)
        self.assertNotIn("Counters", rows)
        self.assertFalse(any(name.startswith("PD raw") for name in rows))
        self.assertEqual(port.raw_trace, bytes(48))  # Presentation only, raw data retained.
        port.liquid_state = "Idle"
        self.assertIn("Liquid state", {name for name, _, _ in asmond.port_diagnostic_rows(usb, 0)})
        port.liquid_state = ""
        self.assertNotIn("Liquid state", {name for name, _, _ in asmond.port_diagnostic_rows(usb, 0)})

    def test_empty_trace_chunks_are_hidden_without_changing_offsets_or_real_zero_bytes(self):
        raw = bytes(20) + b"\x01" + bytes(27)
        port = asmond_ports.diagnostic_snapshot("controller:0", "Controller 1 (port unknown)", {"PortControllerEvtBuffer": raw}, mapped=False)
        rows = asmond.port_diagnostic_rows(asmond.UsbCStats(diagnostics=[port]), 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "PD raw +016")
        self.assertEqual(rows[0][1], raw[16:32].hex(" "))
        self.assertIn("not decoded", rows[0][2])
        self.assertNotIn("Mapping", {name for name, _, _ in rows})

    def test_empty_diagnostics_remain_safe_while_fields_appear_and_disappear(self):
        empty_port = asmond_ports.diagnostic_snapshot("USB-C 1", "USB-C 1", {})
        empty = asmond.UsbCStats(diagnostics=[empty_port])
        filled_port = asmond_ports.diagnostic_snapshot("USB-C 1", "USB-C 1", {"ConnectionActive": False, "LDCM_LiquidDetected": False, "Plug Event Count": 0})
        filled = asmond.UsbCStats(diagnostics=[filled_port])
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=empty), "key": asmond.curses.KEY_UP},
            {"key": asmond.curses.KEY_DOWN},
            {"update": asmond.SideMetricsUpdate(usb_c=filled), "key": asmond.curses.KEY_UP},
            {"update": asmond.SideMetricsUpdate(usb_c=empty), "key": asmond.curses.KEY_DOWN},
            {"key": ord("q")},
        ], with_sample=False, initial={"charge_panel": "diagnostics"})
        self.assertEqual(result["states"][1]["diagnostic_scroll"], 0)
        self.assertEqual(result["states"][3]["diagnostic_scroll"], 0)
        self.assertEqual(result["states"][4]["diagnostic_scroll"], 0)
        result["kill"].assert_not_called()
        for usb in (asmond.UsbCStats(), empty, filled):
            window = LayoutTests.RecordingWindow(8, 40)
            args = argparse.Namespace(mock=False, diagnostic_index=0, diagnostic_scroll=100)
            asmond.draw_port_diagnostic_view(window, asmond.Rect(0, 0, 8, 40), usb, args, LayoutTests.dashboard_colors())
            self.assertNotIn("unavailable", window.rendered())
            self.assertTrue(all(0 <= y < 8 and 0 <= x and x + len(text) <= 40 for y, x, text in window.entries))

    def test_visible_process_table_has_no_actions_or_selection_while_diagnostics_focused(self):
        window = LayoutTests.RecordingWindow(20, 100)
        asmond.draw_process_section(window, 0, 0, 20, 100, ProcessRunLoopSafetyTests.process_rows(), "cpu", 0, 12001, LayoutTests.dashboard_colors(), focused=False)
        rendered = window.rendered()
        self.assertIn("COMMAND", rendered)
        self.assertIn("focus: Port diagnostics", rendered)
        self.assertNotIn("k kill", rendered)
        self.assertNotIn("TERM", rendered)
        self.assertFalse(any(text == ">" for _, _, text in window.entries))

    def test_direct_view_works_before_main_sample_and_does_not_change_tailor(self):
        custom = {"cell_1": {"panel": "process", "detail": "detail"}}
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=asmond.mock_usb_c_stats(4)), "key": asmond.curses.KEY_RIGHT},
            {"key": 27}, {"key": ord("q")},
        ], with_sample=False, initial={"charge_panel": "diagnostics", "custom_layout": custom})
        self.assertEqual(result["states"][1]["diagnostic_index"], 1)
        self.assertEqual(result["args"].custom_layout, custom)
        self.assertEqual(result["args"].custom_grid, "columns")
        result["kill"].assert_not_called()
        fixtures = LayoutTests()
        window = fixtures.RecordingWindow(24, 72)
        asmond.draw_port_diagnostic_view(window, asmond.Rect(0, 0, 10, 72), asmond.UsbCStats(), argparse.Namespace(mock=False), fixtures.dashboard_colors())
        self.assertIn("Waiting for readings", window.rendered())

    def test_intel_battery_graph_does_not_invent_input_or_diagnostics(self):
        raw = {"ExternalConnected": True, "IsCharging": True, "InstantAmperage": 1547, "Voltage": 7743}
        battery = asmond.battery_stats_from_item(raw)
        usb = asmond.usb_c_stats_from_item(raw)
        self.assertEqual(usb.diagnostics, [])
        history = asmond.History(8)
        history.add_charge(battery, usb, 1)
        self.assertIsNone(history.charge_samples[-1][1])
        self.assertAlmostEqual(history.charge_samples[-1][2], 11.978421)


class ChargingGraphTests(unittest.TestCase):
    def test_menu_switches_graph_without_changing_sources_or_layout(self):
        index = next(i for i, item in enumerate(asmond.MENU_ITEMS) if item[0] == "graph_view")
        frames = [{"key": ord("m")}] + [{"key": asmond.curses.KEY_DOWN}] * index + [{"key": asmond.curses.KEY_RIGHT}, {"key": 27}, {"key": ord("q")}]
        result = ProcessRunLoopSafetyTests().run_process_loop(frames, initial={"graph_view": "power", "upper_power_mode": "soc"})
        self.assertEqual(result["args"].graph_view, "charging")
        self.assertEqual(result["args"].upper_power_mode, "soc")
        self.assertEqual(result["args"].layout, "custom")

    def test_history_only_accepts_measured_input_keeps_zero_sign_and_missing(self):
        history = asmond.History(8)
        history.add_charge(asmond.BatteryStats(power_mw=-2300), asmond.UsbCStats(adapter_contract_power_w=60), 1)
        history.add_charge(asmond.BatteryStats(power_mw=0), asmond.UsbCStats(system_power_w=0, external_connected=True), 2)
        history.add_charge(asmond.BatteryStats(), asmond.UsbCStats(system_power_w=99, external_connected=False), 3)
        history.add_charge(asmond.BatteryStats(power_mw=float("nan")), asmond.UsbCStats(system_power_w=float("inf")), 4)
        history.add_charge(asmond.BatteryStats(), asmond.UsbCStats(), 4)
        self.assertEqual(list(history.charge_samples), [(1, None, -2.3), (2, 0, 0), (3, None, None), (4, None, None)])
        history.resize(20)
        self.assertEqual(history.charge_samples.maxlen, 20)
        history.clear_power()
        self.assertFalse(history.charge_samples)

    def test_parser_preserves_genuine_zero_input_and_hides_disconnected_stale_data(self):
        for connected in (False, True):
            usb = asmond.usb_c_stats_from_item({"ExternalConnected": connected, "PowerTelemetryData": {"SystemPowerIn": 0, "SystemCurrentIn": 0}})
            self.assertEqual(usb.system_power_w, 0 if connected else None)
            self.assertEqual(usb.system_current_a, 0 if connected else None)

    def test_graph_signed_values_gaps_empty_and_small_geometry(self):
        colors = LayoutTests.dashboard_colors()
        history = asmond.History(64)
        now = time.monotonic()
        for i in range(30):
            history.add_charge(asmond.BatteryStats(power_mw=(i - 15) * 1000), asmond.UsbCStats(system_power_w=None if i == 20 else 25, external_connected=True), now - 30 + i)
        for width, height in ((30, 7), (72, 10), (181, 14), (25, 5)):
            window = LayoutTests.RecordingWindow(height, width)
            asmond.draw_charging_graph(window, asmond.Rect(0, 0, height, width), history, colors)
            if width >= 30:
                self.assertIn("CHARGING GRAPH", window.rendered())
                self.assertTrue(any(any(0x2800 <= ord(c) <= 0x28FF for c in text) for _, _, text in window.entries))
            self.assertTrue(all(0 <= y < height and 0 <= x and x + len(text) <= width for y, x, text in window.entries))
        window = LayoutTests.RecordingWindow(10, 120)
        asmond.draw_charging_graph(window, asmond.Rect(0, 0, 10, 120), asmond.History(8), colors)
        self.assertIn("waiting for charge samples", window.rendered())

    def test_graph_view_and_diagnostic_panel_settings_roundtrip(self):
        args = asmond.build_parser().parse_args(["--mock"])
        args.graph_view = "charging"
        args.charge_panel = "diagnostics"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(path, args, asmond.settings_schema()))
            restored = asmond_settings.load_settings(path, asmond.settings_schema())
            self.assertEqual(restored["graph_view"], "charging")
            self.assertEqual(restored["charge_panel"], "diagnostics")
            path.write_text('{"graph_view": []}')
            self.assertNotIn("graph_view", asmond_settings.load_settings(path, asmond.settings_schema()))


class GraphSelectionStyleTests(unittest.TestCase):
    def test_selection_uses_green_or_magenta_per_theme_not_warning_or_blue(self):
        for theme_name in asmond.THEMES:
            initialized = {}
            with (
                mock.patch.object(asmond.curses, "has_colors", return_value=True),
                mock.patch.object(asmond.curses, "start_color"),
                mock.patch.object(asmond.curses, "use_default_colors"),
                mock.patch.object(asmond.curses, "init_pair", side_effect=lambda pair, fg, _bg: initialized.update({pair: fg})),
                mock.patch.object(asmond.curses, "color_pair", side_effect=lambda pair: pair),
                mock.patch.object(asmond.curses, "COLORS", 256, create=True),
                mock.patch.object(asmond.curses, "COLOR_PAIRS", 256, create=True),
            ):
                colors = asmond.init_colors(theme_name)
            if theme_name == "mono":
                self.assertTrue(colors["selection"] & asmond.curses.A_UNDERLINE)
            else:
                self.assertIn(initialized[colors["selection"]], (asmond.curses.COLOR_GREEN, asmond.curses.COLOR_MAGENTA))
                self.assertNotEqual(colors["selection"], colors["warn"])
            if theme_name == "solar":
                self.assertEqual(initialized[colors["selection"]], asmond.curses.COLOR_GREEN)

    def test_selected_label_uses_one_color_including_its_shortcut(self):
        class Window(LayoutTests.RecordingWindow):
            def __init__(self):
                super().__init__(5, 90)
                self.attributes = []
            def addstr(self, y, x, text, attr=0):
                super().addstr(y, x, text, attr)
                self.attributes.append((text, attr))
        colors = {"selection": 1024, "warn": 2048, "muted": 4096, "fg": 8192}
        for label, shortcut in (("SoC", "S"), ("CPU", "C"), ("GPU", "G"), ("ANE", "A"), ("soc", "s")):
            win = Window()
            asmond.draw_label_hotkey(win, 0, 0, label, shortcut, colors, selected=True)
            self.assertEqual(win.attributes, [(label, colors["selection"] | asmond.curses.A_BOLD)])
            win = Window()
            asmond.draw_label_hotkey(win, 0, 0, label, shortcut, colors)
            self.assertIn((label[0], colors["warn"] | asmond.curses.A_BOLD), win.attributes)
            self.assertIn((label[1:], colors["muted"]), win.attributes)


class AppleSiliconCollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "tools/collect-apple-silicon-research.py"
        spec = importlib.util.spec_from_file_location("apple_silicon_collector", path)
        cls.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.collector)

    def test_privacy_preserves_numeric_zero_and_false_not_identities(self):
        raw = {
            "IORegistryEntryName": "Private external disk",
            "USB Serial Number": "PRIVATE123", "IOUserClientCreator": "pid 123 PrivateApp",
            "Statistics": {"Bytes (Read)": 0, "active": False},
            "nested": [{"UUID": "private", "payload": b"PRIVATE123"}],
            "temperature": float("nan"), "VDOs": [0x12345678, b"\x78\x56\x34\x12"],
            "port": {"IORegistryEntryName": "Port-USB-C@1"},
        }
        result = self.collector.clean(raw)
        self.assertNotIn("PRIVATE123", json.dumps(result))
        self.assertNotIn("Private", json.dumps(result))
        self.assertEqual(result["Statistics"], {"Bytes (Read)": 0, "active": False})
        self.assertEqual(result["nested"][0]["payload"], {"redacted_binary_bytes": 10})
        self.assertIsNone(result["temperature"])
        self.assertEqual(result["VDOs"], [0x12345678, 0x12345678])
        self.assertEqual(result["port"]["IORegistryEntryName"], "Port-USB-C@1")

    def test_network_keeps_link_counter_positions_without_addresses(self):
        raw = (
            "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\n"
            "en0 1500 <Link#4> aa:bb:cc:dd:ee:ff 12 0 1234 56 0 5678 0\n"
            "en0 1500 192.168.1.0 192.168.1.7 12 - 1234 56 - 5678 -\n"
            "en0 1500 fe80:: fe80::1234 12 - 1234 56 - 5678 -\n"
        ).encode()
        text = self.collector.network_rows(raw)
        self.assertNotIn("aa:bb", text)
        self.assertNotIn("192.168", text)
        self.assertNotIn("fe80", text)
        parts = text.splitlines()[1].split()
        self.assertEqual(parts[6], "1234")
        self.assertEqual(parts[9], "5678")
        self.assertEqual(len(text.splitlines()), 2)

    def test_settings_whitelist_omits_profile_names_and_arbitrary_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "show_io": False, "layout": "custom", "custom_grid": "columns",
                "custom_name": "PRIVATE PROFILE", "custom_profile": "PRIVATE PROFILE",
                "custom_layout": {"a": {"panel": "io"}}, "io_mode": ["bad"],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ASMOND_SETTINGS_DIR": directory}):
                result = self.collector.settings_visibility()
            self.assertEqual(result["custom_panel_counts"]["io"], 1)
            self.assertFalse(result["show_io"])
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertNotIn("io_mode", result)

    def test_sensor_worker_only_enumerates_and_reads_sensor_keys(self):
        import ctypes
        import struct

        calls = []
        keys = ("TC01", "F0Ac", "SSN ")

        class Reader:
            available = True
            connection = ctypes.c_uint32(1)
            def _call(self, data):
                calls.append(data.data8)
                output = asmond_smc._SMCKeyData()
                if data.data8 == 8:
                    output.key = int.from_bytes(keys[data.data32].encode(), "big")
                    return 0, output
                key = int(data.key).to_bytes(4, "big").decode()
                kind, raw = {
                    "#KEY": ("ui32", len(keys).to_bytes(4, "big")),
                    "TC01": ("flt ", struct.pack("<f", 43.25)),
                    "F0Ac": ("fpe2", (2000 * 4).to_bytes(2, "big")),
                }[key]
                output.key_info.data_size = len(raw)
                output.key_info.data_type = int.from_bytes(kind.encode(), "big")
                if data.data8 == 5:
                    for index, byte in enumerate(raw):
                        output.bytes[index] = byte
                return 0, output
            def close(self):
                pass

        smc = argparse.Namespace(AppleSMCTemperatureReader=Reader, _SMCKeyData=asmond_smc._SMCKeyData)
        app = argparse.Namespace(HID_TEMPS=argparse.Namespace(available=False))
        buffer = io.StringIO()
        original_path = list(sys.path)
        try:
            with mock.patch.object(self.collector.importlib, "import_module", side_effect=[smc, app]), mock.patch.object(self.collector.time, "sleep"), contextlib.redirect_stdout(buffer):
                self.collector.sensor_worker(Path("/test/libexec"))
        finally:
            sys.path[:] = original_path
        result = json.loads(buffer.getvalue())
        self.assertEqual(set(calls), {5, 8, 9})
        self.assertEqual(result["smc"]["keys"], ["F0Ac", "TC01"])
        self.assertEqual(result["smc"]["samples"][0]["values"]["F0Ac"]["value"], 2000)
        self.assertEqual(result["smc"]["samples"][0]["values"]["TC01"]["value"], 43.25)
        self.assertNotIn("SSN ", json.dumps(result))

    def test_worker_timeout_discards_unredacted_partial_output(self):
        with mock.patch.object(self.collector.subprocess, "run", side_effect=subprocess.TimeoutExpired("sensor", 45, output=b"PRIVATE")):
            code, output, error = self.collector.run(["sensor"], 45)
        self.assertEqual(code, 124)
        self.assertEqual(output, b"")
        self.assertNotIn(b"PRIVATE", error)

    def test_intel_mode_has_its_own_bundle_identity_and_rejects_rosetta(self):
        import tarfile

        calls = []
        def run(command, timeout=25):
            calls.append(command)
            if "sysctl.proc_translated" in command:
                return 1, b"", b"unknown oid"
            if "/usr/sbin/ioreg" in command or "/usr/sbin/system_profiler" in command:
                return 0, plistlib.dumps([]), b""
            if "--json" in command or "--sensor-worker" in command:
                return 0, b"{}", b""
            return 0, b"", b""
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(sys, "argv", ["collector", "--intel", "idle", directory, "--skip-sudo"]),
                mock.patch.object(self.collector.platform, "system", return_value="Darwin"),
                mock.patch.object(self.collector.platform, "machine", return_value="x86_64"),
                mock.patch.object(self.collector.os, "geteuid", return_value=501, create=True),
                mock.patch.object(self.collector.os, "umask"),
                mock.patch.object(self.collector, "installed_libexec", return_value=Path("/test/libexec")),
                mock.patch.object(self.collector.shutil, "which", return_value="/test/asmond"),
                mock.patch.object(self.collector, "settings_visibility", return_value={}),
                mock.patch.object(self.collector, "run", side_effect=run),
                mock.patch.object(self.collector.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.collector.main()
            archive = next(Path(directory).glob("asmond-intel-mac-idle-*.tgz"))
            with tarfile.open(archive) as tar:
                entry = next(item for item in tar.getmembers() if item.name.endswith("/capture.json"))
                metadata = json.load(tar.extractfile(entry))
            self.assertEqual(metadata["native_arch"], "x86_64")
            self.assertEqual(metadata["platform"], "intel-mac")
            self.assertEqual(metadata["collector_version"], 3)
        self.assertTrue(any("machdep.cpu.model" in command for command in calls))
        with (
            mock.patch.object(sys, "argv", ["collector", "--intel", "idle"]),
            mock.patch.object(self.collector.platform, "system", return_value="Darwin"),
            mock.patch.object(self.collector.platform, "machine", return_value="x86_64"),
            mock.patch.object(self.collector, "run", return_value=(0, b"1\n", b"")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as error:
                self.collector.main()
            self.assertEqual(error.exception.code, 2)

    def test_source_checkout_uses_current_python_without_an_installed_command(self):
        calls = []
        def run(command, timeout=25):
            calls.append(command)
            if "/usr/sbin/ioreg" in command or "/usr/sbin/system_profiler" in command:
                return 0, plistlib.dumps([]), b""
            if "--json" in command or "--sensor-worker" in command:
                return 0, b"{}", b""
            return 0, b"", b""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source checkout"
            source.mkdir()
            for name in ("asmond.py", "asmond_smc.py"):
                (source / name).touch()
            with (
                mock.patch.object(sys, "argv", ["collector", "idle", directory, "--source-dir", str(source), "--skip-sudo"]),
                mock.patch.object(self.collector.platform, "system", return_value="Darwin"),
                mock.patch.object(self.collector.platform, "machine", return_value="arm64"),
                mock.patch.object(self.collector.os, "geteuid", return_value=501, create=True),
                mock.patch.object(self.collector.os, "umask"),
                mock.patch.object(self.collector, "installed_libexec", side_effect=AssertionError("must not require installation")),
                mock.patch.object(self.collector.shutil, "which", side_effect=AssertionError("must not use installed command")),
                mock.patch.object(self.collector, "settings_visibility", return_value={}),
                mock.patch.object(self.collector, "run", side_effect=run),
                mock.patch.object(self.collector.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.collector.main()
            for command in calls:
                if "--json" in command or "--version" in command:
                    self.assertEqual(command[:2], [sys.executable, str(source.resolve() / "asmond.py")])
            worker = next(command for command in calls if "--sensor-worker" in command)
            self.assertEqual(worker[-1], str(source.resolve()))

    def test_partial_bundle_checksums_and_invalid_plists_fail_closed(self):
        import hashlib
        import tarfile

        commands = []
        def run(command, timeout=25):
            commands.append(command)
            if "/usr/sbin/ioreg" in command:
                return 0, b"PRIVATE invalid plist", b""
            if "--json" in command or "--sensor-worker" in command:
                return 0, b'{"ok": true}', b""
            return 0, b"", b""
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(sys, "argv", ["collector", "test", directory, "--skip-sudo"]),
                mock.patch.object(self.collector.platform, "system", return_value="Darwin"),
                mock.patch.object(self.collector.platform, "machine", return_value="arm64"),
                mock.patch.object(self.collector.os, "geteuid", return_value=501, create=True),
                mock.patch.object(self.collector.os, "umask"),
                mock.patch.object(self.collector, "installed_libexec", return_value=Path("/test/libexec")),
                mock.patch.object(self.collector.shutil, "which", return_value="/test/asmond"),
                mock.patch.object(self.collector, "settings_visibility", return_value={"show_io": False}),
                mock.patch.object(self.collector, "run", side_effect=run),
                mock.patch.object(self.collector.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.collector.main()
            archive = next(Path(directory).glob("*.tgz"))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(archive.with_suffix(".tgz.sha256").read_text().split()[0], digest)
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                self.assertTrue(all(item.uid == 0 and item.gid == 0 and item.mode == 0o600 for item in members))
                files = {Path(item.name).name: tar.extractfile(item).read() for item in members}
            self.assertNotIn(b"PRIVATE", b"".join(files.values()))
            self.assertIn("parse_error", json.loads(files["disk-0.status.json"]))
            self.assertNotIn("disk-0.json", files)
            self.assertFalse(json.loads(files["privileged-sampling.json"])["authorized"])
            for line in files["manifest.sha256"].decode().splitlines():
                expected, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(files[name]).hexdigest(), expected)
        self.assertTrue(all("/usr/bin/sudo" not in command for command in commands))


class M2MaxAuxiliaryTemperatureTests(unittest.TestCase):
    def reader(self, values):
        reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
        reader.available = True
        reader._lock = threading.Lock()
        reader.groups = asmond_smc.temperature_key_groups("Apple M2 Max", 8, 4, model="Mac14,5")
        reader.read_fans = True
        reader.raw_temperature_keys = asmond_smc.M2_MAX_RAW_TEMPERATURE_KEYS
        reader._read_value = values.get
        return reader

    def test_only_tested_m2_chassis_and_topology_get_auxiliary_sensors(self):
        groups = asmond_smc.temperature_key_groups("Apple M2 Max", 8, 4, model="Mac14,5")
        self.assertEqual(dict(groups.auxiliary), {"airport_proximity_c": "TW0P", "trackpad_c": "Ts0P", "trackpad_actuator_c": "Ts1P"})
        self.assertEqual((groups.cpu, groups.cpu_labels, groups.gpu), ((), (), ()))
        for brand, p, e, model in (("Apple M2 Max", 8, 4, ""), ("Apple M2 Max", 8, 4, "Mac14,6"), ("Apple M2 Max", 4, 8, "Mac14,5"), ("Apple M2 Pro", 8, 4, "Mac14,5"), ("Intel Core i7", 8, 4, "Mac14,5")):
            self.assertEqual(asmond_smc.temperature_key_groups(brand, p, e, model=model), asmond_smc.TemperatureKeyGroups())

    def test_native_model_detection_does_not_change_the_m4_gate(self):
        with mock.patch.object(asmond_smc.platform, "system", return_value="Darwin"), mock.patch.object(asmond_smc, "_sysctl_text", return_value="Mac14,5") as sysctl, mock.patch.object(asmond_smc.ctypes, "CDLL", side_effect=OSError("unavailable")):
            m2 = asmond_smc.AppleSMCTemperatureReader("Apple M2 Max", 8, 4)
            self.assertEqual(dict(m2.groups.auxiliary), asmond_smc.M2_MAX_MAC14_5_AUX_KEYS)
            sysctl.assert_called_once_with("hw.model")
            sysctl.reset_mock()
            m4 = asmond_smc.AppleSMCTemperatureReader("Apple M4", 4, 6)
            sysctl.assert_not_called()
            self.assertEqual(m4.groups, asmond_smc.temperature_key_groups("Apple M4", 4, 6))

    def test_comparison_values_populate_only_named_auxiliaries_not_raw_duplicates(self):
        for airport, trackpad, actuator in ((40.24456787109375, 26.375, 24.875), (52.322586, 28.75, 26.625)):
            values = {"TW0P": airport, "Ts0P": trackpad, "Ts1P": actuator, "TPSP": 53.072586, "TCMb": 77.523895, "TCMz": 83.487503, "Tp01": 6.7, "FNum": 2, "F0Ac": 0, "F1Ac": 0}
            result = self.reader(values).read()
            self.assertEqual((result.airport_proximity_c, result.trackpad_c, result.trackpad_actuator_c), (airport, trackpad, actuator))
            self.assertIsNone(result.power_supply_proximity_c)
            self.assertIsNone(result.cpu_avg_c)
            self.assertIsNone(result.cpu_max_c)
            self.assertIsNone(result.cpu_max_label)
            self.assertIsNone(result.gpu_avg_c)
            self.assertIsNone(result.gpu_max_c)
            self.assertEqual(dict(result.fan_rpms), {"F0Ac": 0, "F1Ac": 0})
            self.assertEqual(dict(result.raw_temperatures_c), {"TCMb": values["TCMb"], "TCMz": values["TCMz"], "TPSP": values["TPSP"], "Tp01": 6.7})

    def test_missing_or_invalid_auxiliary_never_reuses_previous_value(self):
        values = {"TW0P": 40, "Ts0P": 26.375, "Ts1P": 24.875, "FNum": 1, "F0Ac": 0}
        reader = self.reader(values)
        self.assertEqual(reader.read().trackpad_c, 26.375)
        for invalid in (None, 0, -1, float("nan"), float("inf"), 150):
            values["Ts0P"] = invalid
            sample = reader.read()
            self.assertIsNone(sample.trackpad_c)
            self.assertEqual(sample.airport_proximity_c, 40)
            self.assertEqual(sample.trackpad_actuator_c, 24.875)
            self.assertEqual(sample.fan_rpms, (("F0Ac", 0),))

    def test_m2_named_auxiliaries_flow_to_report_hotspot_and_tui_without_cpu_inference(self):
        received = self.reader({"TW0P": 52.322586, "Ts0P": 28.75, "Ts1P": 26.625, "TCMb": 120, "TCMz": 140}).read()
        sample = asmond.MetricSample()
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(received, asmond.HIDTemperatureSample(average_c=39, maximum_c=40, pmu_average_c=39, pmu_maximum_c=40))):
            asmond.apply_hid_temperatures(sample)
        self.assertEqual(asmond.thermal_hotspot(sample), (52.322586, "airport"))
        self.assertIsNone(sample.cpu_temp_avg_c)
        self.assertIsNone(sample.gpu_temp_avg_c)
        self.assertIsNone(sample.power_supply_temp_c)
        report = asmond.sample_to_report_dict(sample)
        self.assertEqual(report["trackpad_temp"], "28.8°C")
        self.assertEqual(report["airport_temp"], "52.3°C")
        self.assertEqual(report["cpu_temp_avg"], "n/a")
        self.assertEqual(report["raw_smc_temperatures_c"]["TCMb"], 120)
        self.assertNotIn("TEMP 140°C", asmond.build_alerts(sample, asmond.MemoryStats(), asmond.BatteryStats()))
        window = LayoutTests.RecordingWindow(9, 100)
        placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 9, 100), "normal")
        with mock.patch.object(asmond, "draw_box"):
            asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), LayoutTests.dashboard_colors())
        rendered = "\n".join(window.rendered_row(y) for y in range(9))
        for text in ("Trackpad", "28.8°C", "26.6°C", "Airport", "52.3°C"):
            self.assertIn(text, rendered)

    def test_m4_averages_and_hot_core_never_read_tcmb_or_tcmz(self):
        reader = self.reader({})
        reader.groups = asmond_smc.temperature_key_groups("Apple M4", 4, 6)
        reader.raw_temperature_keys = ()
        values = {key: 40 + index for index, key in enumerate(reader.groups.cpu)}
        values.update({key: 35 for key in reader.groups.gpu})
        values.update({key: 30 for _name, key in reader.groups.auxiliary})
        def read(key):
            self.assertNotIn(key, {"TCMb", "TCMz"})
            return values.get(key)
        reader._read_value = read
        sample = reader.read()
        self.assertEqual((sample.cpu_avg_c, sample.cpu_max_c, sample.cpu_max_label), (44.5, 49, "P9"))
        self.assertEqual((sample.gpu_avg_c, sample.gpu_max_c), (35, 35))


class M2TemperatureComparisonCollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("m2_comparison", Path(__file__).parent / "tools/compare-m2-max-temperatures.py")
        cls.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.collector)

    def raw_reader(self, kind, raw):
        calls = []
        def call(request):
            calls.append(request.data8)
            reply = asmond_smc._SMCKeyData()
            reply.key_info.data_size = len(raw)
            reply.key_info.data_type = int.from_bytes(kind.encode(), "big")
            reply.bytes[:len(raw)] = raw
            return 0, reply
        reader = self.collector.RawSMC(asmond_smc, types.SimpleNamespace(_call=call))
        return reader, calls

    def test_numeric_bytes_timestamps_and_offsets_are_preserved_without_type_guess(self):
        import struct
        cases = [("flt ", struct.pack("<f", 6.7), 6.7), ("flt ", struct.pack("<f", 0), 0), ("flt ", struct.pack("<f", float("nan")), None), ("sp78", b"\xff\x80", -0.5), ("fpe2", (10000).to_bytes(2, "big"), 2500), ("ioft", bytes.fromhex("33331e0000000000"), None)]
        for kind, raw, expected in cases:
            reader, calls = self.raw_reader(kind, raw)
            value = reader.read("TCMb")
            self.assertEqual(calls, [9, 5])
            self.assertEqual(value["raw_hex"], raw.hex())
            if expected is None:
                self.assertIsNone(value["value"])
            else:
                self.assertAlmostEqual(value["value"], expected, places=5)
            self.assertLessEqual(value["start"]["monotonic_ns"], value["end"]["monotonic_ns"])
            self.assertEqual(reader.read("dSSN")["status"], "failed")
            self.assertEqual(calls, [9, 5])
        reader, _ = self.raw_reader("ch8*", b"SECRET")
        self.assertNotIn("raw_hex", reader.read("TCMb"))

    def test_read_failure_invalidates_key_info_and_has_no_previous_value(self):
        reader, calls = self.raw_reader("ui16", b"\x00\x00")
        self.assertEqual(reader.read("F0Ac")["value"], 0)
        real_call = reader.reader._call
        reader.reader._call = lambda request: (1, asmond_smc._SMCKeyData())
        failed = reader.read("F0Ac")
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("value", failed)
        self.assertNotIn("F0Ac", reader.info)
        reader.reader._call = real_call
        self.assertEqual(reader.read("F0Ac")["value"], 0)
        self.assertEqual(calls, [9, 5, 9, 5])

    def test_discovery_is_bounded_sensor_only_and_uses_no_write_operation(self):
        reader, calls = self.raw_reader("ui32", (4).to_bytes(4, "big"))
        old_call = reader.reader._call
        def call(request):
            if request.data8 != 8:
                return old_call(request)
            calls.append(8)
            reply = asmond_smc._SMCKeyData()
            if request.data32 == 3:
                return 1, reply
            reply.key = int.from_bytes([b"TCMb", b"F0Ac", b"dSSN"][request.data32], "big")
            return 0, reply
        reader.reader._call = call
        keys, status = reader.discover()
        self.assertEqual(keys, ["F0Ac", "TCMb"])
        self.assertEqual(status, {"reported_key_count": 4, "index_read_failures": 1})
        self.assertTrue(set(calls) <= {5, 8, 9})
        oversized, calls = self.raw_reader("ui32", (8193).to_bytes(4, "big"))
        with self.assertRaises(RuntimeError):
            oversized.discover()
        self.assertEqual(calls, [9, 5])

    def test_record_has_all_phases_per_read_timing_and_json_null_not_fake_zero(self):
        module = self.collector
        now = [0.0]
        raw = types.SimpleNamespace(read=lambda key: {"status": "failed", "start": module.stamp(), "end": module.stamp()})
        cpu = types.SimpleNamespace(read_cpu_load_snapshot=lambda: object(), cpu_usage_from_snapshots=lambda prev, cur: None if prev is None else 37.5)
        handle, events = io.StringIO(), []
        def advance(delay):
            now[0] += delay
        with mock.patch.object(module.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(module, "stamp", side_effect=lambda: {"unix_ns": 1000000000 + int(now[0]*1e9), "monotonic_ns": int(now[0]*1e9)}), mock.patch.object(module.time, "sleep", side_effect=advance), mock.patch.object(module, "hid_readings", return_value={"values": []}), contextlib.redirect_stdout(io.StringIO()):
            count = module.record(handle, raw, ["TCMb"], object(), cpu, 1, 0.5, events)
        frames = [json.loads(line) for line in handle.getvalue().splitlines()]
        self.assertEqual(count, 10)
        self.assertEqual([event["phase"] for event in events], [p[0] for p in module.PHASES])
        self.assertEqual(frames[0]["host_cpu_usage_pct"], None)
        self.assertEqual(frames[1]["host_cpu_usage_pct"], 37.5)
        self.assertEqual({frame["phase"] for frame in frames}, {p[0] for p in module.PHASES})
        self.assertTrue(all("value" not in frame["smc"]["TCMb"] for frame in frames))

    def test_hid_unknown_names_are_not_exported_and_missing_readings_remain_null(self):
        hid = types.SimpleNamespace(available=True, client=1, product_key=2)
        hid.cf = types.SimpleNamespace(CFArrayGetCount=lambda services: 2, CFArrayGetValueAtIndex=lambda services, index: index + 1, CFRelease=mock.Mock())
        hid._service_name = lambda service, key: "Private Computer Name" if service == 1 else "PMU tdie0"
        hid.iokit = types.SimpleNamespace(IOHIDEventSystemClientCopyServices=lambda client: 3, IOHIDServiceClientCopyEvent=lambda service, *args: 4 if service == 1 else 0, IOHIDEventGetFloatValue=lambda *args: 39.5)
        app = types.SimpleNamespace(HID_TEMPS=hid, IOHID_TEMP_TYPE=15, IOHID_TEMP_FIELD=1)
        result = self.collector.hid_readings(app)
        self.assertNotIn("Private Computer", json.dumps(result))
        self.assertEqual(result["values"][0]["sensor"], "unclassified sensor")
        self.assertEqual(result["values"][0]["value"], 39.5)
        self.assertIsNone(result["values"][1]["value"])
        self.assertEqual(result["values"][1]["sensor"], "PMU tdie0")
        self.assertEqual(hid.cf.CFRelease.call_count, 2)

    def test_package_is_private_checksummed_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "capture"
            directory.mkdir()
            self.collector.write_json(directory / "metadata.json", {"status": "interrupted"})
            target = self.collector.package(directory, Path(root))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.with_suffix(".tgz.sha256").read_text().split()[0], hashlib.sha256(target.read_bytes()).hexdigest())
            with tarfile.open(target) as archive:
                self.assertTrue(all(m.uid == 0 and m.gid == 0 and m.mode == 0o600 and m.isfile() for m in archive))
                metadata = archive.extractfile("capture/metadata.json").read()
                manifest = archive.extractfile("capture/manifest.sha256").read().decode()
                self.assertEqual(manifest.split()[0], hashlib.sha256(metadata).hexdigest())
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                self.collector.package(directory, Path(root))
            self.assertEqual(target.read_bytes(), before)

    def test_wrong_machine_is_rejected_before_loading_runtime_or_writing_capture(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(sys, "argv", ["compare", "--source-dir", str(Path(__file__).parent), "--output-dir", root]), mock.patch.object(self.collector, "checked_identity", side_effect=RuntimeError("wrong machine")), mock.patch.object(self.collector.importlib, "import_module") as load, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.collector.main()
            load.assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_identity_requires_native_nonroot_exact_model_and_topology(self):
        module = self.collector
        values = {"hw.model": "Mac14,5", "machdep.cpu.brand_string": "Apple M2 Max", "hw.perflevel0.logicalcpu": "8", "hw.perflevel1.logicalcpu": "4", "kern.osversion": "26A434"}
        with mock.patch.object(module.platform, "system", return_value="Darwin"), mock.patch.object(module.platform, "machine", return_value="arm64"), mock.patch.object(module.os, "geteuid", return_value=501), mock.patch.object(module, "sysctl", side_effect=values.get):
            self.assertEqual(module.checked_identity()["model"], "Mac14,5")
            for key, invalid in (("hw.model", "Mac14,6"), ("machdep.cpu.brand_string", "Apple M2 Pro"), ("hw.perflevel0.logicalcpu", "4")):
                with mock.patch.dict(values, {key: invalid}), self.assertRaises(RuntimeError):
                    module.checked_identity()
            with mock.patch.object(module.os, "geteuid", return_value=0), self.assertRaises(RuntimeError):
                module.checked_identity()
            with mock.patch.object(module.platform, "machine", return_value="x86_64"), self.assertRaises(RuntimeError):
                module.checked_identity()
            with mock.patch.object(module.platform, "system", return_value="Linux"), self.assertRaises(RuntimeError):
                module.checked_identity()

    def test_interrupted_main_finalizes_received_samples_and_closes_reader(self):
        reader = types.SimpleNamespace(available=True, close=mock.Mock())
        fake_smc = types.SimpleNamespace(AppleSMCTemperatureReader=lambda: reader)
        module = self.collector
        def record(handle, *args):
            handle.write('{"phase":"idle","smc":{}}\n')
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as root, mock.patch.object(sys, "argv", ["compare", "--source-dir", str(Path(__file__).parent), "--output-dir", root]), mock.patch.object(module, "checked_identity", return_value={"model": "Mac14,5"}), mock.patch("builtins.input", return_value=""), mock.patch.object(module.importlib, "import_module", side_effect=[fake_smc, types.SimpleNamespace(VERSION="0.8.0-dev"), object()]), mock.patch.object(module.RawSMC, "discover", return_value=(["TCMb"], {})), mock.patch.object(module, "record", side_effect=record), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 130)
            reader.close.assert_called_once()
            with tarfile.open(next(Path(root).glob("*.tgz"))) as archive:
                content = {Path(m.name).name: archive.extractfile(m).read() for m in archive}
                self.assertEqual(json.loads(content["metadata.json"])["status"], "interrupted")
                self.assertEqual(json.loads(content["samples.jsonl"])["phase"], "idle")
            self.assertFalse(any(p.is_dir() for p in Path(root).iterdir()))


class M2MaxCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / "tests/fixtures/apple_m2_max_telemetry.json").read_text())

    def reader(self, values):
        reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
        reader.available = True
        reader._lock = threading.Lock()
        reader.groups = asmond_smc.temperature_key_groups("Apple M2 Max", 8, 4)
        reader.read_fans = True
        reader.raw_temperature_keys = asmond_smc.M2_MAX_RAW_TEMPERATURE_KEYS
        reader._read_value = values.get
        return reader

    def test_primary_port_source_remains_authoritative(self):
        primary = self.fixture["controllers"]
        with mock.patch.object(asmond, "read_ioreg_class_items", return_value=primary) as read:
            self.assertIs(asmond.read_hpm_port_items(), primary)
        read.assert_called_once_with("AppleHPMInterface")

    def test_real_tc_fallback_populates_cable_link_dp_and_physical_ports(self):
        fixture = self.fixture
        sources = {
            "AppleHPMInterface": [], "AppleTCController": fixture["controllers"],
            "IOPortTransportComponentCCUSBPDSOPp": fixture["cables"],
            "IOPortTransportComponentCCUSBPDSOPpp": [],
            "AppleUSBXHCIPort": fixture["usb_ports"], "IOUSBHostDevice": fixture["usb_devices"],
        }
        with mock.patch.object(asmond, "read_battery_items", return_value=fixture["battery"]), mock.patch.object(asmond, "read_ioreg_class_items", side_effect=lambda name: sources[name]):
            _, stats = asmond.read_charge_stats()
        ports = {p["label"]: p for p in asmond.usb_to_report_dict(stats)["ports"]}
        self.assertTrue(ports["USB-C 1"]["connected"])
        self.assertFalse(ports["USB-C 2"]["connected"])
        self.assertTrue(ports["USB-C 3"]["connected"])
        self.assertEqual(ports["USB-C 1"]["data_link_speed_bps"], 10000000000)
        self.assertEqual(ports["USB-C 1"]["cable_current"], "5 A")
        self.assertEqual(ports["USB-C 1"]["cable_max_power"], "100.0 W")
        self.assertEqual(ports["USB-C 3"]["data_link_speed_bps"], 5000000000)
        self.assertEqual(ports["USB-C 3"]["dp_alt_mode"], "active")
        # This real fixture has no controller-to-physical-port identity. Its
        # cable/link data is verified; the charge contract must stay unmapped.
        self.assertIsNone(stats.active_port)
        reordered = asmond.usb_c_stats_from_item(fixture["battery"], list(reversed(fixture["controllers"])), fixture["cables"], fixture["usb_ports"], fixture["usb_devices"])
        self.assertEqual(asmond.usb_to_report_dict(reordered), asmond.usb_to_report_dict(stats))

    def test_each_actual_fan_is_preserved_and_raw_temperatures_are_not_mapped(self):
        for values in self.fixture["smc_samples"]:
            result = self.reader(values).read()
            self.assertEqual(dict(result.fan_rpms), {"F0Ac": values["F0Ac"], "F1Ac": values["F1Ac"]})
            self.assertNotEqual(dict(result.fan_rpms)["F1Ac"], values["F1Tg"])
            self.assertEqual(dict(result.raw_temperatures_c)["Tp1A"], 0)
            self.assertIsNone(result.cpu_avg_c)
            self.assertIsNone(result.gpu_avg_c)
            self.assertIsNone(result.cpu_max_label)

    def test_missing_failed_zero_and_invalid_fans_keep_their_actual_indices(self):
        reader = self.reader({"FNum": 2, "F0Ac": 0, "F1Ac": 2500})
        self.assertEqual(dict(reader.read().fan_rpms), {"F0Ac": 0, "F1Ac": 2500})
        for invalid in (None, -1, float("nan"), float("inf")):
            reader._read_value = {"FNum": 2, "F0Ac": invalid, "F1Ac": 2500}.get
            self.assertEqual(reader.read().fan_rpms, (("F1Ac", 2500),))
        def read(key):
            if key == "F0Ac":
                raise OSError("fan read failed")
            return {"FNum": 2, "F1Ac": 2500}.get(key)
        reader._read_value = read
        self.assertEqual(reader.read().fan_rpms, (("F1Ac", 2500),))
        for count in (None, -1, 2.5, 17, float("nan")):
            reader._read_value = {"FNum": count, "F1Ac": 2500}.get
            self.assertEqual(reader.read().fan_rpms, ())

    def test_raw_temperatures_cannot_affect_cpu_gpu_hotspot_or_alerts(self):
        received = asmond_smc.SMCTemperatureSample(fan_rpms=(("F0Ac", 0), ("F1Ac", 2500)), raw_temperatures_c=(("TCMz", 120), ("Tp1A", 0)))
        sample = asmond.MetricSample(fan_rpm=1196, temperature_source="IOHID")
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(received, asmond.HIDTemperatureSample(average_c=46, maximum_c=47))):
            asmond.apply_hid_temperatures(sample)
        self.assertEqual(sample.temp_max_c, 47)
        self.assertIsNone(sample.cpu_temp_avg_c)
        self.assertIsNone(sample.gpu_temp_avg_c)
        self.assertEqual(sample.fan_rpm, 1196)
        self.assertEqual(sample.fan_rpms, {"F0Ac": 0, "F1Ac": 2500})
        self.assertEqual(sample.raw_smc_temperatures_c, {"TCMz": 120, "Tp1A": 0})
        report = asmond.sample_to_report_dict(sample)
        self.assertEqual(report["fan_rpms"]["F0Ac"], 0)
        self.assertEqual(report["raw_smc_temperatures_c"]["TCMz"], 120)
        rows = asmond.data_source_rows(argparse.Namespace(mock=False), sample, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        fields = {name: (value, source) for name, value, source in rows}
        self.assertEqual(fields["Fan 0"][0], "0 rpm")
        self.assertIn("F1Ac", fields["Fan 1"][1])
        self.assertIn("unverified", fields["SMC TCMz"][1])
        self.assertNotIn("Fan", fields)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asmond.print_sample(sample)
        self.assertIn("Fan 0 (F0Ac): 0 rpm", output.getvalue())
        self.assertIn("Fan 1 (F1Ac): 2500 rpm", output.getvalue())
        self.assertIn("Raw SMC temperatures (unassigned", output.getvalue())
        self.assertNotIn("Fan:          1196", output.getvalue())

    def test_both_fans_render_inside_the_existing_thermal_box(self):
        window = LayoutTests.RecordingWindow(7, 100)
        sample = asmond.MetricSample(fan_rpms={"F0Ac": 2303, "F1Ac": 2512}, thermal_pressure="Nominal", throttled=False, pmu_temp_avg_c=46, temp_max_c=47, temp_max_source="PMU")
        placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 7, 100), "normal")
        with mock.patch.object(asmond, "draw_box"):
            asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), LayoutTests.dashboard_colors())
        rendered = "\n".join(window.rendered_row(y) for y in range(7))
        for value in ("Fan 0", "Fan 1", "2303 rpm", "2512 rpm", "Pressure", "Throttled", "Hot"):
            self.assertIn(value, rendered)

    def test_smc_integer_and_fixed_point_abi_decoding_is_read_only(self):
        import struct
        entries = [("ui8 ", b"\x02", 2), ("ui16", b"\x01\x00", 256), ("ui32", b"\x00\x00\x01\x00", 256), ("fpe2", (2500 * 4).to_bytes(2, "big"), 2500), ("flt ", struct.pack("<f", 2310.5), 2310.5), ("flt ", b"\x00\x00", None)]
        for kind, raw, expected in entries:
            reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
            reader._key_info = {}
            calls = []
            def call(data):
                calls.append(data.data8)
                output = asmond_smc._SMCKeyData()
                output.key_info.data_size = len(raw)
                output.key_info.data_type = int.from_bytes(kind.encode(), "big")
                for index, byte in enumerate(raw):
                    output.bytes[index] = byte
                return 0, output
            reader._call = call
            self.assertEqual(reader._read_value("F0Ac"), expected)
            self.assertEqual(calls, [9, 5])

    def test_m2_raw_key_gate_is_exact_and_native_intel_only_opens_for_fans(self):
        with mock.patch.object(asmond_smc.platform, "system", return_value="Darwin"), mock.patch.object(asmond_smc.ctypes, "CDLL", side_effect=OSError("unavailable")):
            m2 = asmond_smc.AppleSMCTemperatureReader("Apple M2 Max", 8, 4)
            other = asmond_smc.AppleSMCTemperatureReader("Apple M2 Pro", 8, 4)
            topology = asmond_smc.AppleSMCTemperatureReader("Apple M2 Max", 4, 8)
        self.assertEqual(m2.raw_temperature_keys, asmond_smc.M2_MAX_RAW_TEMPERATURE_KEYS)
        self.assertEqual(other.raw_temperature_keys, ())
        self.assertEqual(topology.raw_temperature_keys, ())
        with mock.patch.object(asmond_smc.platform, "system", return_value="Darwin"), mock.patch.object(asmond_smc.platform, "machine", return_value="x86_64"), mock.patch.object(asmond_smc.ctypes, "CDLL", side_effect=OSError("unavailable")) as open_library:
            intel = asmond_smc.AppleSMCTemperatureReader("Intel Core i7", 4, 0)
        open_library.assert_called_once()
        self.assertTrue(intel.read_fans)
        self.assertEqual(intel.groups, asmond_smc.TemperatureKeyGroups())
        self.assertEqual(intel.raw_temperature_keys, ())
        self.assertEqual(intel.read(), asmond_smc.SMCTemperatureSample())

    def test_native_smc_rejects_short_successful_responses(self):
        import ctypes

        reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
        reader.connection = ctypes.c_uint32(1)
        def method(*args):
            args[-1]._obj.value = 79
            return 0
        reader.iokit = argparse.Namespace(IOConnectCallStructMethod=method)
        code, _output = reader._call(asmond_smc._SMCKeyData())
        self.assertNotEqual(code, asmond_smc.KERN_SUCCESS)


class IntelProCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / "tests/fixtures/intel_macbookpro_telemetry.json").read_text())

    def test_temperature_power_targets_are_never_measured_component_power(self):
        raw = self.fixture["powermetrics"]
        sample = asmond.sample_from_plist(raw)
        self.assertAlmostEqual(sample.soc_power_mw, 3734.73)
        self.assertIsNone(sample.cpu_power_mw)
        self.assertIsNone(sample.gpu_power_mw)
        self.assertEqual(sample.soc_temp_c, 44.34)
        self.assertEqual(sample.fan_rpm, 7976)
        self.assertFalse(sample.throttled)
        for component in ("cpu", "gpu", "ane", "media", "battery", "soc"):
            with self.subTest(component=component):
                for suffix in ("die_power_target", "thermal_power", "temperature_power", "power_threshold"):
                    sample = asmond.sample_from_plist({f"{component}_{suffix}": 98})
                    self.assertIsNone(getattr(sample, f"{component}_power_mw"))
        measured = asmond.sample_from_plist({**raw, "processor": {"package_watts": 3.73473, "cpu_watts": 1.25, "igpu_watts": 0.1}})
        self.assertEqual(measured.cpu_power_mw, 1250)
        self.assertEqual(measured.gpu_power_mw, 100)
        generic = asmond.sample_from_plist({"cpu_power": 1250, "gpu_power": 100})
        self.assertEqual(generic.cpu_power_mw, 1250)
        self.assertEqual(generic.gpu_power_mw, 100)

    def test_adapter_only_input_does_not_invent_magsafe_or_a_port(self):
        stats = asmond.usb_c_stats_from_item(self.fixture["adapter_only"])
        report = asmond.usb_to_report_dict(stats)
        self.assertEqual(report["input_type"], "AC Input")
        self.assertEqual(report["active_port"], "AC Input")
        self.assertEqual(report["ports"], [])
        self.assertEqual(report["voltage"], "20.0 V")
        self.assertEqual(report["current"], "3.25 A")
        self.assertEqual(report["adapter_rating"], "65.0 W")
        unplugged = asmond.usb_c_stats_from_item({**self.fixture["adapter_only"], "ExternalConnected": False})
        self.assertEqual(unplugged.input_kind, "")
        self.assertFalse(asmond.charge_input_active(unplugged))

    def test_actual_intel_fans_are_not_clamped_to_targets_or_maximums(self):
        for values in self.fixture["smc_samples"]:
            reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
            reader.available = True
            reader._lock = threading.Lock()
            reader.groups = asmond_smc.TemperatureKeyGroups()
            reader.read_fans = True
            reader.raw_temperature_keys = ()
            reader._read_value = values.get
            result = reader.read()
            self.assertEqual(dict(result.fan_rpms), {"F0Ac": values["F0Ac"], "F1Ac": values["F1Ac"]})
            self.assertGreater(dict(result.fan_rpms)["F1Ac"], values["F1Mx"])
            self.assertIsNone(result.cpu_avg_c)
            self.assertIsNone(result.gpu_avg_c)
            self.assertEqual(result.raw_temperatures_c, ())

    def test_complete_intel_temperatures_do_not_skip_fans_in_report_probe_or_stream(self):
        payload = plistlib.dumps(self.fixture["powermetrics"]) + b"\0"
        smc = asmond_smc.SMCTemperatureSample(fan_rpms=(("F0Ac", 2154), ("F1Ac", 7973)))
        args = argparse.Namespace(mock=False, live=True, interval=1.0, raw=False)
        with (
            mock.patch.object(asmond, "ensure_powermetrics_access"),
            mock.patch.object(asmond.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, payload, b"")),
            mock.patch.object(asmond, "read_cpu_load_snapshot", return_value=asmond.CpuLoadSnapshot((1, 1, 1, 1))),
            mock.patch.object(asmond.SMC_TEMPS, "read", return_value=smc),
            mock.patch.object(asmond, "apply_hid_temperatures") as enrich_temperatures,
        ):
            sample = asmond.collect_report_sample(args)
            self.assertEqual(sample.fan_rpms, {"F0Ac": 2154, "F1Ac": 7973})
            self.assertEqual(sample.soc_temp_c, 44.34)
            self.assertEqual(sample.temp_max_c, 44.34)
            self.assertIsNone(sample.temperature_source)
            self.assertEqual(sample.fan_rpm, 7976)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(asmond.run_probe(args), 0)
            self.assertIn("Fan 0 (F0Ac): 2154 rpm", output.getvalue())
            self.assertIn("Fan 1 (F1Ac): 7973 rpm", output.getvalue())
            enrich_temperatures.assert_not_called()
        process = argparse.Namespace(stdout=io.BytesIO(payload), stderr=io.BytesIO(), poll=lambda: 0)
        stream = asmond.PowerMetricsStream(1000)
        stream.temperature_cache = mock.Mock()
        stream.temperature_cache.snapshot.return_value = (smc, asmond.HIDTemperatureSample(maximum_c=100))
        with (
            mock.patch.object(asmond, "refresh_sudo_credentials", return_value=(True, "")),
            mock.patch.object(asmond.subprocess, "Popen", return_value=process),
            mock.patch.object(asmond, "apply_hid_temperatures") as enrich_temperatures,
        ):
            samples = list(stream.samples())
        stream.temperature_cache.refresh_async.assert_called_once()
        self.assertEqual(samples[0].fan_rpms, {"F0Ac": 2154, "F1Ac": 7973})
        self.assertEqual(samples[0].temp_max_c, 44.34)
        self.assertEqual(samples[0].soc_temp_c, 44.34)
        enrich_temperatures.assert_not_called()

    def test_indexed_fan_failure_preserves_legacy_scalar_and_thermal_fields(self):
        sample = asmond.sample_from_plist(self.fixture["powermetrics"])
        with mock.patch.object(asmond.SMC_TEMPS, "read", side_effect=OSError("unavailable")):
            asmond.apply_indexed_fans(sample)
        self.assertEqual(sample.fan_rpms, {})
        self.assertEqual(sample.fan_rpm, 7976)
        self.assertEqual(sample.soc_temp_c, 44.34)
        self.assertEqual(sample.temp_max_c, 44.34)

    def test_both_intel_fans_remain_visible_with_long_performance_reasons(self):
        sample = asmond.sample_from_plist(self.fixture["powermetrics"])
        sample.fan_rpms = {"F0Ac": 2154, "F1Ac": 7973}
        for width in (36, 48, 50, 72, 100):
            with self.subTest(width=width):
                window = LayoutTests.RecordingWindow(7, width)
                placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 7, width), "normal")
                with mock.patch.object(asmond, "draw_box"):
                    asmond.draw_thermals_panel(window, placement, sample, asmond.BatteryStats(), LayoutTests.dashboard_colors())
                rendered = window.rendered()
                for text in ("Fan 0", "Fan 1", "2154 rpm", "7973 rpm", "Pressure", "Throttled"):
                    self.assertIn(text, rendered)
                self.assertTrue(all(0 <= y < 7 and 0 <= x and x + len(text) <= width for y, x, text in window.entries))


class DiagnosticWrappingTests(unittest.TestCase):
    def test_all_raw_bytes_and_long_fields_survive_narrow_wrapped_views(self):
        raw = bytes(range(1, 49))
        port = asmond_ports.diagnostic_snapshot("controller:0", "Controller 1 (port unknown)", {"PortControllerEvtBuffer": raw}, mapped=False)
        port.counters = {"Very long counter name for testing": 12345678901234567890}
        port.events = [(1, "connection state changed from disconnected to connected and back again")]
        usb = asmond.UsbCStats(diagnostics=[port])
        for width in (30, 36, 50, 90):
            rows = asmond.port_diagnostic_view_rows(usb, 0, width)
            recovered = bytearray(len(raw))
            for name, value, _source in rows:
                if name.startswith("PD raw +"):
                    offset = int(name.removeprefix("PD raw +"))
                    chunk = bytes.fromhex(value)
                    recovered[offset:offset + len(chunk)] = chunk
            self.assertEqual(bytes(recovered), raw)
            logical = asmond.port_diagnostic_rows(usb, 0)
            first_raw = next(index for index, row in enumerate(rows) if row[0].startswith("PD raw"))
            # Wrapped non-hex content retains all text, including long labels.
            self.assertEqual("".join(name for name, _, _ in rows[:first_raw]), "".join(name for name, _, _ in logical if not name.startswith("PD raw")))
            self.assertEqual("".join(value for _, value, _ in rows[:first_raw]), "".join(value for name, value, _ in logical if not name.startswith("PD raw")))
            for selected in range(len(rows)):
                window = LayoutTests.RecordingWindow(8, width)
                args = argparse.Namespace(mock=False, diagnostic_index=0, diagnostic_scroll=selected)
                with mock.patch.object(asmond, "draw_charge_box"):
                    asmond.draw_port_diagnostic_view(window, asmond.Rect(0, 0, 8, width), usb, args, LayoutTests.dashboard_colors())
                selected_line = next(text for _, _, text in window.entries if text.startswith("› "))
                self.assertTrue(selected_line.endswith(rows[selected][1]))
                self.assertTrue(all(0 <= y < 8 and 0 <= x and x + len(text) <= width for y, x, text in window.entries))

    def test_arrow_navigation_uses_wrapped_row_count(self):
        usb = asmond.mock_usb_c_stats(4)
        fixtures = ProcessRunLoopSafetyTests()
        width = asmond.diagnostic_dashboard_layout(40, 120, "full").slot("diagnostics").rect.w
        count = len(asmond.port_diagnostic_view_rows(usb, 0, width))
        result = fixtures.run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=usb), "key": asmond.curses.KEY_UP},
            {"key": asmond.curses.KEY_DOWN},
            {"key": ord("q")},
        ], with_sample=False, initial={"charge_panel": "diagnostics"})
        self.assertEqual(result["states"][1]["diagnostic_scroll"], count - 1)
        self.assertEqual(result["states"][2]["diagnostic_scroll"], 0)


class CableNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / "tests/fixtures/apple_m2_max_telemetry.json").read_text())

    def stats(self, *, reverse=False):
        f = self.fixture
        return asmond.usb_c_stats_from_item(f["battery"], list(reversed(f["controllers"])) if reverse else f["controllers"], f["cables"], f["usb_ports"], f["usb_devices"])

    def test_cycle_connected_ports_including_data_only_without_emarker_and_wrap(self):
        usb = self.stats()
        self.assertEqual([p.label for p in asmond.connected_cable_ports(usb)], ["USB-C 1", "USB-C 3"])
        self.assertEqual(asmond.selected_cable_port(usb).label, "USB-C 1")
        self.assertEqual(asmond.cycle_cable_port(usb, None, 1), "USB-C 3")
        self.assertEqual(asmond.cycle_cable_port(usb, "USB-C 3", 1), "USB-C 1")
        self.assertEqual(asmond.cycle_cable_port(usb, "USB-C 1", -1), "USB-C 3")
        other = asmond.selected_cable_port(usb, "USB-C 3")
        self.assertIsNone(other.cable_info)
        self.assertIn("DisplayPort", other.active_transports)
        self.assertEqual(asmond.connected_cable_ports(asmond.UsbCStats()), [])
        self.assertIsNone(asmond.cycle_cable_port(asmond.UsbCStats(), None, 1))

    def test_manual_selection_survives_reorder_and_rejects_ambiguous_or_absent_identity(self):
        usb = self.stats(reverse=True)
        self.assertEqual(asmond.selected_cable_port(usb, "USB-C 3").label, "USB-C 3")
        self.assertEqual(asmond.selected_cable_port(usb, "USB-C 2").label, "USB-C 1")
        usb.ports.append(asmond.UsbCPortStats("USB-C 3", connected=True, cable_query_supported=True))
        self.assertNotIn("USB-C 3", [p.label for p in asmond.connected_cable_ports(usb)])
        self.assertEqual(asmond.selected_cable_port(usb, "USB-C 3").label, "USB-C 1")

    def test_selected_port_counter_data_and_dp_render_without_changing_input(self):
        usb = self.stats()
        before = asmond.usb_to_report_dict(usb)
        for width in (24, 32, 46, 58, 90):
            window = LayoutTests.RecordingWindow(8, width + 4)
            asmond.draw_cable_section(window, 1, 2, 6, width, usb, LayoutTests.dashboard_colors(), selected_label="USB-C 3")
            rendered = window.rendered()
            self.assertIn("‹ USB-C 3 › 2/2", rendered)
            self.assertIn("no E-marker", rendered)
            self.assertIn("active", rendered)
            self.assertTrue(all(1 <= y < 7 and 2 <= x and x + len(text) <= width + 2 for y, x, text in window.entries))
        self.assertEqual(asmond.usb_to_report_dict(usb), before)
        self.assertIsNone(usb.active_port)

    def test_real_loop_cycles_without_sorting_and_cancels_pending_term(self):
        runner = ProcessRunLoopSafetyTests()
        result = runner.run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=self.stats(), processes=runner.process_rows()), "key": ord("k")},
            {"key": asmond.curses.KEY_RIGHT},
            {"key": asmond.curses.KEY_RIGHT},
            {"key": asmond.curses.KEY_LEFT},
            {"key": ord("b")},
            {"key": ord("q")},
        ], preferred="cpu", initial={"layout": "full", "process_panel": "right", "charge_panel": "cable"})
        self.assertIsNotNone(result["rendered"][1]["pending"])
        self.assertEqual([s["cable_port_label"] for s in result["states"][:5]], [None, None, "USB-C 3", "USB-C 1", "USB-C 3"])
        self.assertTrue(all(s["sort"] == "cpu" for s in result["rendered"]))
        self.assertTrue(all(s["pending"] is None for s in result["rendered"][2:]))
        result["kill"].assert_not_called()

    def test_real_loop_preserves_selection_and_recovers_after_unplug(self):
        unplugged = self.stats()
        next(p for p in unplugged.ports if p.label == "USB-C 3").connected = False
        result = ProcessRunLoopSafetyTests().run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=self.stats()), "key": asmond.curses.KEY_RIGHT},
            {"update": asmond.SideMetricsUpdate(usb_c=self.stats(reverse=True))},
            {"update": asmond.SideMetricsUpdate(usb_c=unplugged)},
            {"update": asmond.SideMetricsUpdate(usb_c=asmond.UsbCStats())},
            {"key": ord("q")},
        ], initial={"layout": "full", "charge_panel": "cable"})
        self.assertEqual([s["cable_port_label"] for s in result["states"]], [None, "USB-C 3", None, None, None])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertIsNone(asmond_settings.save_settings(path, result["args"], asmond.settings_schema()))
            self.assertNotIn("cable_port_label", json.loads(path.read_text()))

    def test_menus_help_tailor_and_inspector_take_priority(self):
        custom = {"cell_1": {"panel": "process", "detail": "detail"}, "cell_2": {"panel": "cable", "detail": "normal"}}
        index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == "sources")
        frames = [
            {"update": asmond.SideMetricsUpdate(usb_c=self.stats()), "key": ord("h")},
            {"key": asmond.curses.KEY_RIGHT}, {"key": 27},
            {"key": ord("T")}, {"key": asmond.curses.KEY_RIGHT}, {"key": 27},
            {"key": ord("m")},
        ]
        frames += [{"key": asmond.curses.KEY_DOWN}] * index
        frames += [{"key": 10}, {"key": asmond.curses.KEY_RIGHT}, {"key": 27}, {"key": 27}, {"key": ord("q")}]
        result = ProcessRunLoopSafetyTests().run_process_loop(frames, initial={"custom_layout": custom, "charge_panel": "cable"})
        self.assertTrue(all(s["cable_port_label"] is None for s in result["states"]))
        result["kill"].assert_not_called()

    def test_visible_fixed_tailor_and_focus_cable_support_navigation_but_hidden_does_not(self):
        usb = self.stats()
        custom = {"cell_1": {"panel": "cable", "detail": "normal"}}
        for name, width, current, expected in (
            ("full", 100, "cable", True), ("compact", 100, "cable", True),
            ("full", 100, "battery", False), ("focus", 181, "battery", True),
            ("focus", 100, "battery", False), ("focus", 72, "cable", True),
            ("custom", 181, "battery", True),
        ):
            with self.subTest(layout=name, width=width, current=current):
                layout = asmond.dashboard_layout(61, width, name, False, "hidden", custom, current, "columns")
                self.assertEqual(asmond.cable_navigation_visible(layout, name, current, usb, width), expected)
                self.assertFalse(asmond.cable_navigation_visible(layout, name, "diagnostics", usb, width))
        hidden = ProcessRunLoopSafetyTests().run_process_loop([
            {"update": asmond.SideMetricsUpdate(usb_c=usb, processes=ProcessRunLoopSafetyTests.process_rows()), "key": asmond.curses.KEY_RIGHT},
            {"key": ord("q")},
        ], preferred="cpu", initial={"layout": "full", "process_panel": "right", "charge_panel": "battery"})
        self.assertIsNone(hidden["args"].cable_port_label)
        self.assertEqual(hidden["rendered"][1]["sort"], "ram")

    def test_all_layouts_render_manual_selection_and_process_hint_is_truthful(self):
        fixtures = LayoutTests()
        sample, history, memory, battery, _usb, io_stats, processes = fixtures.dashboard_data()
        for name, width in (("full", 100), ("compact", 100), ("focus", 72), ("focus", 100), ("focus", 181), ("custom", 181)):
            args = fixtures.dashboard_args(name)
            args.cable_port_label = "USB-C 3"
            args.custom_grid = "columns"
            args.custom_layout = {"cell_1": {"panel": "cable", "detail": "normal"}}
            window = fixtures.RecordingWindow(61, width)
            with mock.patch.object(asmond, "draw_box"), mock.patch.object(asmond, "draw_charge_box"):
                asmond.draw_dashboard(window, sample, history, memory, battery, self.stats(), io_stats, processes, fixtures.dashboard_colors(), args, "live", "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "cable", 0, None, (), False, False, 0, False, False, 0, "", False)
            self.assertIn("‹ USB-C 3 › 2/2", window.rendered())
        window = fixtures.RecordingWindow(20, 140)
        with mock.patch.object(asmond, "draw_box_hotkey_title"):
            asmond.draw_process_section(window, 0, 0, 20, 140, processes, "cpu", 0, None, fixtures.dashboard_colors(), sort_arrows=False)
        self.assertIn("←→ Cable", window.rendered())
        self.assertNotIn("arrows move/sort", window.rendered())


class PresentationPreferenceTests(unittest.TestCase):
    @staticmethod
    def menu_keys(item, change=10, *, current="theme"):
        index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == item)
        previous = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == current)
        return [ord("m")] + [asmond.curses.KEY_DOWN] * ((index - previous) % len(asmond.MENU_ITEMS)) + [change]

    def test_settings_default_strict_booleans_and_restart_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch.object(asmond, "SETTINGS_PATH", path):
                defaults = asmond.build_parser().parse_args(["--mock"])
                self.assertTrue(defaults.rounded_borders)
                self.assertTrue(defaults.diagnostics_in_cycle)
                for invalid in (None, 0, 1, "false", [], {}):
                    path.write_text(json.dumps({"rounded_borders": invalid, "diagnostics_in_cycle": invalid}))
                    args = asmond.build_parser().parse_args(["--mock"])
                    self.assertTrue(args.rounded_borders)
                    self.assertTrue(args.diagnostics_in_cycle)
                for enabled in (False, True):
                    defaults.rounded_borders = enabled
                    defaults.diagnostics_in_cycle = enabled
                    self.assertIsNone(asmond_settings.save_settings(path, defaults, asmond.settings_schema()))
                    restored = asmond.build_parser().parse_args(["--mock"])
                    self.assertIs(restored.rounded_borders, enabled)
                    self.assertIs(restored.diagnostics_in_cycle, enabled)

    def test_b_cycles_three_or_four_views_and_preserves_the_preference(self):
        runner = ProcessRunLoopSafetyTests()
        for enabled, expected in (
            (True, ["battery", "usb", "cable", "diagnostics", "battery"]),
            (False, ["battery", "usb", "cable", "battery", "usb"]),
        ):
            result = runner.run_process_loop([{"key": ord("b")}] * 4 + [{"key": ord("q")}], initial={"diagnostics_in_cycle": enabled})
            self.assertEqual([s["charge_panel"] for s in result["states"]], expected)
            self.assertIs(result["args"].diagnostics_in_cycle, enabled)
        self.assertEqual(asmond.cycle_charge_panel("battery", -1, False), "cable")
        self.assertEqual(asmond.cycle_charge_panel("diagnostics", -1, False), "cable")
        self.assertEqual(asmond.cycle_charge_panel("diagnostics", 1, False), "battery")

    def test_menu_toggles_save_and_leave_active_diagnostics_without_changing_tailor(self):
        keys = self.menu_keys("rounded_borders") + [27]
        keys += self.menu_keys("diagnostics_in_cycle", asmond.curses.KEY_LEFT, current="rounded_borders") + [27, ord("b"), ord("q")]
        custom = {"cell_1": {"panel": "port_diagnostics", "detail": "normal"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            result = ProcessRunLoopSafetyTests().run_process_loop(
                [{"key": key} for key in keys], persist_path=path,
                initial={"charge_panel": "diagnostics", "custom_layout": custom},
            )
            saved = asmond_settings.load_settings(path, asmond.settings_schema())
        self.assertFalse(saved["rounded_borders"])
        self.assertFalse(saved["diagnostics_in_cycle"])
        self.assertEqual(result["states"][-2]["charge_panel"], "cable")
        self.assertEqual(result["states"][-1]["charge_panel"], "battery")
        self.assertEqual(result["args"].custom_layout, custom)
        result["kill"].assert_not_called()

    def test_menu_charge_selection_skips_diagnostics_but_explicit_access_still_works(self):
        keys = self.menu_keys("charge_panel", asmond.curses.KEY_RIGHT) + [27]
        keys += self.menu_keys("port_diagnostics", current="charge_panel") + [ord("b"), ord("q")]
        result = ProcessRunLoopSafetyTests().run_process_loop(
            [{"key": key} for key in keys], initial={"diagnostics_in_cycle": False, "charge_panel": "cable"},
        )
        self.assertEqual(result["states"][len(self.menu_keys("charge_panel"))]["charge_panel"], "battery")
        self.assertEqual([s["charge_panel"] for s in result["states"][-2:]], ["diagnostics", "battery"])
        self.assertFalse(result["args"].diagnostics_in_cycle)
        restart = ProcessRunLoopSafetyTests().run_process_loop([{"key": ord("q")}], initial={"diagnostics_in_cycle": False, "charge_panel": "diagnostics"})
        self.assertEqual(restart["states"][0]["charge_panel"], "battery")

    def test_all_dashboard_layouts_and_overlays_receive_border_choice_without_theme_mutation(self):
        fixtures = LayoutTests()
        for rounded in (False, True):
            for layout in asmond.LAYOUTS:
                for overlay in ({}, {"help_visible": True}, {"menu_visible": True}, {"tailor_visible": True, "tailor_menu_visible": True} if layout == "custom" else {}):
                    with self.subTest(rounded=rounded, layout=layout, overlay=overlay):
                        args = fixtures.dashboard_args(layout)
                        args.rounded_borders = rounded
                        window = fixtures.RecordingWindow(61, 181)
                        with mock.patch.object(asmond, "draw_box") as box:
                            fixtures.draw_test_dashboard(window, args, **overlay)
                        self.assertTrue(box.called)
                        self.assertTrue(all(call.kwargs["rounded"] is rounded for call in box.call_args_list))
        palette = fixtures.dashboard_colors()
        original = dict(palette)
        data = fixtures.dashboard_data()
        args = fixtures.dashboard_args()
        args.rounded_borders = False
        with mock.patch.object(asmond, "draw_box") as box:
            asmond.draw_dashboard(fixtures.RecordingWindow(61, 181), None, *data[1:], palette, args, "waiting", "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery", 0, None, (), False, True, 0, False, False, 0, "", False)
        self.assertTrue(box.called)
        self.assertTrue(all(call.kwargs["rounded"] is False for call in box.call_args_list))
        self.assertEqual(palette, original)

    def test_inspector_uses_square_choice_and_fixed_diagnostics_and_liquid_warning_remain(self):
        keys = self.menu_keys("sources") + [27, 27, ord("q")]
        result = ProcessRunLoopSafetyTests().run_process_loop([{"key": key} for key in keys], initial={"rounded_borders": False, "diagnostics_in_cycle": False})
        self.assertTrue(result["inspector_palettes"])
        self.assertTrue(all(p["rounded_borders"] is False for p in result["inspector_palettes"]))
        fixtures = LayoutTests()
        data = list(fixtures.dashboard_data())
        data[4].diagnostics[0].liquid_detected = True
        args = fixtures.dashboard_args("custom", custom_layout={"right_lower": {"panel": "port_diagnostics", "detail": "normal"}})
        args.diagnostics_in_cycle = False
        window = fixtures.RecordingWindow(61, 181)
        with mock.patch.object(asmond, "draw_box"):
            asmond.draw_dashboard(window, *data, fixtures.dashboard_colors(), args, "live", "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery", 0, None, (), False, False, 0, False, False, 0, "", False)
        self.assertIn("LIQUID REPORTED", window.rendered())
        # The independent Tailor diagnostics block is not disabled by a b-cycle preference.
        layout = asmond.dashboard_layout(61, 181, "custom", True, "hidden", args.custom_layout)
        self.assertEqual(layout.slot("right_lower").panel_id, "port_diagnostics")

    def test_navigation_hint_help_and_menu_values_follow_cycle_choice(self):
        for enabled, next_title in ((False, "Battery"), (True, "Port diag")):
            palette = dict(LayoutTests.dashboard_colors(), diagnostics_in_cycle=enabled)
            window = LayoutTests.RecordingWindow(61, 181)
            with mock.patch.object(asmond, "draw_box"):
                asmond.draw_charge_box(window, asmond.Rect(0, 0, 8, 90), "CABLE", "cable", palette)
            self.assertIn(f"b › {next_title}", window.rendered_row(0))
            window = LayoutTests.RecordingWindow(61, 181)
            with mock.patch.object(asmond, "draw_box"):
                asmond.draw_help_overlay(window, palette)
            self.assertEqual("cycle Battery/Input/Cable/Port diagnostics" in window.rendered(), enabled)
            self.assertIn("cycle Battery/Input/Cable", window.rendered())
            args = LayoutTests.dashboard_args()
            args.diagnostics_in_cycle = enabled
            args.rounded_borders = enabled
            for name in ("rounded_borders", "diagnostics_in_cycle"):
                value = asmond.menu_value_text(name, args, "soc", "cpu", "disk_read", "net_in", "rows", "hidden", "cpu", "battery")
                self.assertEqual(value, "on" if enabled else "off")


class DirectInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent
        major, minor, patch = asmond.VERSION.split("-", 1)[0].split(".")
        cls.newer_version = f"{major}.{minor}.{int(patch) + 1}"
        script = (cls.root / "install.sh").read_text()
        cls.installer = types.ModuleType("asmond_direct_installer_test")
        code = script.split("<<'ASMOND_INSTALLER_PY'\n", 1)[1].rsplit("\nASMOND_INSTALLER_PY", 1)[0]
        exec(compile(code, str(cls.root / "install.sh"), "exec"), cls.installer.__dict__)
        spec = importlib.util.spec_from_file_location("asmond_archive_builder_test", cls.root / "tools/build-release-archive.py")
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="asmond-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.prefix = self.base / "Asmond's test files"
        self.bin_dir = self.base / "user bin"

    def package(self, version=None):
        version = version or asmond.VERSION
        source = self.base / ("source-" + version)
        source.mkdir()
        for name in self.installer.PACKAGE_FILES:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / name, target)
        program = source / "asmond.py"
        program.write_text(program.read_text().replace(f'VERSION = "{asmond.VERSION}"', f'VERSION = "{version}"'))
        manual = source / "man/asmond.1"
        manual.write_text(manual.read_text().replace(f'"Asmond {asmond.VERSION}"', f'"Asmond {version}"'))
        readme = source / "README.md"
        readme.write_text(readme.read_text().replace(f"Development build: {asmond.VERSION}.", f"Current release: {version}."))
        readme.write_text(readme.read_text().replace(f"Current release: {asmond.VERSION}.", f"Current release: {version}."))
        archive, checksum = self.builder.build_archive(source, self.base / ("assets-" + version), allow_dev=True)
        return archive, checksum.read_text().split()[0], version

    def arguments(self, package=None, **overrides):
        archive, digest, version = package or self.package()
        values = dict(prefix=str(self.prefix), bin_dir=str(self.bin_dir), python=None,
                      version=version, archive=str(archive), sha256=digest, yes=True,
                      add_path=False, allow_shadow=True, update=False, rollback=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def install(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            self.installer.install(args)

    def test_release_builder_rejects_dev_by_default_and_is_reproducible_without_history(self):
        if asmond.VERSION.endswith("-dev"):
            with self.assertRaisesRegex(ValueError, "Development"):
                self.builder.build_archive(self.root, self.base / "refused")
        first, _checksum = self.builder.build_archive(self.root, self.base / "one", allow_dev=True)
        second, _checksum = self.builder.build_archive(self.root, self.base / "two", allow_dev=True)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            members = archive.getmembers()
        self.assertEqual({"/".join(m.name.split("/")[1:]) for m in members}, set(self.installer.PACKAGE_FILES))
        self.assertTrue(all(m.isfile() and m.uid == m.gid == m.mtime == 0 and not m.uname and not m.gname for m in members))
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.builder.build_archive(self.root, self.base / "one", allow_dev=True)

    def test_local_install_launcher_quoting_real_smoke_and_settings_are_untouched(self):
        args = self.arguments()
        self.install(args)
        config, version = self.installer.read_installation(self.prefix)
        self.assertEqual(version, asmond.VERSION)
        self.assertEqual(config["python"], str(Path(sys.executable).absolute()))
        self.assertEqual((self.prefix / ".asmond-install.json").stat().st_mode & 0o777, 0o600)
        result = subprocess.run([str(self.bin_dir / "asmond"), "--version"], cwd="/tmp", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"Asmond {asmond.VERSION}\n")
        self.assertFalse((self.prefix / ".install.lock").exists())
        self.assertFalse(any(path.name.startswith(".candidate-") for path in (self.prefix / "releases").iterdir()))

    def test_update_and_rollback_are_atomic_and_never_downgrade_implicitly(self):
        original = self.package()
        self.install(self.arguments(original))
        newer = self.package(self.newer_version)
        self.install(self.arguments(newer, update=True))
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + self.newer_version)
        self.install(self.arguments(original, update=True))
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + self.newer_version)
        self.install(self.arguments(original, archive=None, sha256=None, version=None, rollback=True))
        self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{asmond.VERSION}")
        self.assertTrue((self.prefix / "releases" / self.newer_version / "asmond.py").is_file())
        self.install(self.arguments(newer, update=True))
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + self.newer_version)
        self.install(self.arguments(original, archive=None, sha256=None, version=None, rollback=True))
        retained = self.prefix / "releases" / self.newer_version / "asmond.py"
        retained.write_text(retained.read_text() + "\n# local modification\n")
        with self.assertRaisesRegex(self.installer.InstallError, "modified"):
            self.install(self.arguments(newer, update=True))
        self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{asmond.VERSION}")

    def test_failed_candidate_checksum_or_smoke_leaves_the_active_version_unchanged(self):
        self.install(self.arguments())
        newer = self.package(self.newer_version)
        before = (self.prefix / ".asmond-install.json").read_bytes()
        for overrides in ({"sha256": "0" * 64}, {}):
            with mock.patch.object(self.installer, "smoke_test", side_effect=self.installer.InstallError("candidate failed")):
                with self.assertRaises(self.installer.InstallError):
                    self.install(self.arguments(newer, update=True, **overrides))
            self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{asmond.VERSION}")
            self.assertEqual((self.prefix / ".asmond-install.json").read_bytes(), before)
            self.assertFalse((self.prefix / "releases" / self.newer_version).exists())

    def test_rollback_checks_retained_bytes_and_links_before_any_candidate_execution(self):
        original, newer = self.package(), self.package(self.newer_version)
        self.install(self.arguments(original))
        self.install(self.arguments(newer, update=True))
        old_module = self.prefix / "releases" / original[2] / "asmond_record.py"
        content = old_module.read_bytes()
        marker = self.base / "unexpected-code-executed"
        old_module.write_bytes(content + f'\nfrom pathlib import Path as _P\n_P({str(marker)!r}).write_text("executed")\n'.encode())
        rollback = self.arguments(original, archive=None, sha256=None, version=None, rollback=True)
        with mock.patch.object(self.installer, "smoke_test", wraps=self.installer.smoke_test) as smoke:
            with self.assertRaisesRegex(self.installer.InstallError, "modified"):
                self.install(rollback)
            smoke.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + self.newer_version)
        outside = self.base / "outside.py"
        outside.write_bytes(content)
        old_module.unlink()
        old_module.symlink_to(outside)
        with mock.patch.object(self.installer, "smoke_test") as smoke:
            with self.assertRaises(self.installer.InstallError):
                self.install(rollback)
            smoke.assert_not_called()
        self.assertEqual(outside.read_bytes(), content)
        old_module.unlink()
        old_module.write_bytes(content)
        self.install(rollback)
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + original[2])

    def test_archive_extraction_uses_the_exact_bounded_bytes_that_were_hashed(self):
        archive, digest, version = self.package()
        changed = self.base / "changed.tar.gz"
        marker = b"\n# archive changed after verification\n"
        with tarfile.open(archive, "r:gz") as source, tarfile.open(changed, "w:gz") as output:
            for member in source:
                data = source.extractfile(member).read()
                if member.name.endswith("/asmond_record.py"):
                    data += marker
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        real_gzip = self.installer.gzip.GzipFile
        def replace_path(**kwargs):
            shutil.copyfile(changed, archive)
            return real_gzip(**kwargs)
        destination = self.base / "extracted"
        destination.mkdir()
        with mock.patch.object(self.installer.gzip, "GzipFile", side_effect=replace_path):
            self.installer.extract_package(archive, digest, version, destination)
        self.assertFalse((destination / "asmond_record.py").read_bytes().endswith(marker))
        self.assertNotEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), digest)
        with mock.patch.object(self.installer, "MAX_ARCHIVE_BYTES", 10):
            with self.assertRaisesRegex(self.installer.InstallError, "Archive exceeds"):
                self.installer.extract_package(archive, digest, version, destination)

    def test_builder_rejects_extra_runtime_and_missing_required_module_before_creating_assets(self):
        package = self.package()
        source = self.base / ("source-" + package[2])
        extra = source / "asmond_local_scratch.py"
        extra.write_text("SYNTHETIC_PRIVATE_NOTE = True\n")
        with self.assertRaisesRegex(ValueError, "Unexpected runtime"):
            self.builder.build_archive(source, self.base / "bad-extra", allow_dev=True)
        self.assertFalse((self.base / "bad-extra").exists())
        extra.unlink()
        (source / "asmond_record.py").unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            self.builder.build_archive(source, self.base / "bad-missing", allow_dev=True)
        self.assertFalse((self.base / "bad-missing").exists())

    def test_real_smoke_rejects_wrong_version_and_bad_manual(self):
        archive, digest, version = self.package()
        extracted = self.base / "extracted"
        extracted.mkdir()
        self.installer.extract_package(archive, digest, version, extracted)
        with self.assertRaisesRegex(self.installer.InstallError, "versions do not match"):
            self.installer.smoke_test(sys.executable, extracted, "0.8.99")
        for manual in ("bad manual\n", ""):
            (extracted / "man/asmond.1").write_text(manual)
            with self.assertRaisesRegex(self.installer.InstallError, "Manual"):
                self.installer.smoke_test(sys.executable, extracted, version)
        outputs = [subprocess.CompletedProcess([], 0, f"Asmond {version}\n", ""), subprocess.CompletedProcess([], 0, "[]", "")]
        with mock.patch.object(self.installer.subprocess, "run", side_effect=outputs):
            with self.assertRaisesRegex(self.installer.InstallError, "report version"):
                self.installer.smoke_test(sys.executable, extracted, version)

    def test_foreign_directories_commands_and_symlinks_are_never_overwritten(self):
        args = self.arguments()
        with self.assertRaisesRegex(self.installer.InstallError, "Cellar"):
            self.install(self.arguments((Path(args.archive), args.sha256, args.version), bin_dir=str(self.base / "Cellar/package/bin")))
        self.assertFalse(self.prefix.exists())
        self.prefix.mkdir()
        foreign = self.prefix / "keep.txt"
        foreign.write_text("keep")
        with self.assertRaisesRegex(self.installer.InstallError, "unmanaged"):
            self.install(args)
        self.assertEqual(foreign.read_text(), "keep")
        self.prefix = self.base / "other"
        self.bin_dir.mkdir()
        command = self.bin_dir / "asmond"
        command.symlink_to(foreign)
        with self.assertRaises(self.installer.InstallError):
            self.install(self.arguments((Path(args.archive), args.sha256, args.version)))
        self.assertTrue(command.is_symlink())
        self.assertEqual(foreign.read_text(), "keep")

    def test_unsupported_os_and_root_are_rejected_before_downloads_or_install_writes(self):
        args = self.arguments(archive=None, sha256=None, version=None)
        for system in ("Linux", "Windows"):
            with mock.patch.object(self.installer.platform, "system", return_value=system), mock.patch.object(self.installer, "curl_download") as download:
                with self.assertRaisesRegex(self.installer.InstallError, "not supported"):
                    self.install(args)
                download.assert_not_called()
        with mock.patch.object(self.installer.os, "geteuid", return_value=0), mock.patch.object(self.installer, "curl_download") as download:
            with self.assertRaisesRegex(self.installer.InstallError, "normal user"):
                self.install(args)
            download.assert_not_called()
        self.assertFalse(self.prefix.exists())

    def test_archive_traversal_links_duplicates_missing_members_and_bombs_are_rejected(self):
        for member_name, kind, count in (("asmond-0.8.1/../../escape", "file", 1),
                                         ("asmond-0.8.1/asmond.py", "link", 1),
                                         ("asmond-0.8.1/asmond.py", "file", 2),
                                         ("asmond-0.8.1/asmond.py", "file", 1)):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                for _ in range(count):
                    info = tarfile.TarInfo(member_name)
                    if kind == "link":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "../../outside"
                        archive.addfile(info)
                    else:
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b"x"))
            archive_file = self.base / "bad.tgz"
            archive_file.write_bytes(data.getvalue())
            destination = self.base / "candidate"
            destination.mkdir(exist_ok=True)
            with self.assertRaises(self.installer.InstallError):
                self.installer.extract_package(archive_file, hashlib.sha256(data.getvalue()).hexdigest(), "0.8.1", destination)
            self.assertEqual(list(destination.iterdir()), [])
        payload = gzip.compress(b"x" * 4096)
        archive_file.write_bytes(payload)
        with mock.patch.object(self.installer, "MAX_ARCHIVE_BYTES", 1024):
            with self.assertRaisesRegex(self.installer.InstallError, "Unpacked"):
                self.installer.extract_package(archive_file, hashlib.sha256(payload).hexdigest(), "0.8.1", destination)
        self.assertFalse((self.base / "escape").exists())

    def test_release_selection_requires_stable_matching_assets_and_strict_checksum(self):
        name = "asmond-0.8.1.tar.gz"
        base = "https://github.com/Fxxrz/asmond/releases/download/v0.8.1/"
        good = {"tag_name": "v0.8.1", "draft": False, "prerelease": False,
                "assets": [{"name": item, "browser_download_url": base + item} for item in (name, name + ".sha256")]}
        def downloader(url, destination, _limit):
            if "api.github.com" in url:
                destination.write_text(json.dumps(metadata))
            elif url.endswith(".sha256"):
                destination.write_text("a" * 64 + "  " + name + "\n")
            else:
                destination.write_bytes(b"archive")
        for metadata in (good, {**good, "draft": True}, {**good, "prerelease": True},
                         {**good, "tag_name": "v0.8.1-dev"}, {**good, "assets": []},
                         {**good, "assets": [{"name": name, "browser_download_url": "https://example.com/wrong"}]}):
            with mock.patch.object(self.installer, "curl_download", side_effect=downloader):
                if metadata is good:
                    self.assertEqual(self.installer.release_download(None, self.base)[0], "0.8.1")
                else:
                    with self.assertRaises(self.installer.InstallError):
                        self.installer.release_download(None, self.base)

    def test_unmanaged_update_shadowed_command_and_active_lock_are_rejected(self):
        args = self.arguments()
        with self.assertRaisesRegex(self.installer.InstallError, "not a direct"):
            self.install(self.arguments((Path(args.archive), args.sha256, args.version), update=True))
        with mock.patch.object(self.installer.shutil, "which", return_value="/usr/local/bin/asmond"):
            with self.assertRaisesRegex(self.installer.InstallError, "already on PATH"):
                self.install(self.arguments((Path(args.archive), args.sha256, args.version), allow_shadow=False))
        self.install(args)
        lock = self.prefix / ".install.lock"
        lock.mkdir()
        newer = self.package(self.newer_version)
        with self.assertRaisesRegex(self.installer.InstallError, "Another installer"):
            self.install(self.arguments(newer, update=True))
        self.assertTrue(lock.is_dir())
        self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{asmond.VERSION}")

    def test_path_setup_is_opt_in_idempotent_and_does_not_follow_profile_symlinks(self):
        home = self.base / "fake home"
        home.mkdir()
        with mock.patch.object(self.installer.Path, "home", return_value=home), mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}), contextlib.redirect_stdout(io.StringIO()):
            self.installer.add_path(self.bin_dir)
            first = (home / ".zprofile").read_bytes()
            self.installer.add_path(self.bin_dir)
            self.assertEqual((home / ".zprofile").read_bytes(), first)
            target = home / "untouched"
            target.write_text("keep")
            (home / ".bash_profile").symlink_to(target)
            with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
                with self.assertRaises(self.installer.InstallError):
                    self.installer.add_path(self.bin_dir)
            self.assertEqual(target.read_text(), "keep")

    def test_shell_platform_guard_precedes_bootstrap_and_help_does_not_download(self):
        script = (self.root / "install.sh").read_text()
        head = script.split("requested_python=", 1)[0]
        for name in ("Linux", "MINGW64_NT"):
            unsupported = head.replace('$(/usr/bin/uname -s)', name)
            result = subprocess.run(["/bin/bash", "-c", unsupported], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not supported", result.stderr)
        result = subprocess.run(["/bin/bash", str(self.root / "install.sh"), "--help"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--update", result.stdout)
        self.assertTrue((self.root / "install.sh").is_file())

    def test_update_cli_dispatches_only_managed_installs_before_any_telemetry(self):
        release = self.base / "direct/releases/0.8.1"
        release.mkdir(parents=True)
        (release / "install.sh").write_text("installer")
        (self.base / "direct/.asmond-install.json").write_text("{}")
        with mock.patch.object(asmond, "__file__", str(release / "asmond.py")), mock.patch.object(asmond.subprocess, "call", return_value=0) as call, mock.patch.object(asmond, "ensure_powermetrics_access") as access:
            self.assertEqual(asmond.main(["--update"]), 0)
            self.assertEqual(call.call_args.args[0], ["/bin/bash", str(release / "install.sh"), "--update", "--prefix", str(self.base / "direct"), "--python", sys.executable])
            access.assert_not_called()
        with mock.patch.object(asmond.subprocess, "call") as call, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(asmond.main(["--update"]), 1)
            call.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            asmond.main(["--update", "--remove-settings"])

    def test_real_shell_bootstrap_installs_local_archive_without_network_or_homebrew_writes(self):
        archive, digest, version = self.package()
        result = subprocess.run(["/bin/bash", str(self.root / "install.sh"), "--yes", "--allow-shadow",
                                 "--prefix", str(self.prefix), "--bin-dir", str(self.bin_dir),
                                 "--python", sys.executable, "--archive", str(archive), "--sha256", digest, "--version", version],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installed Asmond " + version, result.stdout)
        self.assertTrue((self.bin_dir / "asmond").is_file())
        self.assertTrue((self.root / "install.sh").is_file())

    @unittest.skipUnless(hasattr(os, "forkpty"), "requires a real controlling terminal")
    def test_confirmation_reads_and_writes_a_real_nonseekable_terminal(self):
        script = (self.root / "install.sh").read_text()
        engine = script.split("<<'ASMOND_INSTALLER_PY'\n", 1)[1].rsplit("\nASMOND_INSTALLER_PY", 1)[0]
        child = self.base / "terminal-check.py"
        child.write_text(
            "namespace = {'__name__': 'installer_terminal_test'}\n"
            f"exec(compile({engine!r}, 'installer', 'exec'), namespace)\n"
            "assert namespace['confirm']('Approve?') is True\n"
            "assert namespace['ask']('Default?', 'kept') == 'kept'\n"
            "assert namespace['confirm']('Decline?') is False\n"
            "print('TERMINAL_CHECK_OK', flush=True)\n"
        )
        # Fork only in this fresh helper process, before any threads exist. The
        # child owns a real /dev/tty even when the test runner has piped stdin.
        harness = r'''
import os, pty, select, signal, sys, time
pid, terminal = pty.fork()
if pid == 0:
    os.execv(sys.executable, [sys.executable, '-I', sys.argv[1]])
output = bytearray()
reaped = False
try:
    for prompt, answer in ((b'Approve? [y/N]', b'yes\n'), (b'Default?', b'\n'), (b'Decline? [y/N]', b'no\n')):
        deadline = time.monotonic() + 5
        while prompt not in output:
            assert time.monotonic() < deadline, bytes(output)
            if select.select([terminal], [], [], .05)[0]:
                data = os.read(terminal, 65536)
                assert data, bytes(output)
                output.extend(data)
        os.write(terminal, answer)
    deadline = time.monotonic() + 5
    while b'TERMINAL_CHECK_OK' not in output:
        assert time.monotonic() < deadline, bytes(output)
        if select.select([terminal], [], [], .05)[0]:
            data = os.read(terminal, 65536)
            assert data, bytes(output)
            output.extend(data)
    _, status = os.waitpid(pid, 0)
    reaped = True
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    print('TERMINAL_CHECK_OK')
finally:
    if not reaped:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    os.close(terminal)
'''
        result = subprocess.run([sys.executable, "-I", "-c", harness, str(child)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TERMINAL_CHECK_OK", result.stdout)

    def test_failed_activation_keeps_the_previous_command_usable(self):
        self.install(self.arguments())
        newer = self.package(self.newer_version)
        with mock.patch.object(self.installer, "activate", side_effect=OSError("simulated activation failure")):
            with self.assertRaises(OSError):
                self.install(self.arguments(newer, update=True))
        self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{asmond.VERSION}")
        result = subprocess.run([str(self.bin_dir / "asmond"), "--version"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout, f"Asmond {asmond.VERSION}\n")
        self.assertFalse((self.prefix / ".install.lock").exists())

    def test_curl_enforces_https_and_bounded_failed_downloads_remain_unusable(self):
        target = self.base / "download"
        with mock.patch.object(self.installer.subprocess, "run") as run:
            with self.assertRaisesRegex(self.installer.InstallError, "HTTPS"):
                self.installer.curl_download("http://example.com/file", target, 100)
            run.assert_not_called()
        target.write_bytes(b"too large")
        with mock.patch.object(self.installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaisesRegex(self.installer.InstallError, "size limit"):
                self.installer.curl_download("https://github.com/Fxxrz/asmond/test", target, 2)
            command = run.call_args.args[0]
            self.assertIn("--fail", command)
            self.assertIn("--proto-redir", command)
            self.assertEqual(command[command.index("--proto-redir") + 1], "=https")

    def test_same_version_rerun_can_configure_path_and_profile_failures_do_not_break_install(self):
        args = self.arguments()
        self.install(args)
        with mock.patch.object(self.installer, "add_path") as add_path:
            self.install(self.arguments((Path(args.archive), args.sha256, args.version), add_path=True))
            add_path.assert_called_once_with(self.bin_dir)
        newer = self.package(self.newer_version)
        with mock.patch.object(self.installer, "add_path", side_effect=self.installer.InstallError("symlinked profile")):
            self.install(self.arguments(newer, update=True, add_path=True))
        self.assertEqual(os.readlink(self.prefix / "current"), "releases/" + self.newer_version)


class ReviewTelemetryRegressions(unittest.TestCase):
    def test_legacy_health_and_capacity_priorities_do_not_change_at_low_charge(self):
        battery = plistlib.loads((Path(__file__).parent / "tests/fixtures/intel_macbookair_battery_sample.plist").read_bytes())[0]
        battery["BatteryData"].update(NominalChargeCapacity=5500, StateOfCharge=1)
        expected_full = battery["AppleRawMaxCapacity"]
        for current in (101, 100, 80, 0):
            battery["CurrentCapacity"] = battery["AppleRawCurrentCapacity"] = current
            result = asmond.battery_stats_from_item(battery)
            self.assertEqual(result.charge_pct, 1)
            self.assertEqual(result.max_capacity, expected_full)
            self.assertAlmostEqual(result.health_pct, expected_full / battery["DesignCapacity"] * 100)
        modern = {"MaxCapacity": 100, "AppleRawCurrentCapacity": 3000, "AppleRawMaxCapacity": 4800, "DesignCapacity": 5000, "BatteryData": {"NominalChargeCapacity": 4500}}
        for current in (71, None):
            modern["CurrentCapacity"] = current
            self.assertEqual(asmond.battery_stats_from_item(modern).max_capacity, 4500)
            self.assertEqual(asmond.battery_stats_from_item(modern).health_pct, 90)

    def test_auxiliary_cache_uses_consumer_interval_and_expires_blocked_refreshes(self):
        class DeferredThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                pass
        for interval in (1, 5, 10, 30):
            with self.subTest(interval=interval):
                cache = asmond.PowerMetricsStream(interval * 1000).temperature_cache
                cache.refreshed_at = 100
                cache.smc = asmond_smc.SMCTemperatureSample(cpu_avg_c=42)
                clock, values = [100.0], []
                with mock.patch.object(asmond.threading, "Thread", DeferredThread), mock.patch.object(asmond.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(asmond.SMC_TEMPS, "read", side_effect=[asmond_smc.SMCTemperatureSample(cpu_avg_c=v) for v in (43, 44, 45)]), mock.patch.object(asmond.HID_TEMPS, "read_metrics", return_value=asmond.HIDTemperatureSample()):
                    for _ in range(3):
                        clock[0] += interval + 0.01
                        values.append(cache.snapshot()[0].cpu_avg_c)
                        self.assertTrue(cache.refreshing)
                        cache.thread.target()
                    self.assertEqual(values, [42, 43, 44])
                    clock[0] += interval + 0.01
                    self.assertEqual(cache.snapshot()[0].cpu_avg_c, 45)
                    self.assertTrue(cache.refreshing)
                    clock[0] = cache.refreshed_at + max(5, 3 * interval) + 0.01
                    self.assertIsNone(cache.snapshot()[0].cpu_avg_c)

    def test_low_legacy_capacity_never_turns_milliamphours_into_percent(self):
        battery = plistlib.loads((Path(__file__).parent / "tests/fixtures/intel_macbookair_battery_sample.plist").read_bytes())[0]
        for charge in (101, 100, 80, 0):
            battery["CurrentCapacity"] = battery["AppleRawCurrentCapacity"] = charge
            battery["BatteryData"]["StateOfCharge"] = 1
            self.assertEqual(asmond.battery_charge_percentage(battery), 1)
            del battery["BatteryData"]["StateOfCharge"]
            self.assertAlmostEqual(asmond.battery_charge_percentage(battery), charge / battery["AppleRawMaxCapacity"] * 100)
        self.assertEqual(asmond.battery_charge_percentage({"CurrentCapacity": 71, "MaxCapacity": 100}), 71)
        self.assertAlmostEqual(asmond.battery_charge_percentage({"CurrentCapacity": 80, "AppleRawCurrentCapacity": 80, "AppleRawMaxCapacity": 6000}), 80 / 60)

    def test_charge_contract_mapping_is_invariant_under_independent_array_permutations(self):
        import itertools
        controllers = [{"PortType": 2, "PortNumber": 2, "PortControllerPortPDO": [(400 << 10) | 300], "PortControllerActiveContractRdo": (1 << 28) | (150 << 10)}, {"PortType": 2, "PortNumber": 1}]
        hpm = [{"PortType": 2, "PortNumber": number, "ConnectionActive": True} for number in (1, 2)]
        feeds = [{"PortType": 2, "PortNumber": number, "FedExternalConnected": number == 2} for number in (1, 2)]
        for c, h, f in itertools.product(itertools.permutations(controllers), itertools.permutations(hpm), itertools.permutations(feeds)):
            stats = asmond.usb_c_stats_from_item({"ExternalConnected": True, "BestAdapterIndex": 0, "PortControllerInfo": list(c), "FedDetails": list(f), "PowerTelemetryData": {"SystemPowerIn": 30000}}, list(h))
            self.assertEqual(stats.active_port.label, "USB-C 2")
            self.assertEqual(stats.active_port.voltage_v, 20)
            self.assertEqual(stats.active_port.current_a, 1.5)
            self.assertIsNone(stats.ports[0].voltage_v)
            self.assertEqual(stats.system_power_w, 30)
        for entries in ([{}, controllers[0]], [controllers[0], controllers[0]]):
            stats = asmond.usb_c_stats_from_item({"ExternalConnected": True, "PortControllerInfo": entries}, hpm)
            if entries[0] == entries[1]:
                self.assertIsNone(stats.active_port)
                self.assertTrue(all(port.voltage_v is None for port in stats.ports))
            else:
                self.assertEqual(stats.active_port.label, "USB-C 2")

    def test_unidentified_controller_and_feed_never_claim_a_physical_connector(self):
        contract = {"PortControllerPortPDO": [(400 << 10) | 300], "PortControllerActiveContractRdo": (1 << 28) | (150 << 10)}
        stats = asmond.usb_c_stats_from_item({"ExternalConnected": True, "PortControllerInfo": [{}, {}, contract]})
        self.assertEqual(stats.active_port.label, "Controller 3 (port unknown)")
        self.assertEqual(stats.input_kind, "ac")
        self.assertEqual(stats.active_port.power_w, 30)
        self.assertFalse(any(port.label == "MagSafe" for port in stats.ports))
        hpm = [{"PortType": 2, "PortNumber": 1, "ConnectionActive": True}]
        stats = asmond.usb_c_stats_from_item({"ExternalConnected": True, "PortControllerInfo": [contract], "FedDetails": [{"FedExternalConnected": True}], "PowerTelemetryData": {"SystemPowerIn": 30000}}, hpm)
        self.assertIsNone(stats.active_port)
        self.assertIsNone(stats.ports[0].voltage_v)
        self.assertEqual(stats.system_power_w, 30)
        self.assertEqual(asmond.usb_to_report_dict(stats)["power"], "30.0 W")
        self.assertEqual(asmond.usb_to_report_dict(stats)["active_port"], "AC Input")

    def test_blocked_native_temperature_refresh_expires_old_values_and_recovers(self):
        started, release = threading.Event(), threading.Event()
        cache = asmond.AuxiliaryTemperatureCache()
        cache.smc = asmond_smc.SMCTemperatureSample(cpu_avg_c=42)
        cache.refreshed_at = 100
        def blocked():
            started.set()
            release.wait(timeout=2)
            return asmond_smc.SMCTemperatureSample(cpu_avg_c=43)
        with mock.patch.object(asmond.SMC_TEMPS, "read", side_effect=blocked), mock.patch.object(asmond.HID_TEMPS, "read_metrics", return_value=asmond.HIDTemperatureSample()), mock.patch.object(asmond.time, "monotonic", return_value=110):
            try:
                self.assertEqual(cache.snapshot(now=100)[0].cpu_avg_c, 42)
                cache.refresh_async(now=103)
                self.assertTrue(started.wait(timeout=1))
                self.assertIsNone(cache.snapshot(now=110)[0].cpu_avg_c)
                self.assertTrue(cache.refreshing)
            finally:
                release.set()
                cache.thread.join(timeout=1)
            self.assertEqual(cache.snapshot(now=110)[0].cpu_avg_c, 43)

    def test_tui_expires_live_values_across_restarts_and_accepts_new_measurement(self):
        now, streams, frames = [100.0], [], []
        args = asmond.build_parser().parse_args(["--mock"])
        class Stream:
            def __init__(self, interval):
                streams.append(self)
                self.number = len(streams)
            def samples(self):
                if self.number in (1, 3):
                    yield asmond.MetricSample(cpu_power_mw=10000 if self.number == 1 else 20000, soc_power_mw=22000, temp_max_c=95 if self.number == 1 else 45, throttled=self.number == 1, telemetry_source="apple")
            def stop(self):
                pass
        class Thread:
            def __init__(self, target=None, args=(), **kwargs):
                self.target, self.args = target, args
            def start(self):
                if self.target.__name__ == "pump":
                    self.target(*self.args)
            def is_alive(self):
                return False
            def join(self, **kwargs):
                pass
        class Window(LayoutTests.RecordingWindow):
            frame = 0
            def nodelay(self, *args):
                pass
            def timeout(self, *args):
                pass
            def getch(self):
                self.frame += 1
                now[0] += 10
                return ord("q") if self.frame == 5 else -1
        original = asmond.draw_dashboard
        def draw(*values):
            original(*values)
            frames.append((values[1].cpu_power_mw, asmond.build_alerts(values[1], values[3], values[4], args), list(values[2].cpu_power)))
        with contextlib.ExitStack() as stack:
            for target, name, value in ((asmond, "MockStream", Stream), (asmond.threading, "Thread", Thread), (asmond.time, "monotonic", lambda: now[0]), (asmond, "init_colors", lambda _: LayoutTests.dashboard_colors()), (asmond, "draw_dashboard", draw), (asmond, "save_settings", lambda _: None), (asmond.curses, "curs_set", lambda _: None), (asmond.curses, "set_escdelay", lambda _: None)):
                stack.enter_context(mock.patch.object(target, name, value))
            asmond.run_curses(Window(61, 180), args)
        self.assertEqual([frame[0] for frame in frames], [10000, None, None, None, 20000])
        self.assertIn("THROTTLE", frames[0][1])
        self.assertTrue(all("THROTTLE" not in frame[1] and not any(alert.startswith("TEMP") for alert in frame[1]) for frame in frames[1:]))
        self.assertIn(10000, frames[-1][2])


if __name__ == "__main__":
    unittest.main()
