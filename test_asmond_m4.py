"""Verified M4 native schemas, sensor identity and independent failure paths."""
import json
import csv
import contextlib
import io
import tempfile
import threading
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest import mock

import test_asmond as fixtures
from test_asmond import IsolatedTestCase, asmond, asmond_powermetrics, asmond_smc
import asmond_record as recording

FIXTURE = json.loads((Path(__file__).parent / "tests/fixtures/apple_m4_air_verified_telemetry.json").read_text())


class M4VerifiedTelemetryTests(IsolatedTestCase):
    fixture_machine = "arm64"

    def reader(self, values=None):
        reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
        reader.available = True
        reader._lock = threading.Lock()
        reader.groups = asmond_smc.temperature_key_groups("Apple M4", 4, 6, model="Mac16,12")
        reader._read_value = (values if values is not None else FIXTURE["sensors"][1]["smc"]).get
        return reader

    def test_new_groups_require_verified_chassis_brand_and_topology(self):
        groups = self.reader().groups
        self.assertEqual(groups.gpu, ("Tg0H", "Tg0L", "Tg0Y", "Tg0e", "Tg0k"))
        self.assertEqual(groups.gpu_base, ("Tg0G", "Tg0K", "Tg0X", "Tg0d", "Tg0j"))
        self.assertEqual(groups.pmu, tuple(f"TPD{i}" for i in range(8)))
        self.assertEqual(dict(groups.auxiliary)["power_supply_proximity_c"], "TPMP")
        for brand, p, e, model in (("Apple M4", 4, 6, ""), ("Apple M4", 4, 6, "Mac16,13"), ("Apple M4 Pro", 4, 6, "Mac16,12"), ("Apple M4", 6, 4, "Mac16,12")):
            with self.subTest(brand=brand, model=model):
                other = asmond_smc.temperature_key_groups(brand, p, e, model=model)
                self.assertFalse(other.gpu_base)
                self.assertFalse(other.pmu)
                self.assertNotEqual(dict(other.auxiliary).get("power_supply_proximity_c"), "TPMP")

    def test_native_model_detection_selects_the_narrow_map_without_opening_sensors(self):
        with (mock.patch.object(asmond_smc.platform, "system", return_value="Darwin"),
              mock.patch.object(asmond_smc, "_sysctl_text", return_value="Mac16,12") as sysctl,
              mock.patch.object(asmond_smc.ctypes, "CDLL", side_effect=OSError("fixture"))):
            reader = asmond_smc.AppleSMCTemperatureReader("Apple M4", 4, 6)
        sysctl.assert_called_once_with("hw.model")
        self.assertEqual(reader.groups, self.reader().groups)
        self.assertFalse(reader.available)
        self.assertEqual(reader.read().pmu_keys, reader.groups.pmu)

    def test_real_sensor_frames_match_the_independent_named_groups(self):
        for frame in FIXTURE["sensors"]:
            with self.subTest(phase=frame["phase"]):
                sample = self.reader(frame["smc"]).read()
                reference = frame["reference"]
                self.assertAlmostEqual(sample.cpu_avg_c, reference["cpu_avg_c"], delta=1.25)
                self.assertAlmostEqual(sample.gpu_avg_c, reference["gpu_avg_c"], delta=.5)
                self.assertAlmostEqual(sample.gpu_max_c, max(reference["gpu_clusters_c"]), delta=.6)
                self.assertAlmostEqual(sample.pmu_avg_c, reference["pmu_avg_c"], delta=1.2)
                self.assertAlmostEqual(sample.power_supply_proximity_c, reference["power_supply_c"], delta=.5)
                self.assertAlmostEqual(sample.trackpad_c, reference["trackpad_c"], delta=.2)
                self.assertIn(sample.cpu_max_label, ("E0", "E1", "E2", "E3", "E4", "E5", "P6", "P7", "P8", "P9"))

    def test_bad_gpu_base_or_adjusted_member_never_creates_a_partial_average(self):
        values = dict(FIXTURE["sensors"][1]["smc"])
        reader = self.reader(values)
        expected = reader.read()
        for key in reader.groups.gpu + reader.groups.gpu_base:
            original = values[key]
            for bad in (None, 0, -1, float("nan"), float("inf"), 150):
                with self.subTest(key=key, bad=bad):
                    values[key] = bad
                    result = reader.read()
                    self.assertIsNone(result.gpu_avg_c)
                    self.assertIsNone(result.gpu_max_c)
                    self.assertEqual(result.cpu_avg_c, expected.cpu_avg_c)
                    self.assertEqual(result.pmu_avg_c, expected.pmu_avg_c)
            values[key] = original
            self.assertEqual(reader.read().gpu_avg_c, expected.gpu_avg_c)

    def test_gpu_guard_loss_after_the_group_and_native_exceptions_preserve_other_groups(self):
        values = FIXTURE["sensors"][1]["smc"]
        for failure in (None, 0, OSError("native read failed")):
            reader = self.reader(values)
            guard = reader.groups.gpu_base[-1]
            calls = 0
            def read(key):
                nonlocal calls
                if key == guard:
                    calls += 1
                    if calls == 2:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                return values.get(key)
            reader._read_value = read
            sample = reader.read()
            self.assertEqual(calls, 2)
            self.assertIsNone(sample.gpu_avg_c)
            self.assertIsNotNone(sample.cpu_avg_c)
            self.assertIsNotNone(sample.pmu_avg_c)
            self.assertEqual(sample.power_supply_proximity_c, values["TPMP"])
        reader = self.reader(values)
        reader.groups = replace(reader.groups, gpu_base=reader.groups.gpu_base[:-1])
        self.assertIsNone(reader.read().gpu_avg_c)

    def test_pmu_member_failure_stays_unknown_and_never_uses_the_broader_hid_group(self):
        values = dict(FIXTURE["sensors"][1]["smc"])
        reader = self.reader(values)
        expected = reader.read()
        for key in reader.groups.pmu:
            original = values[key]
            for bad in (None, 0, -1, float("nan"), float("inf"), 150):
                with self.subTest(key=key, bad=bad):
                    values[key] = bad
                    received = reader.read()
                    current = asmond.MetricSample()
                    with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(received, asmond.HIDTemperatureSample(pmu_average_c=25, pmu_maximum_c=29))):
                        asmond.apply_hid_temperatures(current)
                    self.assertIsNone(current.pmu_temp_avg_c)
                    self.assertIsNone(current.pmu_temp_max_c)
                    self.assertIsNone(current.pmu_temp_source)
                    self.assertEqual(current.gpu_temp_avg_c, expected.gpu_avg_c)
                    self.assertEqual(current.temperature_source, "AppleSMC")
            values[key] = original
        self.assertEqual(reader.read().pmu_avg_c, expected.pmu_avg_c)

    def test_missing_smc_connection_cannot_change_the_pmu_definition(self):
        reader = self.reader()
        reader.available = False
        current = asmond.MetricSample()
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(reader.read(), asmond.HIDTemperatureSample(pmu_average_c=25, pmu_maximum_c=29))):
            asmond.apply_hid_temperatures(current)
        self.assertIsNone(current.pmu_temp_avg_c)
        self.assertIsNone(current.pmu_temp_source)

    def test_smc_pmu_source_reaches_report_inspector_and_thermals(self):
        received = self.reader().read()
        current = asmond.MetricSample()
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(received, asmond.HIDTemperatureSample(pmu_average_c=25, pmu_maximum_c=29))):
            asmond.apply_hid_temperatures(current)
        self.assertEqual(current.pmu_temp_avg_c, received.pmu_avg_c)
        self.assertEqual(current.pmu_temp_max_c, received.pmu_max_c)
        self.assertEqual(current.temperature_source, "AppleSMC")
        self.assertIn("TPD7", current.pmu_temp_source)
        self.assertEqual(asmond.sample_to_report_dict(current)["pmu_temp_source"], current.pmu_temp_source)
        rows = asmond.data_source_rows(Namespace(mock=False), current, asmond.MemoryStats(), asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [])
        self.assertIn(("PMU sensors", current.pmu_temp_source), [(label, value) for label, value, _ in rows])
        window = fixtures.LayoutTests.RecordingWindow(9, 100)
        placement = asmond.PanelPlacement("thermals", asmond.Rect(0, 0, 9, 100), "normal")
        with mock.patch.object(asmond, "draw_box"):
            asmond.draw_thermals_panel(window, placement, current, asmond.BatteryStats(), fixtures.LayoutTests.dashboard_colors())
        text = "\n".join(window.rendered_row(i) for i in range(9))
        self.assertIn(asmond.fmt_temp(received.pmu_avg_c), text)
        self.assertNotIn("25.0°C", text)

    def test_existing_unmapped_hid_group_remains_available_and_named(self):
        current = asmond.MetricSample()
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(asmond_smc.SMCTemperatureSample(), asmond.HIDTemperatureSample(pmu_average_c=25, pmu_maximum_c=29))):
            asmond.apply_hid_temperatures(current)
        self.assertEqual((current.pmu_temp_avg_c, current.pmu_temp_max_c), (25, 29))
        self.assertEqual(current.pmu_temp_source, "IOHID PMU/PMU2 tdie")

    def test_cache_failure_and_expiry_preserve_group_identity_and_clear_values(self):
        reader = self.reader()
        with (mock.patch.object(asmond, "SMC_TEMPS", reader),
              mock.patch.object(asmond.HID_TEMPS, "read_metrics", return_value=asmond.HIDTemperatureSample(pmu_average_c=25, pmu_maximum_c=29))):
            cache = asmond.AuxiliaryTemperatureCache()
            cache.native_gate = threading.BoundedSemaphore(1)
            cache.refresh_async()
            cache.thread.join(timeout=1)
            self.assertFalse(cache.thread.is_alive())
            current = asmond.MetricSample()
            with mock.patch.object(cache, "refresh_async"):
                asmond.apply_hid_temperatures(current, cache, now=cache.refreshed_at)
            self.assertIsNotNone(current.pmu_temp_source)
            with mock.patch.object(reader, "read", side_effect=OSError("native reader failed")):
                cache.refresh_async(now=cache.refreshed_at + 1)
                cache.thread.join(timeout=1)
                self.assertFalse(cache.thread.is_alive())
            self.assertEqual(cache.smc.pmu_keys, reader.groups.pmu)
            with mock.patch.object(cache, "refresh_async"):
                asmond.apply_hid_temperatures(current, cache, now=cache.refreshed_at)
                self.assertIsNone(current.pmu_temp_avg_c)
                self.assertIsNone(current.pmu_temp_source)
                expired, hid = cache.snapshot(now=cache.refreshed_at + cache.max_age_s + 1)
            self.assertEqual(expired.pmu_keys, reader.groups.pmu)
            self.assertIsNone(hid.pmu_average_c)
            self.assertIsNone(expired.pmu_avg_c)
            battery_only = asmond.AuxiliaryTemperatureCache(battery_only=True)
            self.assertFalse(battery_only.unavailable_smc.pmu_keys)

    def test_recording_and_csv_preserve_pmu_source_without_making_absence_available(self):
        current = asmond.MetricSample()
        with mock.patch.object(asmond, "auxiliary_temperature_samples", return_value=(self.reader().read(), asmond.HIDTemperatureSample())):
            asmond.apply_hid_temperatures(current)
        args = Namespace(interval=5, mock=False, groups=["temperatures"])
        feed = mock.Mock()
        feed.snapshot.return_value = (current, {"status": "ok"})
        row = recording.Capture(asmond, args, feed).read()
        self.assertEqual(row["groups"]["temperatures"]["pmu_source"], current.pmu_temp_source)
        with tempfile.TemporaryDirectory() as root:
            writer = recording.DailyWriter(Path(root), 1024**2)
            writer.write(row)
            writer.close()
            output = Path(root) / "csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(recording.run_export(Namespace(session=writer.directory, output_dir=output), asmond), 0)
            with (output / "metrics.csv").open() as handle:
                exported = next(csv.DictReader(handle))
            self.assertEqual(exported["temperatures.pmu_source"], current.pmu_temp_source)
            self.assertAlmostEqual(float(exported["temperatures.pmu_temp_avg_c"]), current.pmu_temp_avg_c)
        feed.snapshot.return_value = (asmond.MetricSample(), {"status": "unavailable"})
        absent = recording.Capture(asmond, args, feed).read()
        self.assertEqual(absent["sources"]["temperatures"]["status"], "unavailable")
        self.assertNotIn("pmu_source", absent["groups"]["temperatures"])

    def test_native_clock_fixtures_preserve_the_reported_mhz_and_usage(self):
        for frame in FIXTURE["gpu_clocks"]:
            with self.subTest(phase=frame["phase"]):
                sample = asmond.sample_from_plist({"gpu": frame["gpu"]})
                self.assertEqual(sample.gpu_freq_mhz, frame["expected_gpu_mhz"])
                self.assertAlmostEqual(sample.gpu_usage_pct, 100 * (1-frame["gpu"]["idle_ratio"]))

    def test_clock_units_are_explicit_and_native_table_bounds_are_checked(self):
        gpu = dict(FIXTURE["gpu_clocks"][0]["gpu"])
        for value, expected in ((0, 0), (338, 338), ("338 MHz", 338), ("0.338 GHz", 338), ("338000000 Hz", 338)):
            with self.subTest(value=value):
                sample = asmond.sample_from_plist({"gpu": {**gpu, "freq_hz": value}})
                self.assertEqual(sample.gpu_freq_mhz, expected)
        for bad in (None, -1, True, float("nan"), float("inf"), 338000000, .000338, "338 rpm"):
            with self.subTest(bad=bad):
                self.assertIsNone(asmond.sample_from_plist({"gpu": {**gpu, "freq_hz": bad}}).gpu_freq_mhz)
        for bad in (None, [], [None], [{}], [{"freq": 0}], [{"freq": -1}], [{"freq": float("nan")}], [{"freq": "338 rpm"}]):
            with self.subTest(states=bad):
                self.assertIsNone(asmond.sample_from_plist({"gpu": {**gpu, "dvfm_states": bad}}).gpu_freq_mhz)
        self.assertIsNone(asmond.sample_from_plist({"gpu": {**gpu, "sw_requested_state": None}}).gpu_freq_mhz)
        self.assertEqual(asmond.sample_from_plist({"gpu": {"freq_hz": 338000000}}).gpu_freq_mhz, 338)
        self.assertEqual(asmond_powermetrics.as_mhz("freq_hz", 338), .000338)

    def test_gpu_unit_fix_does_not_touch_intel_or_cpu_hz_fields(self):
        gpu = FIXTURE["gpu_clocks"][0]["gpu"]
        sample = asmond.sample_from_plist({"processor": {"cpu_watts": 3, "igpu_watts": .1, "freq_hz": 2500000000}, "gpu": gpu, "GPU": [{"name": "IntelIG", "freq_hz": 350000000, "misc_counters": {"GPU Busy": .02}}]})
        self.assertEqual(sample.p_freq_mhz, 2500)
        self.assertEqual(sample.gpu_freq_mhz, 350)
        sample = asmond.sample_from_plist({"gpu": {**gpu, "invalid": True}})
        self.assertIsNone(sample.gpu_freq_mhz)
