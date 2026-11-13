"""Intel identity regressions using minimized real Mini data and failure cases."""

import ctypes as c
import copy
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

import asmond
import asmond_ports
import asmond_powermetrics
import asmond_smc
import asmond_system as storage
import test_asmond


FIXTURE = json.loads((Path(__file__).parent / "tests/fixtures/intel_macmini_verified_sensors_usb.json").read_text())


def mini_reader():
    reader = object.__new__(asmond_smc.AppleSMCTemperatureReader)
    reader.available = True
    reader._lock = threading.Lock()
    reader.groups = asmond_smc.temperature_key_groups(FIXTURE["cpu_brand"], None, None, model=FIXTURE["model"])
    reader.read_fans = True
    reader._read_value = FIXTURE["smc_values"].get
    return reader


class IntelIdentityTests(unittest.TestCase):
    def test_conflicting_port_locations_keep_diagnostics_without_assigning_a_device(self):
        port = next(item for item in FIXTURE["usb_ports"] if item["UsbCPortNumber"] == 1)
        devices = FIXTURE["usb_devices"]
        other = {**port, "UsbCPortNumber": 2}
        for ports in ([port, other], [other, port]):
            with self.subTest(order=[item["UsbCPortNumber"] for item in ports]):
                cable, diagnostics = asmond_ports.xhci_usb_c_connections(ports, devices)
                self.assertEqual(cable, [])
                self.assertEqual([item.label for item in diagnostics], ["USB-C 1", "USB-C 2"])
                self.assertTrue(all(item.connected is None for item in diagnostics))
                self.assertEqual(diagnostics[0].counters, diagnostics[1].counters)
                apple = [{**item, "UsbIOPort": f"IOService:/Port-USB-C@{item['UsbCPortNumber']}"} for item in ports]
                apple_devices = [{**device, "UsbLinkSpeed": 10_000_000_000} for device in devices]
                self.assertEqual(asmond_ports.usb_devices_by_port(apple, apple_devices), {})
                self.assertEqual(asmond.usb_data_link_speeds_by_port(apple, apple_devices), {})

    def test_six_real_cores_and_auxiliaries_have_strict_model_cpu_gate(self):
        result = mini_reader().read()
        self.assertEqual(result.cpu_avg_c, sum(FIXTURE["smc_values"][f"TC{i}C"] for i in range(1, 7)) / 6)
        self.assertEqual(result.cpu_max_c, 79)
        self.assertIsNone(result.cpu_max_label)  # No established powermetrics CPU index.
        self.assertEqual(result.gpu_die_c, 69)
        self.assertIsNone(result.gpu_avg_c)  # One die sensor is not a cluster mean.
        self.assertEqual(result.cpu_peci_c, FIXTURE["smc_values"]["TCXC"])
        self.assertNotEqual(result.cpu_peci_c, FIXTURE["smc_values"]["TC0F"])
        for brand, model in ((FIXTURE["cpu_brand"], "Macmini7,1"), ("Intel Core i7-8700B", "Macmini8,1")):
            self.assertEqual(asmond_smc.temperature_key_groups(brand, None, None, model=model).cpu, ())
        self.assertEqual(asmond_smc.temperature_key_groups("Apple M4", 4, 6).cpu_labels[-1], "P9")

    def test_missing_or_sentinel_core_cannot_create_partial_mean(self):
        for value in (None, -127, 0, float("nan")):
            reader = mini_reader()
            values = {**FIXTURE["smc_values"], "TC6C": value, "TC7C": 120, "TC8C": 130}
            reader._read_value = values.get
            result = reader.read()
            self.assertIsNone(result.cpu_avg_c)
            self.assertIsNone(result.cpu_max_c)
            self.assertEqual(result.gpu_die_c, 69)

    def test_complete_intel_sample_is_enriched_without_generic_hid(self):
        sample = asmond.MetricSample(telemetry_source="intel", soc_temp_c=70, temp_max_c=70)
        with mock.patch.object(asmond, "HOST_MACHINE", "x86_64"), mock.patch.object(asmond, "SMC_TEMPS", mini_reader()), mock.patch.object(asmond.SSD_TEMPS, "snapshot", return_value=49.85), mock.patch.object(asmond.HID_TEMPS, "read_metrics", side_effect=AssertionError("HID must not replace real cores")):
            asmond.apply_indexed_fans(sample)
        self.assertEqual(sample.cpu_temp_max_c, 79)
        self.assertEqual(sample.gpu_die_temp_c, 69)
        self.assertEqual(sample.ssd_temp_c, 49.85)
        self.assertEqual(sample.temperature_source, "AppleSMC")
        self.assertNotIn("@", sample.temp_max_source)
        self.assertEqual(asmond.power_row_temperature(sample, "gpu"), 69)
        self.assertEqual(asmond.sample_to_report_dict(sample)["ssd_temp"], "49.9°C")
        sample.fan_rpms = {"F0Ac": 1979.610107421875}
        for width, height in ((48, 7), (72, 7), (100, 8), (100, 10)):
            window = test_asmond.LayoutTests.RecordingWindow(height, width)
            with mock.patch.object(asmond, "draw_box"):
                asmond.draw_thermals_panel(window, asmond.PanelPlacement("thermals", asmond.Rect(0, 0, height, width), "normal"), sample, asmond.BatteryStats(), test_asmond.LayoutTests.dashboard_colors())
            self.assertIn("SSD", window.rendered())
            self.assertIn("Fan 0", window.rendered())
            if width >= 72:
                self.assertIn("CPU PECI", window.rendered())
                self.assertIn("RAM proximity", window.rendered())
            self.assertTrue(all(0 <= y < height and 0 <= x and x + len(text) <= width for y, x, text in window.entries))

    def test_amd_first_is_never_combined_with_intel_watts(self):
        intel = {"name": "IntelIG", "freq_hz": 350_000_000, "misc_counters": {"GPU Busy": 0.02}}
        amd = {"name": "AMD Radeon", "freq_hz": 1_200_000_000, "misc_counters": {"GPU Busy": 0.8}}
        for gpus in ([amd, intel], [intel, amd]):
            sample = asmond.sample_from_plist({"processor": {"igpu_watts": .1}, "GPU": gpus})
            self.assertEqual(sample.gpu_power_mw, 100)
            self.assertEqual(sample.gpu_usage_pct, 2)
            self.assertEqual(sample.gpu_freq_mhz, 350)
        for gpus in ([amd], [intel, intel], [{"freq_hz": 300}, {"freq_hz": 400}], [{"vendor_id": 0x1002, "freq_hz": 100}], [{"name": "NotIntel", "freq_hz": 100}], [{"vendor_id": 0x8086, "name": "Intel Arc", "freq_hz": 100}]):
            sample = asmond.sample_from_plist({"processor": {"igpu_watts": .1}, "GPU": gpus, "gpu": {"freq_hz": 999_000_000, "idle_ratio": .1}})
            self.assertEqual(sample.gpu_power_mw, 100)
            self.assertIsNone(sample.gpu_usage_pct)
            self.assertIsNone(sample.gpu_freq_mhz)
            cache = {"gpu_freq_mhz": 350}
            asmond.keep_last_nonzero_frequencies(sample, cache)
            self.assertIsNone(sample.gpu_freq_mhz)
            self.assertNotIn("gpu_freq_mhz", cache)
        self.assertIsNone(asmond_powermetrics.intel_integrated_gpu({"GPU": [intel, "invalid"]}))

    def test_invalid_gpu_cleanup_cannot_create_single_unnamed_identity(self):
        unnamed = {"freq_hz": 1_200_000_000, "misc_counters": {"GPU Busy": 0.8}}
        for invalid in ({"name": "IntelIG", "invalid": True}, {"name": "AMD Radeon", "invalid": True}, None):
            for gpus in ([invalid, unnamed], [unnamed, invalid]):
                with self.subTest(gpus=gpus):
                    original = {"processor": {"igpu_watts": .1}, "GPU": gpus}
                    before = copy.deepcopy(original)
                    sample = asmond.sample_from_plist(original)
                    self.assertEqual(sample.gpu_power_mw, 100)
                    self.assertIsNone(sample.gpu_usage_pct)
                    self.assertIsNone(sample.gpu_freq_mhz)
                    self.assertEqual(original, before)

    def test_gpu_cleanup_retains_verified_identities_and_package_fallback(self):
        invalid = {"name": "AMD Radeon", "invalid": True}
        intel = {"name": "IntelIG", "freq_hz": 350_000_000, "misc_counters": {"GPU Busy": .02}}
        unnamed = {"freq_hz": 350_000_000, "misc_counters": {"GPU Busy": .02}}
        for gpus in ([invalid, intel], [intel, invalid], [unnamed]):
            with self.subTest(gpus=gpus):
                sample = asmond.sample_from_plist({"processor": {"igpu_watts": .1}, "GPU": gpus})
                self.assertEqual(sample.gpu_power_mw, 100)
                self.assertEqual(sample.gpu_usage_pct, 2)
                self.assertEqual(sample.gpu_freq_mhz, 350)
        sample = asmond.sample_from_plist({
            "processor": {"igpu_watts": .1, "packages": [{"gpu_active_ratio": .03, "igpu_freq_hz": 400_000_000}]},
            "GPU": [invalid, unnamed],
        })
        self.assertEqual(sample.gpu_power_mw, 100)
        self.assertEqual(sample.gpu_usage_pct, 3)
        self.assertEqual(sample.gpu_freq_mhz, 400)

    def test_real_xhci_links_have_no_charge_or_marker_claim(self):
        ports, diagnostics = asmond_ports.xhci_usb_c_connections(FIXTURE["usb_ports"], FIXTURE["usb_devices"])
        self.assertEqual([p.label for p in ports], [f"USB-C {i}" for i in range(1, 5)])
        self.assertEqual([p.data_link_speed_bps for p in ports], [10_000_000_000] * 4)
        self.assertTrue(all(not p.cable_query_supported and p.power_w is None and p.current_a is None for p in ports))
        usb = asmond.UsbCStats(ports=ports, diagnostics=diagnostics)
        self.assertFalse(asmond.usb_c_supported(usb))
        self.assertTrue(asmond.cable_panel_supported(usb))
        self.assertEqual(asmond.effective_charge_panel("battery", asmond.BatteryStats(), usb), "cable")
        self.assertEqual(asmond.next_charge_panel("battery", 1, asmond.BatteryStats(), usb), "diagnostics")
        self.assertEqual(asmond.effective_charge_panel(asmond.next_charge_panel("diagnostics", 1, asmond.BatteryStats(), usb), asmond.BatteryStats(), usb), "cable")
        self.assertEqual(asmond.next_charge_panel("battery", 1, asmond.BatteryStats(), usb, False), "battery")
        self.assertEqual(asmond.cycle_cable_port(usb, "USB-C 1", 1), "USB-C 2")
        self.assertEqual(asmond.cycle_cable_port(usb, "USB-C 1", -1), "USB-C 4")
        self.assertTrue(all(p["dp_alt_mode"] == "unavailable" for p in asmond.usb_to_report_dict(usb)["ports"]))
        self.assertEqual(diagnostics[0].counters["Link error"], 0)
        self.assertIs(diagnostics[0].overcurrent_active, False)
        self.assertIsNone(diagnostics[0].liquid_detected)
        self.assertIn("XHCI", asmond.port_diagnostic_rows(usb, 0)[0][2])

    def test_legacy_and_host_speed_enums_are_distinct_and_conflicts_unknown(self):
        port = FIXTURE["usb_ports"][:1]
        location = port[0]["locationID"]
        for props, expected in (({"USBSpeed": 5}, 10_000_000_000), ({"Device Speed": 4}, 10_000_000_000), ({"USBSpeed": 4}, 5_000_000_000), ({"USBSpeed": 5, "Device Speed": 4}, 10_000_000_000), ({"USBSpeed": 4, "Device Speed": 4}, None), ({"USBSpeed": True}, None), ({"USBSpeed": 7, "Device Speed": 4}, None)):
            output, _ = asmond_ports.xhci_usb_c_connections(port, [{"locationID": location, **props}])
            self.assertEqual(output[0].data_link_speed_bps, expected)

    def test_ambiguous_identities_and_usb2_companions_are_not_guessed(self):
        port = FIXTURE["usb_ports"][:1]
        self.assertEqual(asmond_ports.xhci_usb_c_connections(port * 2, FIXTURE["usb_devices"]), ([], []))
        output, diagnostics = asmond_ports.xhci_usb_c_connections(port, [{"locationID": 1234, "USBSpeed": 5}])
        self.assertEqual(output, [])
        self.assertIsNone(diagnostics[0].connected)  # No false physical disconnect.
        self.assertEqual(asmond_ports.xhci_usb_c_connections([{**port[0], "UsbCPortNumber": True}], []), ([], []))

    def test_intel_without_hpm_collects_topology_but_apple_path_stays_conditional(self):
        for machine, expected in (("x86_64", 4), ("arm64", 0)):
            with mock.patch.object(asmond, "HOST_MACHINE", machine), mock.patch.object(asmond, "read_battery_items", return_value=None), mock.patch.object(asmond, "read_hpm_port_items", return_value=[]), mock.patch.object(asmond, "read_usb_port_items", return_value=FIXTURE["usb_ports"]) as read_ports, mock.patch.object(asmond, "read_usb_device_items", return_value=FIXTURE["usb_devices"]):
                battery, usb = asmond.read_charge_stats()
            self.assertFalse(asmond.battery_supported(battery))
            self.assertEqual(len(usb.ports), expected)
            self.assertEqual(read_ports.call_count, 1 if expected else 0)

    def test_xhci_fallback_preserves_existing_charge_port_and_controller_snapshot(self):
        charge_port = asmond.UsbCPortStats("USB-C 1", connected=True, power_w=45)
        controller = asmond_ports.PortDiagnostic("controller:0", "Controller 1 (port unknown)", mapped=False, counters={"Attach": 5})
        original = asmond.UsbCStats(ports=[charge_port], active_index=0, diagnostics=[controller])
        with mock.patch.object(asmond, "HOST_MACHINE", "x86_64"), mock.patch.object(asmond, "read_battery_items", return_value=None), mock.patch.object(asmond, "read_hpm_port_items", return_value=[]), mock.patch.object(asmond, "read_usb_port_items", return_value=FIXTURE["usb_ports"]), mock.patch.object(asmond, "read_usb_device_items", return_value=FIXTURE["usb_devices"]), mock.patch.object(asmond, "usb_c_stats_from_item", return_value=original):
            _, usb = asmond.read_charge_stats()
        self.assertIs(usb.active_port, charge_port)
        self.assertEqual(usb.active_port.power_w, 45)
        self.assertEqual(len(usb.ports), 4)
        self.assertIn(controller, usb.diagnostics)
        self.assertTrue(charge_port.charge_query_supported)
        self.assertTrue(charge_port.data_link_query_supported)
        self.assertEqual(charge_port.data_link_speed_bps, 10_000_000_000)
        self.assertIsNone(charge_port.device_info)  # Minimized sensor fixture has no descriptors.
        self.assertTrue(asmond.usb_c_supported(usb))

    def test_explicit_controller_and_xhci_device_share_one_port_without_invented_charging(self):
        fixture = json.loads((Path(__file__).parent / "tests/fixtures/usb_device_identity.json").read_text())
        port = next(p for p in fixture["usb_ports"] if p["UsbCPortNumber"] == 1)
        devices = [d for d in fixture["usb_devices"] if d.get("locationID") == port["locationID"]]
        for external in (False, True):
            battery = {"ExternalConnected": external, "IsCharging": external, "Temperature": 3000,
                       "AdapterDetails": {"Watts": 45}, "PortControllerInfo": [{"PortType": 2, "PortNumber": 1}]}
            with self.subTest(external=external), mock.patch.object(asmond, "HOST_MACHINE", "x86_64"), mock.patch.object(asmond, "read_battery_items", return_value=battery), mock.patch.object(asmond, "read_hpm_port_items", return_value=[]), mock.patch.object(asmond, "read_usb_port_items", return_value=[port]), mock.patch.object(asmond, "read_usb_device_items", return_value=devices), mock.patch.object(asmond.USB_DEVICE_DETAILS, "snapshot", return_value={}):
                _, usb = asmond.read_charge_stats()
            self.assertEqual(len(usb.ports), 1)
            merged = usb.ports[0]
            self.assertEqual(merged.label, "USB-C 1")
            self.assertTrue(merged.connected)
            self.assertTrue(merged.charge_query_supported)
            self.assertTrue(merged.data_link_query_supported)
            self.assertEqual(merged.data_link_speed_bps, 10_000_000_000)
            self.assertIsNotNone(merged.device_info)
            self.assertTrue(asmond.cable_panel_supported(usb))
            self.assertEqual(asmond.connected_cable_ports(usb), [merged])
            self.assertIsNone(usb.active_index)  # No inferred physical charging port.
            self.assertIsNone(merged.power_w)
            self.assertEqual(merged.role, "source/data")
            self.assertEqual(asmond.charge_input_active(usb), external)

    def test_xhci_counter_provider_is_not_dropped_or_mixed_with_controller_counters(self):
        controller = asmond_ports.PortDiagnostic("USB-C 1", "USB-C 1", source="controller", counters={"Link error": 7})
        original = asmond.UsbCStats(diagnostics=[controller])
        with mock.patch.object(asmond, "HOST_MACHINE", "x86_64"), mock.patch.object(asmond, "read_battery_items", return_value=None), mock.patch.object(asmond, "read_hpm_port_items", return_value=[]), mock.patch.object(asmond, "read_usb_port_items", return_value=FIXTURE["usb_ports"][:1]), mock.patch.object(asmond, "read_usb_device_items", return_value=FIXTURE["usb_devices"]), mock.patch.object(asmond.USB_DEVICE_DETAILS, "snapshot", return_value={}), mock.patch.object(asmond, "usb_c_stats_from_item", return_value=original):
            _, usb = asmond.read_charge_stats()
        self.assertEqual(len(usb.diagnostics), 2)
        self.assertEqual(controller.counters["Link error"], 7)
        xhci = usb.diagnostics[1]
        self.assertNotEqual(xhci.key, controller.key)
        self.assertEqual(xhci.counters["Link error"], 0)
        self.assertIn("XHCI", xhci.source)
        self.assertFalse(usb.ports[0].charge_query_supported)
        self.assertFalse(asmond.usb_c_supported(usb))

    def test_focus_hint_explains_menu_path_and_fits(self):
        usb = asmond.UsbCStats(diagnostics=[asmond_ports.PortDiagnostic("USB-C 1", "USB-C 1", connected=True)])
        for width in (30, 48, 90):
            window = test_asmond.LayoutTests.RecordingWindow(9, width)
            asmond.draw_port_diagnostic_section(window, asmond.Rect(0, 0, 9, width), usb, 0, test_asmond.LayoutTests.dashboard_colors())
            text = window.rendered()
            self.assertNotIn("m: diagnostics", text)
            self.assertTrue("m → Port diagnostics" in text or "menu (m)" in text)


class UsbDeviceIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / "tests/fixtures/usb_device_identity.json").read_text())

    def test_real_storage_model_and_physical_capacity_ignore_larger_raid(self):
        ports, _ = asmond_ports.xhci_usb_c_connections(self.fixture["usb_ports"], self.fixture["usb_devices"])
        self.assertEqual(len(ports), 4)
        by_label = {port.label: port.device_info for port in ports}
        samsung = by_label["USB-C 1"]
        self.assertEqual(samsung.manufacturer, "Ugreen")
        self.assertEqual(samsung.storage_model, "SSD 970 EVO Plus")
        self.assertEqual(samsung.capacity_bytes, 1_000_204_886_016)
        kingston = by_label["USB-C 2"]
        self.assertEqual(kingston.storage_model, "SNVS500G")
        self.assertEqual(kingston.storage_manufacturer, "KINGSTON")
        self.assertEqual(kingston.capacity_bytes, 500_107_862_016)
        self.assertNotEqual(kingston.capacity_bytes, 999_527_743_488)  # Real RAID Whole=True below a partition.
        self.assertNotIn("NVMe", asmond.usb_device_summary(kingston))  # USB enclosure does not establish that protocol.

    def test_apple_explicit_mapping_reuses_same_device_and_rejects_ambiguity(self):
        device = self.fixture["usb_devices"][0]
        location = device["locationID"]
        ports = [{"UsbIOPort": "IOService:/machine/Port-USB-C@2", "locationID": location}]
        result = asmond_ports.usb_devices_by_port(ports, [device])
        self.assertEqual(result[(2, 2)].capacity_bytes, 500_107_862_016)
        self.assertEqual(asmond_ports.usb_devices_by_port(ports, [device, device]), {})
        self.assertEqual(asmond_ports.usb_devices_by_port(ports * 2, [device]), {})
        self.assertEqual(asmond_ports.usb_devices_by_port([{**ports[0], "UsbIOPort": "unknown"}], [device]), {})
        unknown = {"locationID": location + 1}
        self.assertEqual(asmond_ports.usb_devices_by_port([*ports, {**ports[0], "locationID": location + 1}], [device, unknown]), {})
        hpm = [{"PortType": 2, "PortNumber": 2, "ConnectionActive": True, "TransportsActive": ["USB3"]}]
        stats = asmond.usb_c_stats_from_item({}, hpm, [], ports, [device])
        self.assertEqual(stats.ports[0].device_info, result[(2, 2)])
        hpm[0]["ConnectionActive"] = False
        stats = asmond.usb_c_stats_from_item({}, hpm, [], ports, [device])
        self.assertIsNone(stats.ports[0].device_info)

    def test_real_m4_attachment_matches_usb_c_two_without_cable_identity(self):
        data = self.fixture["apple_silicon"]
        stats = asmond.usb_c_stats_from_item({}, data["hpm_items"], [], data["usb_ports"], data["usb_devices"])
        selected = asmond.selected_cable_port(stats)
        self.assertEqual(selected.label, "USB-C 2")
        self.assertIsNone(selected.cable_info)
        self.assertEqual(selected.device_info.manufacturer, "Ugreen")
        self.assertEqual(selected.device_info.storage_manufacturer, "MSI M470")
        self.assertEqual(selected.device_info.storage_model, "PRO 1TB")
        self.assertNotIn("device_storage_model", asmond.usb_to_report_dict(stats)["ports"][1])
        self.assertEqual(selected.device_info.capacity_bytes, 1_000_204_886_016)
        self.assertIsNone(next(port for port in stats.ports if port.label == "USB-C 1").device_info)

    def test_hub_never_inherits_child_disk_and_multi_lun_is_ambiguous(self):
        device = copy.deepcopy(self.fixture["usb_devices"][0])
        hub = {"IOObjectClass": "IOUSBHostDevice", "USB Product Name": "USB Hub", "IORegistryEntryChildren": [device]}
        info = asmond_ports.usb_device_info(hub)
        self.assertEqual(info.product, "USB Hub")
        self.assertIsNone(info.capacity_bytes)
        self.assertEqual(info.storage_model, "")
        device["IORegistryEntryChildren"] *= 2
        info = asmond_ports.usb_device_info(device)
        self.assertIsNone(info.capacity_bytes)
        self.assertEqual(info.storage_model, "")

    def test_invalid_size_missing_identity_and_serial_fields_do_not_become_values(self):
        for value in (None, True, -1, 0, 500.5, 2**64):
            root = {"USB Product Name": "Enclosure", "USB Serial Number": "PRIVATE_SERIAL", "IORegistryEntryChildren": [{"IOObjectClass": "IOBlockStorageDriver", "IORegistryEntryChildren": [{"IOObjectClass": "IOMedia", "Whole": True, "Size": value}]}]}
            info = asmond_ports.usb_device_info(root)
            self.assertIsNone(info.capacity_bytes)
            self.assertNotIn("PRIVATE_SERIAL", repr(info))
        self.assertIsNone(asmond_ports.usb_device_info({"USB Product Name": "unknown"}))
        info = asmond_ports.usb_device_info({"USB Product Name": "A\x1b\nB", "USB Vendor Name": " Maker "})
        self.assertEqual(info.product, "A B")
        self.assertEqual(info.manufacturer, "Maker")

    def test_renderer_device_under_pd_and_narrow_bounds_with_and_without_marker(self):
        device = asmond_ports.usb_device_info(self.fixture["usb_devices"][0])
        for marker in (None, asmond.CableInfo(cable_type="passive", current_label="5 A", pd_revision="3.0")):
            port = asmond.UsbCPortStats("USB-C 2", connected=True, cable_query_supported=True,
                cable_info=marker, active_transports=["USB3"], data_link_speed_bps=10_000_000_000, device_info=device)
            usb = asmond.UsbCStats(ports=[port])
            for width in (30, 48, 58, 72, 96, 160):
                window = test_asmond.LayoutTests.RecordingWindow(8, width + 4)
                asmond.draw_cable_section(window, 1, 2, 6, width, usb, test_asmond.LayoutTests.dashboard_colors())
                self.assertIn("Device", window.rendered())
                if width >= 96:
                    self.assertIn("500.1 GB", window.rendered())
                    self.assertIn("SNVS500G", window.rendered())
                    self.assertIn("10 Gb/s", window.rendered())
                if marker is not None and width >= 96:
                    self.assertIn("PD 3.0", window.rendered_row(1))
                    device_y = next(y for y, _x, text in window.entries if text == "Device")
                    self.assertGreater(device_y, 1)
                self.assertIn("Port", window.rendered_row(1))
                self.assertIn("Status", window.rendered_row(2))
                self.assertTrue(all(1 <= y <= 6 and 2 <= x and x + len(text) <= width + 2 for y, x, text in window.entries))
            report = asmond.usb_to_report_dict(usb)["ports"][0]
            self.assertEqual(report["device_capacity_bytes"], 500_107_862_016)
            self.assertNotIn("device_storage_model", report)
            port.connected = False
            self.assertNotIn("device_capacity_bytes", asmond.usb_to_report_dict(usb)["ports"][0])

    def test_matching_capacity_in_model_is_not_repeated_but_conflicts_remain(self):
        data = self.fixture["apple_silicon"]
        stats = asmond.usb_c_stats_from_item({}, data["hpm_items"], [], data["usb_ports"], data["usb_devices"])
        port = next(port for port in stats.ports if port.label == "USB-C 2")
        before = asmond.usb_to_report_dict(stats)
        window = test_asmond.LayoutTests.RecordingWindow(8, 104)
        asmond.draw_cable_section(window, 1, 2, 6, 100, stats, test_asmond.LayoutTests.dashboard_colors())
        self.assertIn("MSI M470 PRO 1TB", window.rendered())
        self.assertNotIn("Capacity", window.rendered())
        self.assertNotIn(" · 1 TB", asmond.usb_device_summary(port.device_info))
        self.assertEqual(asmond.usb_to_report_dict(stats), before)
        self.assertEqual(before["ports"][1]["device_capacity_bytes"], 1_000_204_886_016)
        for model, size, expected in (
            ("Model 1TB", 1_000_204_886_016, True),
            ("Model 1000 GB", 1_000_204_886_016, True),
            ("Model 2TB", 1_000_204_886_016, False),
            ("Model SNVS500G", 500_107_862_016, False),
            ("Model 500GB", 500_107_862_016, False),
            ("Model 1 TB/s", 1_000_204_886_016, False),
            ("Model 1TBW", 1_000_204_886_016, False),
            ("Model 1TB", None, False),
        ):
            self.assertEqual(asmond.device_model_shows_capacity(asmond.UsbDeviceInfo(storage_model=model, capacity_bytes=size)), expected)

    def test_rating_packs_only_complete_values_and_keeps_narrow_fallback_rows(self):
        info = asmond.CableInfo(max_power_w=240, max_voltage_v=50, current_label="5 A", pd_revision="PD 3.0")
        self.assertEqual(asmond.cable_rating_rows(info, 40), [("Rating", "240.0 W · 50 V max · 5 A · PD 3.0")])
        narrow = asmond.cable_rating_rows(info, 19)
        self.assertEqual(narrow, [("Rating", "240.0 W · 50 V max"), ("Current", "5 A"), ("PD", "PD 3.0")])
        self.assertTrue(all(len(value) <= 19 for _name, value in narrow))

    def test_registry_query_requests_descendant_properties_only_for_devices(self):
        with mock.patch.object(asmond.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"")) as run:
            asmond.read_usb_device_metadata_items()
            self.assertIn("-l", run.call_args.args[0])
            asmond.read_usb_device_items()
            self.assertNotIn("-l", run.call_args.args[0])
            asmond.read_usb_port_items()
            self.assertNotIn("-l", run.call_args.args[0])

    def test_slow_metadata_worker_does_not_block_charge_and_binds_registry_id(self):
        started, release = threading.Event(), threading.Event()
        cache = asmond.UsbDeviceMetadataCache()
        device = {"IORegistryEntryID": 101, "USB Product Name": "Old device"}
        def slow_read():
            started.set()
            release.wait(timeout=2)
            return [device]
        with mock.patch.object(asmond, "read_usb_device_metadata_items", side_effect=slow_read) as read:
            try:
                self.assertEqual(cache.snapshot([device], now=100), {})
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(cache.snapshot([device], now=101), {})
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
                cache.thread.join(timeout=1)
        self.assertEqual(cache.snapshot([device], now=101)[101].product, "Old device")
        cache.refreshing = True  # A later blocked refresh must not extend age.
        cache.started_at = 110
        self.assertEqual(cache.snapshot([device], now=116), {})
        reconnected = {"IORegistryEntryID": 202, "locationID": 1234}
        self.assertEqual(cache.snapshot([reconnected], now=105), {})
        self.assertEqual(asmond_ports.device_info_with_details(reconnected, cache.values), None)


