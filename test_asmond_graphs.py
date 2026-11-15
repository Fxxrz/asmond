from __future__ import annotations

import contextlib
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asmond
import asmond_settings
import test_asmond as fixtures


class GraphHistoryTests(unittest.TestCase):
    def test_timestamps_do_not_turn_one_cluster_into_a_whole_cpu_reading(self):
        history = asmond.History(10)
        history.add(asmond.MetricSample(p_usage_pct=40), 1)
        history.add(asmond.MetricSample(p_usage_pct=60, e_usage_pct=20), 2)
        self.assertEqual(list(history.cpu_usage), [None, 40])

    def test_duration_pruning_keeps_series_aligned_across_missing_and_new_cores(self):
        history = asmond.History(100, retention_s=10)
        history.add(asmond.MetricSample(cpu_power_mw=100, cores=[asmond.CoreMetric('P0', 5)]), now=1)
        history.add(asmond.MetricSample(cpu_power_mw=200, cores=[asmond.CoreMetric('E0', 10)]), now=5)
        history.add(asmond.MetricSample(cpu_power_mw=300, cores=[asmond.CoreMetric('P0', 20)]), now=12)
        self.assertEqual(list(history.power_times), [5, 12])
        self.assertEqual(list(history.core_usage['P0']), [None, 20])
        self.assertEqual(list(history.core_usage['E0']), [10, None])
        self.assertEqual(list(history.cpu_power), [200, 300])
        history.prune(23)
        for timestamps, values in history.timestamp_groups():
            self.assertFalse(timestamps)
            self.assertTrue(all(not series for series in values))

    def test_retained_values_survive_window_growth_and_larger_retention(self):
        history = asmond.History(1)
        history.configure_retention(10, 0)
        for index in range(700):
            history.add(asmond.MetricSample(cpu_power_mw=index), now=float(index))
        self.assertEqual(history.power_times[0], 99)
        history.hover_enabled = True
        history.register_graph(asmond.Rect(0, 0, 5, 10), 'CPU', history.cpu_power, history.power_times, 'power')
        self.assertEqual(history.graph_regions[-1].columns[0][0].received_at, 680)
        history.register_graph(asmond.Rect(0, 0, 5, 200), 'CPU', history.cpu_power, history.power_times, 'power')
        self.assertEqual(history.graph_regions[-1].columns[0][0].received_at, 300)
        history.configure_retention(30, 699)
        self.assertEqual(history.power_times[0], 99)  # Discarded points do not reappear.
        history.configure_retention(5, 699)
        self.assertEqual(history.power_times[0], 399)
        self.assertLessEqual(history.length, 3002)

    def test_duration_and_explicit_point_cap_both_bound_every_series(self):
        history = asmond.History(1)
        history.point_limit = 20
        history.configure_retention(30, 0)
        for index in range(50):
            stamp = index / 10
            history.add(asmond.MetricSample(cpu_power_mw=index), stamp)
            history.add_io(asmond.IoStats(net_in_bps=index), stamp)
            history.add_charge(asmond.BatteryStats(power_mw=-100), asmond.UsbCStats(), stamp)
        self.assertEqual(len(history.cpu_power), 20)
        self.assertEqual(len(history.net_in_io), 20)
        self.assertEqual(len(history.charge_samples), 20)
        self.assertEqual(history.length, 20)
        history.prune(2000)
        self.assertFalse(history.charge_samples)
        self.assertFalse(history.io_times)

    def test_core_padding_remains_aligned_when_capacity_wraps(self):
        history = asmond.History(3)
        for index in range(8):
            cores = [asmond.CoreMetric('P0', index)]
            if index == 6:
                cores.append(asmond.CoreMetric('E0', 42))
            history.add(asmond.MetricSample(cores=cores), float(index))
        self.assertEqual(list(history.sample_times), [5, 6, 7])
        self.assertEqual(list(history.core_usage['E0']), [None, 42, None])
        history.add_gap(8)
        self.assertEqual(list(history.core_usage['E0']), [42, None, None])
        self.assertEqual(len(history.core_usage['P0']), len(history.sample_times))

    def test_independent_cpu_and_io_timestamps_are_not_relabelled_by_power(self):
        history = asmond.History(10)
        history.add(asmond.MetricSample(cpu_usage_pct=90, cpu_usage_source='mach', gpu_usage_pct=5), 100)
        history.add_cpu_usage(7, 99)
        history.add_io(asmond.IoStats(net_in_bps=123), 98)
        self.assertEqual(list(history.cpu_times), [99])
        self.assertEqual(list(history.cpu_usage), [7])
        self.assertEqual(list(history.sample_times), [100])
        self.assertEqual(list(history.io_times), [98])
        history.clear_power()
        self.assertEqual(list(history.cpu_times), [99])
        self.assertEqual(list(history.sample_times), [100])
        history.add(asmond.MetricSample(cpu_usage_pct=8), 101)
        self.assertEqual(list(history.cpu_times), [99, 101])
        self.assertEqual(list(history.power_times), [101])

    def test_duplicate_or_old_samples_cannot_reverse_time(self):
        history = asmond.History(10)
        for stamp in (10, 10, 9):
            history.add(asmond.MetricSample(cpu_power_mw=123), stamp)
            history.add_io(asmond.IoStats(), stamp)
            history.add_cpu_usage(0, stamp)
        self.assertEqual(list(history.sample_times), [10])
        self.assertEqual(list(history.cpu_times), [10])
        self.assertEqual(list(history.io_times), [10])

    def test_clock_gap_never_gives_old_points_an_unverified_age(self):
        history = asmond.History(10)
        history.add(asmond.MetricSample(cpu_power_mw=123), 10)
        history.add_gap(11)
        history.hover_enabled = True
        history.register_graph(asmond.Rect(0, 0, 1, 1), 'CPU', history.cpu_power, history.power_times, 'power')
        selection = asmond.graph_hover_at(history, 0, 0)
        self.assertEqual(selection.received_at, 10)
        self.assertEqual(asmond.graph_hover_lines(selection, 12), ['before clock gap: 123 mW'])
        history.clear_power()
        self.assertEqual(history.clock_gap_at, 11)

    def test_unexpected_core_labels_cannot_grow_memory_without_bound(self):
        history = asmond.History(10)
        history.add(asmond.MetricSample(cores=[asmond.CoreMetric(str(index), 1) for index in range(300)]), 10)
        self.assertEqual(len(history.core_usage), 256)
        self.assertTrue(all(len(values) == 1 for values in history.core_usage.values()))


