"""Isolated native-schema, lifecycle, history and geometry regressions."""
import argparse
import contextlib
import ctypes
import queue
from unittest import mock

import test_asmond as fixtures
import asmond
import asmond_metrics_memory as memory


class BandwidthSamplerTests(fixtures.IsolatedTestCase):
    @contextlib.contextmanager
    def sampler(self):
        clock = [10.0, 1010.0]
        reader = mock.Mock()
        reader.read.return_value = ((7, 1, 7, 2), (100, 200))
        with (mock.patch.object(memory, "_IOReportBandwidthReader", return_value=reader) as factory,
              mock.patch.object(memory.sys, "platform", "darwin"),
              mock.patch.object(memory.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(memory.time, "time", side_effect=lambda: clock[1])):
            sampler = memory.MemoryBandwidthSampler()
            try:
                yield sampler, reader, factory, clock
            finally:
                sampler.close()

    def test_requires_progress_before_zero_is_a_valid_measurement(self):
        with self.sampler() as (sampler, reader, factory, clock):
            self.assertIsNone(sampler.read(1).total_bps)
            clock[:] = [11, 1011]
            self.assertIsNone(sampler.read(1).total_bps)
            clock[:] = [12.5, 1012.5]
            reader.read.return_value = ((7, 1, 7, 2), (400, 350))
            current = sampler.read(1)
            self.assertEqual((current.read_bps, current.write_bps, current.total_bps), (200, 100, 300))
            self.assertEqual(current.sampled_at, 12.5)
            self.assertEqual(current.source, memory.MEMORY_BANDWIDTH_SOURCE)
            clock[:] = [13.5, 1013.5]
            self.assertEqual(sampler.read(1).total_bps, 0)
            factory.assert_called_once()

    def test_source_change_reset_and_clock_discontinuities_rebase_both_directions(self):
        for event in ("identity", "decrease", "wall", "pause", "backwards", "same", "reset"):
            with self.subTest(event=event), self.sampler() as (sampler, reader, _, clock):
                sampler.read(1)
                clock[:] = [11, 1011]
                reader.read.return_value = ((7, 1, 7, 2), (400, 350))
                self.assertEqual(sampler.read(1).total_bps, 450)
                clock[:] = [12, 1012]
                if event == "identity":
                    reader.read.return_value = ((8, 1, 8, 2), (500, 450))
                elif event == "decrease":
                    reader.read.return_value = ((7, 1, 7, 2), (399, 500))
                elif event == "wall":
                    clock[1] += 20
                elif event == "pause":
                    clock[:] = [21, 1021]
                elif event == "backwards":
                    clock[:] = [10, 1010]
                elif event == "same":
                    clock[:] = [11, 1011]
                else:
                    sampler.reset()
                self.assertIsNone(sampler.read(1).total_bps)
                clock[:] = [clock[0] + 1, clock[1] + 1]
                # Standing counters after rebasing do not qualify a new source.
                self.assertIsNone(sampler.read(1).total_bps)

    def test_progress_in_only_one_counter_does_not_qualify_the_other_as_zero(self):
        with self.sampler() as (sampler, reader, _, clock):
            sampler.read(1)
            clock[:] = [11, 1011]
            reader.read.return_value = ((7, 1, 7, 2), (400, 200))
            self.assertIsNone(sampler.read(1).total_bps)
            clock[:] = [12, 1012]
            reader.read.return_value = ((7, 1, 7, 2), (400, 300))
            self.assertEqual(sampler.read(1).total_bps, 100)

    def test_failure_releases_native_objects_and_retries_with_a_fresh_baseline(self):
        with self.sampler() as (sampler, reader, factory, clock):
            sampler.read(1)
            reader.read.side_effect = OSError("fixture unavailable")
            clock[:] = [11, 1011]
            self.assertIsNone(sampler.read(1).total_bps)
            reader.close.assert_called_once()
            reader.read.side_effect = None
            clock[:] = [70, 1070]
            sampler.read(1)
            factory.assert_called_once()
            clock[:] = [71, 1071]
            self.assertIsNone(sampler.read(1).total_bps)
            self.assertEqual(factory.call_count, 2)
            sampler.close()
            sampler.close()
            self.assertEqual(reader.close.call_count, 2)

    def test_unavailable_library_and_disabled_platform_never_produce_zero(self):
        with self.sampler() as (sampler, _, factory, _):
            factory.side_effect = OSError("fixture library absent")
            self.assertIsNone(sampler.read(1).total_bps)
            factory.assert_called_once()
        with mock.patch.object(memory, "_IOReportBandwidthReader") as factory:
            self.assertIsNone(memory.MemoryBandwidthSampler(enabled=False).read(1).total_bps)
            with mock.patch.object(memory.sys, "platform", "linux"):
                self.assertIsNone(memory.MemoryBandwidthSampler().read(1).total_bps)
            factory.assert_not_called()


class BandwidthNativeSchemaTests(fixtures.IsolatedTestCase):
    def reader(self, channels=None):
        reader = object.__new__(memory._IOReportBandwidthReader)
        reader.refs, reader.key, reader.subscription, reader.channels = [], "channels", 20, 21
        reader.cf, reader.io = mock.Mock(), mock.Mock()
        reader._text = lambda value: value
        channels = channels if channels is not None else [self.channel("DCS RD", 1), self.channel("DCS WR", 2)]
        reader.cf.CFDictionaryGetValue.side_effect = lambda dictionary, key: dictionary.get(key)
        reader.cf.CFArrayGetCount.side_effect = len
        reader.cf.CFArrayGetValueAtIndex.side_effect = lambda array, index: array[index]
        for function, field in (("Group", "group"), ("SubGroup", "subgroup"), ("ChannelName", "name"),
                                ("Format", "format"), ("Unit", "unit"), ("DriverID", "driver"), ("ChannelID", "id")):
            getattr(reader.io, "IOReportChannelGet" + function).side_effect = lambda channel, field=field: channel[field]
        reader.io.IOReportCreateSamples.return_value = {"channels": channels}
        reader.io.IOReportSimpleGetIntegerValue.side_effect = lambda channel, _: channel["value"]
        return reader

    @staticmethod
    def channel(name, identity):
        return {"name": name, "id": identity, "driver": 7, "group": "AMC Stats", "subgroup": "Perf Counters",
                "format": 1, "unit": memory.IOREPORT_BYTE_UNIT, "value": identity * 100}

    def test_accepts_only_exact_controller_pair_with_real_byte_metadata(self):
        channels = [self.channel("DCS RD", 1), self.channel("DCS WR", 2), self.channel("DCS", 3),
                    self.channel("DCS F1 RD", 4), self.channel("PCPU DCS RD", 5)]
        reader = self.reader(channels)
        self.assertEqual(reader.read(), ((7, 1, 7, 2), (100, 200)))
        reader.cf.CFRelease.assert_called_once()

    def test_rejects_ambiguous_identity_units_schema_and_invalid_sentinels(self):
        for field, value in (("name", "DCS F1 RD"), ("driver", 8), ("driver", 0), ("id", 0), ("id", 2),
                             ("format", 2), ("unit", 0), ("group", "PMP"), ("subgroup", "DCS BW"),
                             ("value", -1), ("value", -(1 << 63))):
            with self.subTest(field=field, value=value):
                channels = [self.channel("DCS RD", 1), self.channel("DCS WR", 2)]
                channels[0][field] = value
                reader = self.reader(channels)
                with self.assertRaises(OSError):
                    reader.read()
                reader.cf.CFRelease.assert_called_once()
        reader = self.reader([self.channel("DCS RD", 1), self.channel("DCS WR", 2), self.channel("DCS RD", 3)])
        with self.assertRaises(OSError):
            reader.read()

    def test_missing_samples_and_arrays_cannot_escape_cleanup(self):
        reader = self.reader()
        reader.io.IOReportCreateSamples.return_value = None
        with self.assertRaises(OSError):
            reader.read()
        reader.cf.CFRelease.assert_not_called()
        for channels in (None, [], [None]):
            reader.io.IOReportCreateSamples.return_value = {"channels": channels}
            with self.assertRaises(OSError):
                reader.read()
        self.assertEqual(reader.cf.CFRelease.call_count, 3)

    def test_partial_initialization_interruption_releases_all_owned_refs(self):
        cf = mock.Mock()
        def interrupt(reader):
            reader._own(40)
            reader._own(41)
            raise KeyboardInterrupt
        with (mock.patch.object(ctypes, "CDLL", return_value=cf),
              mock.patch.object(memory._IOReportBandwidthReader, "_bind_api"),
              mock.patch.object(memory._IOReportBandwidthReader, "_open", interrupt)):
            with self.assertRaises(KeyboardInterrupt):
                memory._IOReportBandwidthReader()
        self.assertEqual(cf.CFRelease.call_args_list, [mock.call(41), mock.call(40)])

    def test_subscription_selects_two_counters_and_cleans_partial_failures(self):
        for subscription, actual in ((104, 103), (0, 103), (104, 0)):
            with self.subTest(subscription=subscription, actual=actual):
                reader = self.reader()
                channels = [self.channel("DCS RD", 1), self.channel("DCS WR", 2), self.channel("DCS", 3)]
                objects = {100: {"channels": channels}, 101: []}
                reader._string = lambda text: text.decode().replace("IOReportChannels", "channels")
                reader.io.IOReportCopyChannelsInGroup.return_value = 100
                reader.cf.CFDictionaryGetValue.side_effect = lambda dictionary, key: objects[dictionary][key]
                reader.cf.CFArrayCreateMutable.return_value = 101
                reader.cf.CFArrayAppendValue.side_effect = lambda pointer, channel: objects[pointer].append(channel)
                def copy(*_):
                    objects[102] = objects[100].copy()
                    return 102
                reader.cf.CFDictionaryCreateMutableCopy.side_effect = copy
                reader.cf.CFDictionarySetValue.side_effect = lambda pointer, key, value: objects[pointer].update({key: objects[value]})
                def subscribe(_allocator, request, result, _flags, _options):
                    result._obj.value = actual
                    objects[103] = objects[request]
                    return subscription
                reader.io.IOReportCreateSubscription.side_effect = subscribe
                callbacks = ctypes.c_byte()
                with mock.patch.object(memory.c, "c_byte", mock.Mock(in_dll=mock.Mock(return_value=callbacks))):
                    if subscription and actual:
                        reader._open()
                        self.assertEqual(reader.channels, actual)
                    else:
                        with self.assertRaises(OSError):
                            reader._open()
                self.assertEqual([channel["name"] for channel in objects[101]], ["DCS RD", "DCS WR"])
                self.assertEqual(len(objects[100]["channels"]), 3)  # Original legend stays intact.
                reader.close()
                reader.close()
                freed = [call.args[0] for call in reader.cf.CFRelease.call_args_list]
                self.assertEqual(len(freed), len(set(freed)))
                self.assertEqual(set(freed), {100, 101, 102} | ({actual} if actual else set()) | ({subscription} if subscription else set()))

    def test_binding_and_text_conversion_fail_closed(self):
        reader = self.reader()
        reader._bind_api()
        self.assertEqual(reader.io.IOReportSimpleGetIntegerValue.restype, ctypes.c_int64)
        self.assertEqual(reader.io.IOReportChannelGetUnit.restype, ctypes.c_uint64)
        self.assertEqual(reader.io.IOReportChannelGetFormat.restype, ctypes.c_int)
        native_text = memory._IOReportBandwidthReader._text
        self.assertEqual(native_text(reader, None), "")
        reader.cf.CFStringGetCString.return_value = False
        self.assertEqual(native_text(reader, 10), "")
        def fill(_pointer, target, _size, _encoding):
            target.value = b"DCS RD"
            return True
        reader.cf.CFStringGetCString.side_effect = fill
        self.assertEqual(native_text(reader, 10), "DCS RD")
        with self.assertRaises(OSError):
            reader._own(None)
        reader.cf.CFStringCreateWithCString.return_value = 123
        self.assertEqual(reader._string(b"IOReportChannels"), 123)
        reader.close()


class BandwidthIntegrationTests(fixtures.IsolatedTestCase):
    def test_menu_revisions_cannot_accelerate_native_polling_below_one_second(self):
        clock, revision, sampled = [10.0], [0], []
        def wait(_timeout):
            clock[0] += 0.1
            revision[0] += 1
            return revision[0] == 19
        def read(_interval):
            sampled.append(clock[0])
            return asmond.MemoryBandwidthStats(100, 200, clock[0])
        sampler = mock.Mock(read=mock.Mock(side_effect=read))
        stop = mock.Mock(is_set=lambda: False, wait=mock.Mock(side_effect=wait))
        state = mock.Mock(observe_clock=lambda: 0, snapshot=lambda: (False, False, 1, revision[0]))
        with (mock.patch.object(asmond.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(asmond, "MemoryBandwidthSampler", return_value=sampler),
              mock.patch.object(asmond, "read_memory_stats", return_value=asmond.MemoryStats()),
              mock.patch.object(asmond, "read_charge_stats", return_value=(asmond.BatteryStats(), asmond.UsbCStats())),
              mock.patch.object(asmond, "read_cpu_load_snapshot", side_effect=lambda: asmond.CpuLoadSnapshot((1, 2, 3, 4), clock[0]))):
            asmond.side_metrics_worker(queue.Queue(), stop, state)
        self.assertEqual(len(sampled), 2)
        self.assertGreaterEqual(sampled[1] - sampled[0], 1)

    def test_source_failure_does_not_stop_other_worker_groups(self):
        sampler = mock.Mock()
        sampler.read.side_effect = RuntimeError("fixture bandwidth failure")
        updates = queue.Queue()
        stop = mock.Mock(is_set=lambda: False, wait=mock.Mock(return_value=True))
        with (mock.patch.object(asmond, "MemoryBandwidthSampler", return_value=sampler),
              mock.patch.object(asmond, "read_memory_stats", return_value=asmond.MemoryStats(total_bytes=100)),
              mock.patch.object(asmond, "read_charge_stats", return_value=(asmond.BatteryStats(), asmond.UsbCStats())),
              mock.patch.object(asmond, "read_cpu_load_snapshot", return_value=asmond.CpuLoadSnapshot((1, 2, 3, 4)))):
            asmond.side_metrics_worker(updates, stop, asmond.SideMetricsPollState())
        values = list(updates.queue)
        self.assertTrue(any(update.memory is not None for update in values))
        self.assertTrue(any(update.warnings and update.warnings[0].startswith("bandwidth:") for update in values))
        self.assertEqual(sampler.close.call_count, 2)  # Error path and owner-finally; both idempotent.

    def test_bounded_history_gaps_hover_hold_and_pruning_keep_values_aligned(self):
        history = asmond.History(3, retention_s=10)
        history.hover_enabled = True
        for stamp in range(5):
            history.add_memory_bandwidth(asmond.MemoryBandwidthStats(2e9, 1e9, stamp, "fixture"))
        history.add_memory_bandwidth(asmond.MemoryBandwidthStats(99, 99, 4))
        self.assertEqual(list(history.bandwidth_times), [2, 3, 4])
        frozen = history.snapshot()
        history.add_gap(5)
        self.assertIsNone(history.bandwidth_total[-1])
        self.assertEqual(frozen.bandwidth_total[-1], 3e9)
        history.prune(100)
        self.assertFalse(history.bandwidth_times)
        self.assertFalse(history.bandwidth_total)
        frozen.hover_enabled = True
        window = fixtures.LayoutTests.RecordingWindow()
        self.assertTrue(asmond.draw_memory_bandwidth(window, 2, 3, 75, frozen, fixtures.LayoutTests.dashboard_colors(), graph=True))
        region = frozen.graph_regions[-1]
        hit = asmond.graph_hover_at(frozen, region.rect.x + region.rect.w - 1, 2)
        self.assertEqual((hit.value, hit.received_at, hit.unit), (3e9, 4, "bandwidth"))
        self.assertEqual(asmond.graph_hover_lines(hit, 5), ["1.0s: 3.00 GB/s"])

    def test_bandwidth_expiry_is_independent_of_memory_and_main_power(self):
        sampler = asmond.DashboardSampler(1, True, asmond.History(20))
        state = asmond.SideMetricsState(sampler)
        with mock.patch.object(asmond.time, "monotonic", return_value=100):
            state.apply(asmond.SideMetricsUpdate(memory_bandwidth=asmond.MemoryBandwidthStats(2e9, 1e9, 100),
                                                source_times={"bandwidth": 100}), 1, 0)
        with mock.patch.object(asmond.time, "monotonic", return_value=106):
            state.apply(asmond.SideMetricsUpdate(memory=asmond.MemoryStats(total_bytes=24 * 1024**3),
                                                source_times={"vm": 106}), 1, 0)
            state.expire(1, False, False, 0)
        self.assertIsNone(state.memory_bandwidth.total_bps)
        self.assertEqual(state.memory.total_bytes, 24 * 1024**3)
        self.assertIsNone(sampler.history.bandwidth_total[-1])
        self.assertIn("bandwidth", state.warnings)

    def test_queue_coalescing_keeps_gap_and_rejects_an_earlier_epoch(self):
        old = asmond.SideMetricsUpdate(memory_bandwidth=asmond.MemoryBandwidthStats(1, 2, 1),
                                      source_times={"bandwidth": 1})
        new = asmond.SideMetricsUpdate(memory_bandwidth=asmond.MemoryBandwidthStats(2, 3, 3),
                                      source_times={"bandwidth": 3})
        merged = asmond.merge_side_updates(old, new)
        self.assertEqual(merged.memory_bandwidth.total_bps, 5)
        self.assertEqual(merged.history_gaps["bandwidth"], 1)
        newer_epoch = asmond.SideMetricsUpdate(observation_epoch=1)
        self.assertIs(asmond.merge_side_updates(newer_epoch, old), newer_epoch)

    def test_no_native_library_is_opened_by_mock_worker_or_unsupported_host(self):
        for is_mock in (True, False):
            stop = mock.Mock(is_set=lambda: False, wait=mock.Mock(return_value=True))
            with (mock.patch.object(asmond, "HOST_MACHINE", "x86_64"),
                  mock.patch.object(asmond, "_poll_side_metrics") as poll,
                  mock.patch.object(memory, "_IOReportBandwidthReader") as native):
                asmond.side_metrics_worker(queue.Queue(), stop, asmond.SideMetricsPollState(), is_mock)
                self.assertIsNone(poll.call_args.args[-1].read(1).total_bps)
                native.assert_not_called()

    def test_worker_owns_cleanup_even_when_another_reader_is_interrupted(self):
        sampler = mock.Mock()
        with (mock.patch.object(asmond, "MemoryBandwidthSampler", return_value=sampler),
              mock.patch.object(asmond, "_poll_side_metrics", side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                asmond.side_metrics_worker(queue.Queue(), mock.Mock(), asmond.SideMetricsPollState())
        sampler.close.assert_called_once()

    def test_geometry_preserves_ram_rows_and_complete_numeric_values(self):
        history = asmond.History(20)
        for stamp in range(4):
            history.add_memory_bandwidth(asmond.MemoryBandwidthStats(100e9, 20e9, stamp))
        colors = fixtures.LayoutTests.dashboard_colors()
        for width in (30, 34, 40, 58, 80, 120):
            for height in (7, 9, 11, 13, 14, 15, 17, 25):
                with self.subTest(width=width, height=height):
                    window = fixtures.LayoutTests.RecordingWindow(100, 200)
                    baseline = fixtures.LayoutTests.RecordingWindow(100, 200)
                    args = (2, 3, height, width, asmond.mock_memory_stats(10), None, colors)
                    asmond.draw_memory_section(baseline, *args)
                    asmond.draw_memory_section(window, *args, history=history)
                    for y, x, text in window.entries:
                        self.assertGreaterEqual(y, 2)
                        self.assertLess(y, 2 + height)
                        self.assertGreaterEqual(x, 3)
                        self.assertLessEqual(x + len(text), 3 + width)
                    labels = {name for name, *_ in asmond.memory_detail_value_rows(args[4])}
                    before = {text for _, _, text in baseline.entries if text in labels}
                    after = {text for _, _, text in window.entries if text in labels}
                    self.assertEqual(before, after)
                    self.assertFalse(any("20.00 GB/s" == text for _, _, text in window.entries))

    def test_inspector_reports_source_and_shared_units_without_starting_native_readers(self):
        rows = asmond.data_source_rows(argparse.Namespace(mock=False), None, asmond.MemoryStats(),
                                      asmond.BatteryStats(), asmond.UsbCStats(), asmond.IoStats(), [],
                                      bandwidth=asmond.MemoryBandwidthStats(2e9, 1e9, 10, memory.MEMORY_BANDWIDTH_SOURCE))
        measured = {label: (value, source) for label, value, source in rows}
        self.assertEqual(measured["RAM bandwidth total"], ("3.00 GB/s", memory.MEMORY_BANDWIDTH_SOURCE))

    def test_unsupported_history_is_hidden_but_verified_zero_and_later_gaps_remain_visible(self):
        history = asmond.History(20)
        colors = fixtures.LayoutTests.dashboard_colors()
        win = fixtures.LayoutTests.RecordingWindow()
        history.add_memory_bandwidth(asmond.MemoryBandwidthStats(sampled_at=1))
        self.assertFalse(asmond.draw_memory_bandwidth(win, 1, 1, 70, history, colors, graph=True))
        self.assertFalse(win.entries)
        history.add_memory_bandwidth(asmond.MemoryBandwidthStats(0, 0, 2))
        history.hover_enabled = True
        self.assertTrue(asmond.draw_memory_bandwidth(win, 1, 1, 70, history, colors, graph=True))
        self.assertIn("0.00 GB/s", [text for _, _, text in win.entries])
        history.add_memory_bandwidth(asmond.MemoryBandwidthStats(sampled_at=3))
        win.entries.clear()
        self.assertTrue(asmond.draw_memory_bandwidth(win, 1, 1, 70, history, colors, graph=True))
        self.assertIn("n/a", [text for _, _, text in win.entries])