class NVMeTemperatureTests(unittest.TestCase):
    def test_smart_log_composite_kelvin_is_converted_and_sentinels_rejected(self):
        for kelvin, expected in ((323, 49.85), (0, None), (65535, None), (273, None), (500, None)):
            data = bytearray(512)
            data[1:3] = kelvin.to_bytes(2, "little")
            value = storage.nvme_temperature_c(bytes(data))
            self.assertAlmostEqual(value, expected) if expected is not None else self.assertIsNone(value)
        self.assertIsNone(storage.nvme_temperature_c(b"\x43\x01"))
        self.assertEqual(storage._SMARTInterface.smart_read_data.offset, 40)

    def test_enumeration_requires_one_proven_internal_capable_device_and_releases_all(self):
        for internal, capable, expected in (({2}, {2}, 50), ({2, 3}, {2, 3}, None), ({2}, {3}, None), (set(), {2}, None)):
            reader = object.__new__(storage.InternalNVMeReader)
            reader.io = mock.Mock()
            reader.io.IOServiceGetMatchingServices.side_effect = lambda _a, _b, pointer: setattr(pointer._obj, "value", 99) or 0
            reader.io.IOIteratorNext.side_effect = [1, 2, 3, 0]
            reader._internal = lambda device: device in internal
            reader._capable = lambda device: device in capable
            reader._read_device = mock.Mock(return_value=50)
            self.assertEqual(reader.read(), expected)
            self.assertEqual(sorted(getattr(call.args[0], "value", call.args[0]) for call in reader.io.IOObjectRelease.call_args_list), [1, 2, 3, 99])
            self.assertEqual(reader._read_device.call_count, 1 if expected else 0)

    def test_smart_ffi_success_and_failure_release_interfaces(self):
        for plugin_status, query_status, read_status in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
            releases = []
            release = c.CFUNCTYPE(storage.U, storage.V)(lambda _p: releases.append("smart") or 0)
            def read(_p, buffer):
                c.cast(buffer, c.POINTER(c.c_ubyte * 512)).contents[1:3] = [0x43, 0x01]
                return read_status
            read_fn = c.CFUNCTYPE(c.c_int, storage.V, storage.V)(read)
            smart = storage._SMARTInterface(version=1, release=c.cast(release, storage.V), smart_read_data=c.cast(read_fn, storage.V))
            smart_ptr = c.pointer(c.pointer(smart))
            def query(_p, _uuid, output):
                if query_status == 0:
                    output[0] = c.cast(smart_ptr, storage.V)
                return query_status
            query_fn = c.CFUNCTYPE(c.c_int32, storage.V, storage._UUIDBytes, c.POINTER(storage.V))(query)
            plugin = storage._SMARTInterface(query=c.cast(query_fn, storage.V))
            plugin_ptr = c.pointer(c.pointer(plugin))
            reader = object.__new__(storage.InternalNVMeReader)
            reader.cf, reader.io = mock.Mock(), mock.Mock()
            reader._uuid = mock.Mock(side_effect=[101, 102])
            def create(_device, _client, _interface, output, _score):
                if plugin_status == 0:
                    output._obj.value = c.cast(plugin_ptr, storage.V).value
                return plugin_status
            reader.io.IOCreatePlugInInterfaceForService.side_effect = create
            result = reader._read_device(7)
            if plugin_status or query_status or read_status:
                self.assertIsNone(result)
            else:
                self.assertAlmostEqual(result, 49.85)
            self.assertEqual(len(releases), 1 if plugin_status == query_status == 0 else 0)
            self.assertEqual(reader.io.IODestroyPlugInInterface.call_count, 0 if plugin_status else 1)
            self.assertEqual(reader.cf.CFRelease.call_args_list, [mock.call(101), mock.call(102)])

    def test_cache_stuck_native_read_cannot_spawn_or_refresh_stale_value(self):
        cache = storage.StorageTemperatureCache()
        cache.value, cache.value_started_at = 50, 10
        cache.started_at, cache.refreshing = 20, True
        cache.thread = mock.Mock()
        with mock.patch.object(storage.platform, "system", return_value="Darwin"), mock.patch.object(storage.threading, "Thread") as thread:
            self.assertEqual(cache.snapshot(now=25), 50)
            self.assertIsNone(cache.snapshot(now=41))
            thread.assert_not_called()
        with mock.patch.object(storage.platform, "system", return_value="Linux"):
            self.assertIsNone(cache.snapshot(now=25))

    def test_cache_invalidation_discards_old_and_inflight_values_without_extra_workers(self):
        cache = storage.StorageTemperatureCache()
        cache.value, cache.started_at, cache.value_started_at = 45.85, 100, 100
        entered, release = threading.Event(), threading.Event()
        reads = []

        def read():
            reads.append(len(reads))
            if len(reads) == 1:
                entered.set()
                release.wait(timeout=2)
                return 46
            return 52

        with mock.patch.object(storage.platform, "system", return_value="Darwin"), mock.patch.object(storage, "InternalNVMeReader") as reader:
            reader.return_value.read.side_effect = read
            try:
                cache.snapshot(now=111)
                worker = cache.thread
                self.assertTrue(entered.wait(timeout=1))
                cache.invalidate()
                self.assertIsNone(cache.snapshot(now=111.1))
                self.assertIs(cache.thread, worker)
                self.assertEqual(len(reads), 1)
                cache.invalidate()
                self.assertIsNone(cache.snapshot(now=111.2))
                self.assertIs(cache.thread, worker)
                release.set()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(cache.value)
                self.assertEqual(cache.value_started_at, 0)
                self.assertEqual(cache.snapshot(now=111.3, wait_s=1), 52)
                self.assertEqual(len(reads), 2)
            finally:
                release.set()
                if cache.thread is not None:
                    cache.thread.join(timeout=1)

    def test_cache_success_failure_and_native_error(self):
        for result in (49.85, None, OSError("unavailable")):
            cache = storage.StorageTemperatureCache()
            with mock.patch.object(storage.platform, "system", return_value="Darwin"), mock.patch.object(storage, "InternalNVMeReader") as reader:
                if isinstance(result, Exception):
                    reader.return_value.read.side_effect = result
                else:
                    reader.return_value.read.return_value = result
                value = cache.snapshot(now=10, wait_s=1)
            self.assertEqual(value, result if isinstance(result, float) else None)
            self.assertFalse(cache.refreshing)