class GraphHoverTests(unittest.TestCase):
    def test_two_braille_samples_include_real_zero_and_missing_value(self):
        history = asmond.History(10)
        history.hover_enabled = True
        history.register_graph(asmond.Rect(2, 4, 3, 2), 'CPU', [123, 0, None], [10, 11, 12], 'power')
        selection = asmond.graph_hover_at(history, 5, 3)
        self.assertEqual(selection.value, 0)
        self.assertEqual(selection.received_at, 11)
        self.assertEqual(asmond.graph_hover_lines(selection, 13), ['2.0s: 0 mW'])
        self.assertIsNone(asmond.graph_hover_at(history, 3, 3))
        self.assertIsNone(asmond.graph_hover_at(history, 5, 5))

    def test_padding_is_not_presented_as_a_measurement_and_mismatch_is_rejected(self):
        history = asmond.History(10)
        history.hover_enabled = True
        history.register_graph(asmond.Rect(0, 0, 2, 5), 'CPU', [3], [5], 'percent')
        self.assertIsNone(asmond.graph_hover_at(history, 0, 0))
        self.assertEqual(asmond.graph_hover_at(history, 4, 0).value, 3)
        history.register_graph(asmond.Rect(0, 0, 2, 5), 'GPU', [3, 4], [5], 'percent')
        self.assertEqual(len(history.graph_regions), 1)

    def test_dense_charge_cell_selects_one_actual_source_point_by_proximity(self):
        points = [asmond.GraphPoint(10, (0, -3), row_spans=((3, 3), (14, 14))),
                  asmond.GraphPoint(11, (4, None), row_spans=((1, 1), None)),
                  asmond.GraphPoint(12, (2, -1), row_spans=((2, 2), (12, 12)))]
        history = asmond.History(10)
        history.graph_regions = [asmond.GraphRegion(asmond.Rect(0, 0, 20, 1), ('Input', 'Battery'), 'watts', [points])]
        for row, label, value, timestamp in ((1, 'Input', 4, 11), (2, 'Input', 2, 12),
                (3, 'Input', 0, 10), (13, 'Battery', -1, 12), (14, 'Battery', -3, 10)):
            with self.subTest(row=row):
                selection = asmond.graph_hover_at(history, 0, row)
                self.assertEqual((selection.label, selection.value, selection.received_at), (label, value, timestamp))
                self.assertEqual(len(asmond.graph_hover_lines(selection, 15)), 1)
        self.assertIsNone(asmond.graph_hover_at(history, 0, 7))
        self.assertIsNone(asmond.graph_hover_at(history, 0, 19))

    def test_low_cpu_gpu_load_only_responds_within_two_rows_of_the_bars(self):
        history = asmond.History(10)
        history.hover_enabled = True
        sample = asmond.MetricSample(cpu_usage_pct=2, gpu_usage_pct=3)
        history.add(sample, 10)
        window = fixtures.LayoutTests.RecordingWindow()
        asmond.draw_avg_load_graph(window, 2, 4, 25, 50, sample, history, fixtures.LayoutTests.dashboard_colors())
        for row, label, value in ((13, 'CPU avg', 2), (11, 'CPU avg', 2),
                                  (15, 'GPU avg', 3), (17, 'GPU avg', 3)):
            selection = asmond.graph_hover_at(history, 53, row)
            self.assertEqual((selection.label, selection.value), (label, value))
        for row in (2, 3, 10, 14, 18, 26):
            self.assertIsNone(asmond.graph_hover_at(history, 53, row), row)

    def test_hover_keeps_the_real_peak_and_prefers_newest_when_both_bars_are_hit(self):
        history = asmond.History(10)
        history.hover_enabled = True
        history.add(asmond.MetricSample(cpu_usage_pct=80), 10)
        sample = asmond.MetricSample(cpu_usage_pct=2)
        history.add(sample, 11)
        asmond.draw_avg_load_graph(fixtures.LayoutTests.RecordingWindow(), 2, 4, 25, 50,
                                  sample, history, fixtures.LayoutTests.dashboard_colors())
        peak = asmond.graph_hover_at(history, 53, 5)
        self.assertEqual((peak.value, peak.received_at), (80, 10))
        recent = asmond.graph_hover_at(history, 53, 13)
        self.assertEqual((recent.value, recent.received_at), (2, 11))
        self.assertEqual(asmond.graph_hover_lines(recent, 20), ['9.0s: 2.0%'])

    def test_missing_points_do_not_become_zero_but_real_zero_remains_selectable(self):
        history = asmond.History(10)
        history.hover_enabled = True
        sample = asmond.MetricSample(cpu_usage_pct=0)
        history.add(sample, 10)
        asmond.draw_avg_load_graph(fixtures.LayoutTests.RecordingWindow(), 2, 4, 25, 50,
                                  sample, history, fixtures.LayoutTests.dashboard_colors())
        self.assertEqual(asmond.graph_hover_at(history, 53, 13).value, 0)
        self.assertIsNone(asmond.graph_hover_at(history, 53, 10))
        self.assertIsNone(asmond.graph_hover_at(history, 53, 15))

    def test_power_and_io_use_the_heights_of_the_bars_they_really_draw(self):
        colors = fixtures.LayoutTests.dashboard_colors()
        for kind in ('power', 'io'):
            with self.subTest(kind=kind):
                history = asmond.History(10)
                history.hover_enabled = True
                window = fixtures.LayoutTests.RecordingWindow()
                for stamp, value in ((10, 100000), (11, 1000), (12, 1000)):
                    if kind == 'power':
                        history.add(asmond.MetricSample(cpu_power_mw=value), stamp)
                    else:
                        history.add_io(asmond.IoStats(net_in_bps=value), stamp)
                if kind == 'power':
                    asmond.draw_split_power_graph(window, 2, 4, 25, 50, history,
                        asmond.MetricSample(cpu_power_mw=1000), 'cpu', 'gpu', colors)
                    x, bar_y = 51, 13
                else:
                    asmond.draw_io_mini_graph(window, 2, 4, 25, 50, asmond.IoStats(net_in_bps=1000),
                        history, colors, 'net_in', 'disk_write')
                    x, bar_y = 53, 14
                self.assertEqual(asmond.graph_hover_at(history, x, bar_y).value, 1000)
                self.assertEqual(asmond.graph_hover_at(history, x, bar_y - 2).value, 1000)
                self.assertIsNone(asmond.graph_hover_at(history, x, bar_y - 3))
                self.assertEqual(asmond.graph_hover_at(history, x - 1, 3).value, 100000)

    def test_charging_renderer_registers_only_the_real_input_and_battery_dot_rows(self):
        history = asmond.History(10)
        history.hover_enabled = True
        history.add_charge(asmond.BatteryStats(power_mw=-5000), asmond.UsbCStats(system_power_w=20), 10)
        history.add_charge(asmond.BatteryStats(power_mw=-5000), asmond.UsbCStats(system_power_w=20), 11)
        asmond.draw_charging_graph(fixtures.LayoutTests.RecordingWindow(), asmond.Rect(2, 3, 18, 44),
                                   history, fixtures.LayoutTests.dashboard_colors())
        top = asmond.graph_hover_at(history, 44, 4)
        bottom = asmond.graph_hover_at(history, 44, 17)
        self.assertEqual((top.label, top.value), ('Input', 20))
        self.assertEqual((bottom.label, bottom.value), ('Battery', -5))
        self.assertIsNone(asmond.graph_hover_at(history, 44, 10))

    def test_tooltip_stays_within_terminal_at_every_edge(self):
        colors = fixtures.LayoutTests.dashboard_colors()
        for x, y in ((0, 0), (79, 0), (0, 23), (79, 23), (40, 12)):
            with self.subTest(x=x, y=y):
                win = fixtures.LayoutTests.RecordingWindow(24, 80)
                history = asmond.History(2)
                history.hover_enabled = True
                history.register_graph(asmond.Rect(y, x, 1, 1), 'CPU', [1234, 0], [10, 11], 'power')
                with mock.patch.object(asmond, 'draw_box'), mock.patch.object(asmond.time, 'monotonic', return_value=12):
                    asmond.draw_graph_hover(win, history, (x, y), colors)
                self.assertTrue(win.entries)
                for row, col, text in win.entries:
                    self.assertGreaterEqual(row, 0)
                    self.assertGreaterEqual(col, 0)
                    self.assertLess(row, 24)
                    self.assertLessEqual(col + len(text), 80)

    def test_every_dashboard_and_modal_uses_current_rendered_geometry(self):
        fixture = fixtures.LayoutTests()
        for layout in ('full', 'compact', 'focus', 'custom'):
            for load_view in ('rows', 'graph'):
                for graph_view in ('power', 'charging'):
                    with self.subTest(layout=layout, load_view=load_view, graph_view=graph_view):
                        args = fixture.dashboard_args(layout)
                        args.graph_view = graph_view
                        data = fixture.dashboard_data()
                        sample, history, memory, battery, usb, io, processes = data
                        for menu in (False, True, False):
                            window = fixture.RecordingWindow(61, 180)
                            with mock.patch.object(asmond, 'draw_box'):
                                asmond.draw_dashboard(window, sample, history, memory, battery, usb, io, processes,
                                    fixture.dashboard_colors(), args, '', 'soc', 'cpu', 'net_in', 'net_out', load_view,
                                    'hidden', 'cpu', 'battery', 0, None, (), False, menu, 0, False, False, 0, '', False)
                            if menu:
                                self.assertEqual(history.graph_regions, [])
                            else:
                                self.assertTrue(history.graph_regions)
                            for region in history.graph_regions:
                                self.assertGreaterEqual(region.rect.x, 0)
                                self.assertLessEqual(region.rect.x + region.rect.w, 180)
                                self.assertGreaterEqual(region.rect.y, 0)
                                self.assertLessEqual(region.rect.y + region.rect.h, 61)

    def test_scale_ignores_old_peak_outside_the_visible_power_graph(self):
        history = asmond.History(500)
        for index in range(300):
            history.add(asmond.MetricSample(cpu_power_mw=100000 if index == 0 else 1000), float(index))
        win = fixtures.LayoutTests.RecordingWindow()
        with mock.patch.object(asmond, 'draw_box'):
            asmond.draw_split_power_graph(win, 0, 0, 10, 40, history, asmond.MetricSample(), 'cpu', 'cpu', fixtures.LayoutTests.dashboard_colors())
        self.assertIn('scale 1.00 W', win.rendered())
        self.assertNotIn('scale 100', win.rendered())


class GraphMouseTests(unittest.TestCase):
    def test_pending_escape_preserves_a_following_escape_or_arrow(self):
        for following in (27, asmond.curses.KEY_UP):
            with self.subTest(following=following):
                mouse = asmond.GraphMouse()
                mouse.sgr_protocol = True
                window = mock.Mock()
                with mock.patch.object(asmond.time, 'monotonic', return_value=100):
                    window.getch.side_effect = [27, -1, -1]
                    self.assertEqual(mouse.read_key(window), -1)
                with mock.patch.object(asmond.time, 'monotonic', return_value=100.08):
                    window.getch.side_effect = [following]
                    self.assertEqual(mouse.read_key(window), 27)
                    if following != 27:
                        self.assertEqual(mouse.read_key(window), following)
                    else:
                        window.getch.side_effect = [-1, -1]
                        self.assertEqual(mouse.read_key(window), -1)
                if following == 27:
                    with mock.patch.object(asmond.time, 'monotonic', return_value=100.19):
                        window.getch.side_effect = [-1]
                        self.assertEqual(mouse.read_key(window), 27)

    def test_disabled_tracking_consumes_an_in_flight_fragment_without_changing_focus(self):
        mouse = asmond.GraphMouse()
        mouse.sgr_protocol = True
        window = mock.Mock()
        with mock.patch.object(asmond.time, 'monotonic', return_value=100):
            window.getch.side_effect = [27, -1, -1]
            self.assertEqual(mouse.read_key(window), -1)
        with mock.patch.object(asmond.time, 'monotonic', return_value=100.08):
            window.getch.side_effect = [*map(ord, '[<35;301;50M'), -1]
            self.assertEqual(mouse.read_key(window), -1)
        self.assertIsNone(mouse.position)
        with mock.patch.object(asmond.time, 'monotonic', return_value=101):
            window.getch.side_effect = [27, -1, -1]
            self.assertEqual(mouse.read_key(window), -1)
        with mock.patch.object(asmond.time, 'monotonic', return_value=101.11):
            window.getch.side_effect = [-1]
            self.assertEqual(mouse.read_key(window), 27)

    def test_steady_motion_does_not_wait_for_the_next_event_before_rendering(self):
        class Window:
            timeout_ms = 200
            reads = 0

            def timeout(self, value):
                self.timeout_ms = value

            def getch(self):
                self.reads += 1
                if self.reads == 1:
                    return asmond.curses.KEY_MOUSE
                if self.timeout_ms != 0:
                    raise AssertionError('a second blocking read would stall the dashboard')
                return -1

        window = Window()
        mouse = asmond.GraphMouse()
        mouse.enabled = True
        with mock.patch.object(asmond.curses, 'getmouse', return_value=(0, 9, 8, 0, 0)):
            self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (9, 8))
        self.assertEqual(window.timeout_ms, 200)

    def test_mouse_prefix_split_after_escape_does_not_quit(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        with mock.patch.object(asmond.time, 'monotonic', return_value=100):
            window.getch.side_effect = [27, -1, -1]
            self.assertEqual(mouse.read_key(window), -1)
        with mock.patch.object(asmond.time, 'monotonic', return_value=100.08):
            window.getch.side_effect = [*map(ord, '[<35;301;50M'), -1]
            self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (300, 49))

    def test_slow_report_keeps_its_suffix_out_of_keyboard_commands(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        with mock.patch.object(asmond.time, 'monotonic', return_value=100):
            window.getch.side_effect = [*map(ord, '\x1b[<35;30'), -1]
            self.assertEqual(mouse.read_key(window), -1)
        with mock.patch.object(asmond.time, 'monotonic', return_value=100.35):
            window.getch.side_effect = [-1]
            self.assertEqual(mouse.read_key(window), -1)
            window.getch.side_effect = [*map(ord, '1;50M'), -1]
            self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (300, 49))

    def test_keyboard_and_escape_can_cancel_a_truncated_report(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        for command in (ord('q'), ord('h'), asmond.curses.KEY_UP):
            window.getch.side_effect = [*map(ord, '\x1b[<35;30'), -1]
            self.assertEqual(mouse.read_key(window), -1)
            window.getch.side_effect = [command]
            self.assertEqual(mouse.read_key(window), command)
            self.assertEqual(mouse.report, '')
        with mock.patch.object(asmond.time, 'monotonic', return_value=100):
            window.getch.side_effect = [27, -1, -1]
            self.assertEqual(mouse.read_key(window), -1)
        with mock.patch.object(asmond.time, 'monotonic', return_value=100.11):
            window.getch.side_effect = [-1]
            self.assertEqual(mouse.read_key(window), 27)

    def test_legacy_coordinate_limit_is_not_treated_as_a_verified_position(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = True
        window = mock.Mock()
        with mock.patch.object(asmond.curses, 'getmouse', return_value=(0, 222, 5, 0, 0)):
            window.getch.side_effect = [asmond.curses.KEY_MOUSE, -1]
            mouse.read_key(window)
            self.assertIsNone(mouse.position)
            mouse.native_sgr = True
            window.getch.side_effect = [asmond.curses.KEY_MOUSE, -1]
            mouse.read_key(window)
            self.assertEqual(mouse.position, (222, 5))

    def test_tracking_controls_are_written_and_flushed_immediately(self):
        with mock.patch.object(asmond.sys, 'stdout') as output:
            asmond.GraphMouse.write_tracking(True)
            self.assertEqual(output.mock_calls, [mock.call.write('\x1b[?1003h\x1b[?1006h'), mock.call.flush()])

    def test_sgr_motion_supports_wide_windows_without_legacy_coordinate_wrap(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        window.getch.side_effect = [*map(ord, '\x1b[<35;301;50M'), -1]
        self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (300, 49))
        window.getch.side_effect = [*map(ord, '\x1b[<64;1;1M'), ord('q')]
        self.assertEqual(mouse.read_key(window), ord('q'))
        self.assertIsNone(mouse.position)

    def test_sgr_fragmented_report_is_buffered_and_plain_escape_keeps_following_key(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        window.getch.side_effect = [*map(ord, '\x1b[<35;30'), -1]
        self.assertEqual(mouse.read_key(window), -1)
        self.assertIsNone(mouse.position)
        window.getch.side_effect = [*map(ord, '1;50M'), -1]
        self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (300, 49))
        window.getch.side_effect = [27, ord('q')]
        self.assertEqual(mouse.read_key(window), 27)
        self.assertEqual(mouse.read_key(window), ord('q'))
        window.timeout.assert_any_call(0)
        window.timeout.assert_any_call(200)

    def test_sgr_flood_and_malformed_coordinates_never_become_dashboard_shortcuts(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = mouse.sgr_protocol = True
        window = mock.Mock()
        report = '\x1b[<35;' + '1' * 120 + ';50M'
        window.getch.side_effect = [*map(ord, report * 64), ord('q')]
        for _ in range(5):
            key = mouse.read_key(window)
            if key != -1:
                break
        self.assertEqual(key, ord('q'))
        self.assertIsNone(mouse.position)

    def test_motion_is_coalesced_without_eating_the_next_keyboard_event(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = True
        window = mock.Mock()
        window.getch.side_effect = [asmond.curses.KEY_MOUSE, asmond.curses.KEY_MOUSE, -1]
        with mock.patch.object(asmond.curses, 'getmouse', side_effect=[(0, 2, 3, 0, 0), (0, 4, 5, 0, 0)]):
            self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(mouse.position, (4, 5))
        window.getch.side_effect = [asmond.curses.KEY_MOUSE, ord('m')]
        with mock.patch.object(asmond.curses, 'getmouse', return_value=(0, 2, 3, 0, 0)):
            self.assertEqual(mouse.read_key(window), ord('m'))
        self.assertIsNone(mouse.position)

    def test_flood_is_bounded_and_wheel_does_not_select_another_graph(self):
        mouse = asmond.GraphMouse()
        mouse.enabled = True
        window = mock.Mock()
        window.getch.return_value = asmond.curses.KEY_MOUSE
        with mock.patch.object(asmond.curses, 'getmouse', return_value=(0, 9, 8, 0, 0)):
            self.assertEqual(mouse.read_key(window), -1)
        self.assertEqual(window.getch.call_count, 64)
        window.getch.side_effect = [asmond.curses.KEY_MOUSE, -1]
        with mock.patch.object(asmond.curses, 'getmouse', return_value=(0, 1, 2, 0, asmond.curses.BUTTON4_PRESSED)):
            mouse.read_key(window)
        self.assertEqual(mouse.position, (9, 8))

    def test_original_mouse_mask_is_restored_and_unavailable_terminal_is_safe(self):
        mouse = asmond.GraphMouse()
        with mock.patch.object(asmond.curses, 'tigetstr', return_value=b'\x1b[M'), mock.patch.object(asmond.GraphMouse, 'write_tracking'), mock.patch.object(asmond.curses, 'mousemask', side_effect=[(123, 42), (0, 123), (42, 0)]) as mask:
            mouse.configure(True)
            self.assertTrue(mouse.enabled)
            mouse.configure(False)
            mouse.restore()
            self.assertEqual(mask.call_args.args, (42,))
        with mock.patch.object(asmond.curses, 'mousemask', side_effect=asmond.curses.error):
            mouse = asmond.GraphMouse()
            mouse.configure(True)
            self.assertFalse(mouse.enabled)
            self.assertTrue(mouse.unavailable)
            self.assertEqual(mouse.read_key(mock.Mock(getch=lambda: ord('q'))), ord('q'))

    def test_stale_queued_sample_does_not_become_a_fresh_live_value(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        sample = asmond.MetricSample(cpu_power_mw=1000, received_at=asmond.time.monotonic() - 20)
        result = runner.run_process_loop([{'sample': sample, 'key': ord('q')}], with_sample=False)
        self.assertEqual(result['power_observations'][0][0], None)
        self.assertEqual(result['power_observations'][0][1], [sample.received_at])

    def test_menu_changes_hover_and_retention_without_new_shortcuts(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        items = [row[0] for row in asmond.MENU_ITEMS]
        frames = [{'key': ord('m')}]
        frames += [{'key': asmond.curses.KEY_DOWN}] * items.index('graph_hover')
        frames += [{'key': asmond.curses.KEY_RIGHT}, {'key': asmond.curses.KEY_DOWN}, {'key': asmond.curses.KEY_RIGHT}]
        frames += [{'key': 27}, {'key': ord('q')}]
        result = runner.run_process_loop(frames)
        self.assertFalse(result['args'].graph_hover)
        self.assertEqual(result['args'].graph_history_minutes, 30)


class GraphWorkerTimestampTests(unittest.TestCase):
    def test_failed_baseline_and_recovered_reads_are_stamped_at_the_worker(self):
        now = [100.0]
        stop = mock.Mock()
        stop.is_set.return_value = False

        def advance(_timeout):
            now[0] += 1
            return now[0] >= 103

        stop.wait.side_effect = advance
        updates = queue.Queue()
        state = asmond.SideMetricsPollState(poll_io=True, sample_interval_s=1)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(asmond.time, 'monotonic', side_effect=lambda: now[0]))
            stack.enter_context(mock.patch.object(asmond.SideMetricsPollState, 'observe_clock', return_value=0))
            stack.enter_context(mock.patch.object(asmond, 'read_memory_stats', return_value=asmond.MemoryStats()))
            stack.enter_context(mock.patch.object(asmond, 'read_charge_stats', return_value=(asmond.BatteryStats(), asmond.UsbCStats())))
            stack.enter_context(mock.patch.object(asmond, 'read_cpu_load_snapshot', side_effect=[
                OSError('unavailable'), asmond.CpuLoadSnapshot((10, 0, 10, 0), 101),
                asmond.CpuLoadSnapshot((10, 0, 20, 0), 102),
            ]))
            stack.enter_context(mock.patch.object(asmond, 'read_io_snapshot', side_effect=[
                OSError('unavailable'), asmond.IoSnapshot(timestamp=101), asmond.IoSnapshot(timestamp=102),
            ]))
            asmond.side_metrics_worker(updates, stop, state)
        cpu = [update for update in updates.queue if 'cpu' in update.recovered or any(w.startswith('cpu:') for w in update.warnings)]
        io = [update for update in updates.queue if update.io_stats is not None]
        self.assertEqual([u.cpu_sampled_at for u in cpu], [100, 101, 102])
        self.assertEqual([u.cpu_usage_pct for u in cpu], [None, None, 0])
        self.assertEqual([u.io_sampled_at for u in io], [100, 101, 102])
        self.assertEqual([u.io_stats.net_in_bps for u in io], [None, None, 0])

    def test_one_queue_drain_preserves_failure_baseline_and_real_zero_recovery(self):
        for source in ('cpu', 'io'):
            with self.subTest(source=source):
                histories = []
                original_history = asmond.History

                class CapturedHistory(original_history):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        histories.append(self)

                now = asmond.time.monotonic()
                if source == 'cpu':
                    first = asmond.SideMetricsUpdate(cpu_usage_pct=80, cpu_sampled_at=now - 4, recovered=['cpu'])
                    failure = asmond.SideMetricsUpdate(cpu_sampled_at=now - 3, warnings=['cpu:OSError'])
                    baseline = asmond.SideMetricsUpdate(cpu_sampled_at=now - 2, recovered=['cpu'])
                    recovery = asmond.SideMetricsUpdate(cpu_usage_pct=0, cpu_sampled_at=now - 1, recovered=['cpu'])
                else:
                    first = asmond.SideMetricsUpdate(io_stats=asmond.IoStats(net_in_bps=123), io_sampled_at=now - 4)
                    failure = asmond.SideMetricsUpdate(io_stats=asmond.IoStats(), io_sampled_at=now - 3, warnings=['io:OSError'])
                    baseline = asmond.SideMetricsUpdate(io_stats=asmond.IoStats(), io_sampled_at=now - 2, recovered=['io'])
                    recovery = asmond.SideMetricsUpdate(io_stats=asmond.IoStats(net_in_bps=0), io_sampled_at=now - 1, recovered=['io'])
                original_put = asmond.queue.Queue.put

                def enqueue(self, item, *args, **kwargs):
                    original_put(self, item, *args, **kwargs)
                    if item is failure:
                        original_put(self, baseline, *args, **kwargs)
                        original_put(self, recovery, *args, **kwargs)

                sample = asmond.MetricSample(telemetry_source='intel', cpu_usage_pct=24,
                    cpu_usage_source='powermetrics', cpu_power_mw=1000, received_at=now - 4.5)
                with mock.patch.object(asmond, 'History', CapturedHistory), mock.patch.object(asmond.queue.Queue, 'put', enqueue):
                    fixtures.ProcessRunLoopSafetyTests().run_process_loop([
                        {'sample': sample, 'update': first}, {'update': failure, 'key': ord('q')},
                    ], with_sample=False)
                history = histories[0]
                values = list(history.cpu_usage if source == 'cpu' else history.net_in_io)
                timestamps = list(history.cpu_times if source == 'cpu' else history.io_times)
                self.assertEqual(values, [24, 80, None, 0] if source == 'cpu' else [123, None, None, 0])
                self.assertEqual(timestamps[-1], now - 1)
                self.assertTrue(all(a < b for a, b in zip(timestamps, timestamps[1:])))


class GraphSettingsTests(unittest.TestCase):
    def test_strict_settings_round_trip_and_cli_duration(self):
        args = asmond.build_parser().parse_args(['--mock', '--history-minutes', '5'])
        args.graph_hover = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            self.assertIsNone(asmond_settings.save_settings(path, args, asmond.settings_schema()))
            loaded = asmond_settings.load_settings(path, asmond.settings_schema())
            self.assertEqual(loaded['graph_history_minutes'], 5)
            self.assertIs(loaded['graph_hover'], False)
            for value in (True, '10', 10.0, 0, 18000, None):
                path.write_text(json.dumps({'graph_hover': 'yes', 'graph_history_minutes': value}))
                loaded = asmond_settings.load_settings(path, asmond.settings_schema())
                self.assertNotIn('graph_history_minutes', loaded)
                self.assertNotIn('graph_hover', loaded)
        defaults = asmond.build_parser().parse_args([])
        self.assertEqual(defaults.graph_history_minutes, 10)
        self.assertIsNone(defaults.history)


if __name__ == '__main__':
    unittest.main()
