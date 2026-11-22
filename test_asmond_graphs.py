from __future__ import annotations

import contextlib
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_asmond as fixtures
import asmond
import asmond_settings


class GraphHistoryTests(fixtures.IsolatedTestCase):
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


class GraphHoldTests(fixtures.IsolatedTestCase):
    def test_snapshot_survives_live_updates_pruning_and_reset_without_sharing_containers(self):
        history = asmond.History(4, retention_s=10)
        history.add(asmond.MetricSample(cpu_power_mw=123.4, cores=[asmond.CoreMetric('P0', 5)]), 1)
        history.add_io(asmond.IoStats(net_in_bps=2), 1)
        frozen = history.snapshot()
        self.assertIs(frozen.cpu_power[0], history.cpu_power[0])
        self.assertIsNot(frozen.core_usage['P0'], history.core_usage['P0'])
        for stamp in range(2, 30):
            history.add(asmond.MetricSample(cpu_power_mw=999), stamp)
        history.clear_power()
        self.assertEqual(list(frozen.cpu_power), [123.4])
        self.assertEqual(list(frozen.core_usage['P0']), [5])
        self.assertEqual(list(frozen.net_in_io), [2])
        self.assertLessEqual(len(history.sample_times), 4)
        self.assertEqual(frozen.cpu_power.maxlen, 4)
        self.assertFalse(frozen.graph_regions)

    def test_hold_keeps_collecting_and_resume_selects_current_graph_data(self):
        new = asmond.MetricSample(cpu_power_mw=7654)
        result = fixtures.ProcessRunLoopSafetyTests().run_process_loop([
            {'key': ord('h')}, {'sample': new}, {'key': ord('h')}, {'key': ord('q')},
        ])
        views = result['views']
        self.assertEqual([v['held'] for v in views], [False, True, True, False])
        self.assertNotEqual(views[1]['graph_power'], views[1]['live_power'])
        self.assertEqual(views[1]['graph_power'], views[2]['graph_power'])
        self.assertEqual(views[3]['graph_power'], views[3]['live_power'])

    def test_waiting_help_and_reset_hold_rules(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        result = runner.run_process_loop([{'key': ord('h')}, {'key': ord('q')}], with_sample=False)
        self.assertFalse(any(v['held'] for v in result['views']))
        result = runner.run_process_loop([
            {'key': ord('?')}, {'key': ord('h')}, {'key': 27},
            {'key': ord('h')}, {'key': ord('r')}, {'key': ord('q')},
        ])
        self.assertEqual([v['held'] for v in result['views']], [False, False, False, False, True, False])

    def test_clock_gap_releases_held_graphs(self):
        result = fixtures.ProcessRunLoopSafetyTests().run_process_loop([
            {'key': ord('h')}, {}, {'wall_jump': 120}, {'key': ord('q')},
        ])
        self.assertTrue(result['views'][1]['held'])
        self.assertFalse(result['views'][2]['held'])

    def test_menu_hold_and_retention_do_not_persist_a_snapshot(self):
        hold_index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == 'graph_hold')
        retention_index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == 'graph_history_minutes')
        keys = [ord('m')] + [asmond.curses.KEY_DOWN] * hold_index + [10]
        keys += [asmond.curses.KEY_DOWN] * (retention_index - hold_index) + [asmond.curses.KEY_LEFT, 27, ord('q')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            result = fixtures.ProcessRunLoopSafetyTests().run_process_loop([{'key': key} for key in keys], persist_path=path)
            self.assertTrue(result['views'][-1]['held'])
            self.assertIsNone(result['args'].held_graphs)  # Released on return as well as on process exit.
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted['graph_history_minutes'], 5)
            self.assertNotIn('held_graphs', persisted)

    def test_snapshot_retention_reduction_and_resize_use_held_observation_window(self):
        history = asmond.History(1)
        history.configure_retention(30, 0)
        for stamp in range(1000):
            history.add(asmond.MetricSample(cpu_power_mw=stamp), stamp)
        frozen = history.snapshot()
        frozen.configure_retention(5, 999)
        self.assertEqual(frozen.power_times[0], 699)
        self.assertEqual(len(history.power_times), 1000)
        frozen.hover_enabled = True
        for width in (2, 100):
            frozen.graph_regions.clear()
            frozen.register_graph(asmond.Rect(1, 0, 1, width), 'CPU', frozen.cpu_power, frozen.power_times, 'power')
            self.assertEqual(asmond.graph_hover_at(frozen, width - 1, 1).value, 999)

    def test_render_dispatch_keeps_numeric_panels_live_and_graph_legends_held(self):
        data = fixtures.LayoutTests.dashboard_data()
        sample, history, memory, battery, usb, io_stats, processes = data
        args = fixtures.LayoutTests.dashboard_args()
        args.held_graphs = asmond.HeldGraphs(history.snapshot(), asmond.MetricSample(cpu_power_mw=12), asmond.IoStats(net_in_bps=34), 10)
        win = fixtures.LayoutTests.RecordingWindow()
        colors = fixtures.LayoutTests.dashboard_colors()
        for panel, function in [('power', 'draw_power_panel'), ('power_graph', 'draw_split_power_graph'), ('load', 'draw_load_panel'), ('io', 'draw_io_panel')]:
            with self.subTest(panel=panel), mock.patch.object(asmond, function) as draw:
                asmond.draw_dashboard_panel(win, asmond.PanelPlacement(panel, asmond.Rect(2, 0, 20, 100), 'normal'),
                    sample, history, memory, battery, usb, io_stats, processes, colors, args,
                    'soc', 'cpu', 'disk_read', 'net_in', 'graph', 'cpu', 'battery', 0, None, True)
                values = draw.call_args.args
                if panel == 'power':
                    self.assertIs(values[2], sample)
                    self.assertIs(values[3], history)
                elif panel == 'power_graph':
                    self.assertIs(values[5], args.held_graphs.history)
                    self.assertIs(values[6], args.held_graphs.sample)
                elif panel == 'load':
                    self.assertIs(values[2], sample)
                    self.assertIs(values[3], args.held_graphs.history)
                    self.assertIs(draw.call_args.kwargs['held'], args.held_graphs)
                else:
                    self.assertIs(values[2], args.held_graphs.io_stats)
                    self.assertIs(values[3], args.held_graphs.history)

    def test_charging_legend_uses_hold_time_instead_of_expiring_held_reading(self):
        history = asmond.History(10)
        history.charge_samples.append((10, 42, -3))
        window = fixtures.LayoutTests.RecordingWindow()
        with mock.patch.object(asmond.time, 'monotonic', return_value=999):
            asmond.draw_charging_graph(window, asmond.Rect(0, 0, 12, 100), history, fixtures.LayoutTests.dashboard_colors(), now=10)
        self.assertIn('Input 42.0 W', window.rendered())

    def test_lost_live_core_readings_do_not_remove_held_core_plots(self):
        sample = asmond.MetricSample(cores=[asmond.CoreMetric('E0', 25)])
        history = asmond.History(10)
        history.add(sample, 10)
        history.hover_enabled = True
        held = asmond.HeldGraphs(history, sample, asmond.IoStats(), 10)
        window = fixtures.LayoutTests.RecordingWindow()
        asmond.draw_usage_matrix(window, 0, 0, 30, 100, asmond.MetricSample(), history,
            fixtures.LayoutTests.dashboard_colors(), 'graph', False, asmond.IoStats(), 'disk_read', 'net_in', held=held)
        self.assertIn('E0', window.rendered_row(1))
        self.assertIn('n/a', window.rendered_row(1))
        self.assertTrue(any(region.labels == ('E0',) for region in history.graph_regions))


class ProcessFilterTests(fixtures.IsolatedTestCase):
    @staticmethod
    def edit_keys(text):
        index = next(i for i, row in enumerate(asmond.MENU_ITEMS) if row[0] == 'process_filter')
        return [ord('m')] + [asmond.curses.KEY_DOWN] * index + [10] + list(text.encode('utf-8'))

    def test_literal_name_casefold_exact_pid_and_empty_query(self):
        rows = fixtures.ProcessRunLoopSafetyTests.process_rows()
        rows += [asmond.ProcessInfo(120, 0, 0, 0, 'Straße[abc]')]
        for query, expected in [('', [12001, 12002, 120]), (' CPU ', [12001]), ('00120', [120]),
                                ('1200', []), ('STRASSE[', [120]), ('.*', []), ('²', [])]:
            with self.subTest(query=query):
                self.assertEqual([p.pid for p in asmond.filter_processes(rows, query)], expected)

    def test_editor_utf8_backspace_bounds_and_literal_shortcuts(self):
        editor = asmond.ProcessFilterEditor('')
        for key in 'qhp? GPU Straße'.encode('utf-8'):
            self.assertIsNone(editor.feed(key))
        self.assertEqual(editor.text, 'qhp? GPU Straße')
        editor.feed(127)
        self.assertEqual(editor.text, 'qhp? GPU Straß')
        for _ in range(100):
            editor.feed(ord('x'))
        self.assertEqual(len(editor.text), 64)
        self.assertEqual(editor.feed(27), 'cancel')
        self.assertEqual(editor.feed(10), 'apply')
        editor = asmond.ProcessFilterEditor('')
        for key in (0xff, 0xc3, 127, ord('a'), 9, -1):
            editor.feed(key)
        self.assertEqual(editor.text, 'a')

    def test_filter_applies_to_navigation_and_term_and_clears_old_confirmation(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        keys = [ord('k')] + self.edit_keys('gpu') + [10, 27, asmond.curses.KEY_DOWN, ord('k'), ord('q')]
        result = runner.run_process_loop([{'update': runner.gpu_update(), 'key': keys[0]}] + [{'key': k} for k in keys[1:]])
        self.assertEqual(result['views'][-1]['pids'], [12002])
        self.assertEqual(result['rendered'][-1]['selected'], 0)
        self.assertEqual(result['rendered'][-1]['pending'], 12002)
        result['kill'].assert_not_called()  # Filtering requires a fresh confirmation.
        self.assertEqual(result['read'].call_count, 2)

    def test_no_matches_do_not_enable_term_or_claim_missing_source(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        keys = self.edit_keys('missing') + [10, 27, ord('k'), ord('k'), ord('q')]
        result = runner.run_process_loop([{'update': runner.gpu_update(), 'key': keys[0]}] + [{'key': k} for k in keys[1:]])
        self.assertEqual(result['views'][-1]['pids'], [])
        self.assertEqual(result['args'].process_filter_total, 2)
        result['read'].assert_not_called()
        result['kill'].assert_not_called()
        args = fixtures.LayoutTests.dashboard_args()
        args.process_filter_total = 2
        text = asmond.source_status(args, asmond.MetricSample(temp_max_c=40), asmond.MemoryStats(total_bytes=100),
            asmond.BatteryStats(charge_pct=50), asmond.UsbCStats(), asmond.IoStats(net_in_bps=0), [], 'left')
        self.assertIn('ps', text.partition('miss')[0])

    def test_escape_cancels_edit_and_shortcuts_are_text(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        keys = self.edit_keys('qhp?') + [27, 27, ord('q')]
        result = runner.run_process_loop([{'key': k} for k in keys])
        self.assertEqual(result['args'].process_filter, '')
        self.assertFalse(any(v['held'] for v in result['views']))

    def test_pid_filter_can_confirm_only_its_visible_target_and_clear_back_to_all(self):
        runner = fixtures.ProcessRunLoopSafetyTests()
        keys = self.edit_keys('12002') + [10, 27, ord('k'), ord('k'), ord('q')]
        result = runner.run_process_loop([{'update': runner.gpu_update(), 'key': keys[0]}] + [{'key': k} for k in keys[1:]])
        result['kill'].assert_called_once_with(12002, asmond.signal.SIGTERM)
        # Reopening the menu preserves its selected entry; erase the name.
        keys = self.edit_keys('cpu') + [10, 27, ord('m'), 10, 127, 127, 127, 10, 27, ord('q')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            result = runner.run_process_loop([{'update': runner.gpu_update(), 'key': keys[0]}] + [{'key': k} for k in keys[1:]], persist_path=path)
            self.assertEqual(result['views'][-1]['pids'], [12001, 12002])
            self.assertEqual(result['args'].process_filter, '')
            self.assertNotIn('process_filter', json.loads(path.read_text()))

    def test_filter_summary_and_empty_result_message_are_visible(self):
        window = fixtures.LayoutTests.RecordingWindow()
        asmond.draw_process_section(window, 0, 0, 12, 80, [], 'cpu', 0, None,
            fixtures.LayoutTests.dashboard_colors(), filter_query='test', total_processes=123)
        self.assertIn('FILTER 0/123', window.rendered_row(0))
        self.assertIn('test', window.rendered_row(1))
        self.assertIn('no matching processes', window.rendered())


class InlineProcessFilterTests(fixtures.IsolatedTestCase):
    def run_keys(self, keys, *, preferred='name', frames=None, **kwargs):
        runner = fixtures.ProcessRunLoopSafetyTests()
        steps = [{'key': key} for key in keys] if frames is None else frames
        steps[0]['update'] = runner.gpu_update()
        return runner.run_process_loop(steps, preferred=preferred, **kwargs)

    def test_command_enter_filters_before_confirmation_and_keeps_search_text(self):
        result = self.run_keys([10, ord('g'), ord('p'), ord('u'), 10, ord('q')])
        views = result['views']
        self.assertEqual([view['filter'] for view in views], ['', '', 'g', 'gp', 'gpu', 'gpu'])
        self.assertEqual([view['inline'] for view in views], [False, True, True, True, True, False])
        self.assertEqual(views[2]['pids'], [12002])
        self.assertEqual(result['args'].process_filter, 'gpu')
        self.assertIsNone(result['args'].process_filter_editor)

    def test_shortcut_letters_and_arrows_cannot_escape_or_act_while_typing(self):
        text = 'qQhmpTk?'
        keys = [10] + list(text.encode()) + [asmond.curses.KEY_DOWN, asmond.curses.KEY_LEFT, 9, 10, ord('q')]
        result = self.run_keys(keys)
        self.assertEqual(len(result['views']), len(keys))
        self.assertEqual(result['args'].process_filter, text)
        self.assertTrue(all(view['editing'] for view in result['views'][1:-1]))
        self.assertFalse(any(view['held'] for view in result['views']))
        result['read'].assert_not_called()
        result['kill'].assert_not_called()

    def test_escape_restores_previous_filter_and_selected_process(self):
        keys = [10] + list(b'worker') + [10, asmond.curses.KEY_DOWN, 10]
        keys += [127] * 6 + list(b'cpu') + [27, ord('q')]
        result = self.run_keys(keys)
        self.assertEqual(result['views'][-2]['filter'], 'cpu')
        self.assertEqual(result['views'][-2]['pids'], [12001])
        self.assertEqual(result['views'][-1]['filter'], 'worker')
        self.assertEqual(result['rendered'][-1]['selected'], 1)

    def test_empty_result_remains_editable_and_empty_input_restores_all(self):
        keys = [10] + list(b'missing') + [10, 10] + [127] * 7 + [10, ord('q')]
        result = self.run_keys(keys)
        self.assertEqual(result['views'][9]['pids'], [])
        self.assertTrue(result['views'][10]['inline'])
        self.assertEqual(result['views'][-1]['filter'], '')
        self.assertEqual(result['views'][-1]['pids'], [12001, 12002])

    def test_enter_without_changing_text_preserves_selected_process(self):
        result = self.run_keys([asmond.curses.KEY_DOWN, 10, 10, ord('q')])
        self.assertEqual(result['rendered'][-1]['selected'], 1)

    def test_enter_requires_command_sort_visible_table_and_keyboard_focus(self):
        for preferred, initial in (('cpu', {}), ('name', {'charge_panel': 'diagnostics'})):
            result = self.run_keys([10, ord('q')], preferred=preferred, initial=initial)
            self.assertFalse(any(view['editing'] for view in result['views']))
        result = self.run_keys([], frames=[{'key': 10, 'size': (23, 70)}, {'key': ord('q')}])
        self.assertFalse(any(view['editing'] for view in result['views']))
        result = self.run_keys([10, ord('q')], with_sample=False)
        self.assertFalse(any(view['editing'] for view in result['views']))

    def test_resize_and_clock_gap_do_not_silently_restore_shortcuts(self):
        result = self.run_keys([], frames=[
            {'key': 10}, {'size': (23, 60), 'wall_jump': 120},
            {'key': ord('q')}, {'size': (61, 180)}, {'key': 27}, {'key': ord('q')},
        ])
        self.assertTrue(all(view['editing'] for view in result['views'][1:-1]))
        self.assertEqual(result['views'][-2]['input'], 'q')
        self.assertEqual(result['views'][-1]['filter'], '')

    def test_editing_clears_term_and_requires_a_new_confirmation(self):
        result = self.run_keys([ord('k'), 10, ord('k'), 27, ord('k'), ord('q')])
        self.assertEqual(result['rendered'][1]['pending'], 12001)
        self.assertTrue(all(frame['pending'] is None for frame in result['rendered'][2:-1]))
        self.assertEqual(result['rendered'][-1]['pending'], 12001)
        result['kill'].assert_not_called()
        self.assertEqual(result['read'].call_count, 2)

    def test_unmatched_preview_keeps_the_same_gpu_and_power_columns(self):
        window = fixtures.LayoutTests.RecordingWindow()
        editor = asmond.ProcessFilterEditor('', inline=True, column_modes=('pid', 'cpu', 'gpu', 'ram', 'rss', 'power', 'name'))
        with mock.patch.object(asmond.time, 'monotonic', return_value=0):
            asmond.draw_process_section(window, 0, 0, 12, 80, [], 'name', 0, None,
                fixtures.LayoutTests.dashboard_colors(), filter_query='missing', total_processes=2, editor=editor)
        row = window.rendered_row(1)
        self.assertIn('GPU%', row)
        self.assertIn('CPU power', row)
        self.assertEqual(row[53], '|')
        self.assertIn('COMMAND', row)
        self.assertIn('no matching processes', window.rendered())

    def test_heading_cursor_blinks_only_in_edit_mode_and_text_tail_fits(self):
        colors = fixtures.LayoutTests.dashboard_colors()
        for encoding in ('utf-8', 'ascii'):
            for width in (5, 9, 20):
                for stamp in (0, .5):
                    with self.subTest(encoding=encoding, width=width, stamp=stamp):
                        window = fixtures.LayoutTests.RecordingWindow(2, 40)
                        window.encoding = encoding
                        editor = asmond.ProcessFilterEditor('Straße e\u0301 温度 long-query', inline=True)
                        with mock.patch.object(asmond.time, 'monotonic', return_value=stamp):
                            asmond.draw_process_filter_heading(window, 0, 3, width, 'COMMAND', '', editor, colors, ready=True)
                        for _, x, text in window.entries:
                            cells = sum(size for _, size in asmond.terminal_cell_groups(text))
                            self.assertLessEqual(x + cells, 3 + width)
                            text.encode(encoding)
                        cursor = window.entries[-1][2]
                        self.assertEqual(cursor, '|' if stamp == 0 else ' ')
        window = fixtures.LayoutTests.RecordingWindow(2, 40)
        asmond.draw_process_filter_heading(window, 0, 0, 20, 'COMMAND', '', None, colors, ready=True)
        self.assertIn('COMMAND ↵', window.rendered_row(0))
        self.assertNotIn('|', window.rendered_row(0))
        window.erase()
        asmond.draw_process_filter_heading(window, 0, 0, 20, 'COMMAND', 'Safari', None, colors, ready=False)
        self.assertIn('Safari', window.rendered_row(0))
        self.assertNotIn('COMMAND', window.rendered_row(0))

    def test_input_header_advertises_enter_and_escape_instead_of_quit(self):
        for width in (72, 170):
            args = fixtures.LayoutTests.dashboard_args()
            args.process_filter_editor = asmond.ProcessFilterEditor('', inline=True)
            window = fixtures.LayoutTests.RecordingWindow(24, width)
            asmond.draw_header(window, args, fixtures.LayoutTests.dashboard_colors())
            self.assertIn('Enter', window.rendered_row(0))
            self.assertIn('Esc', window.rendered_row(0))
            self.assertNotIn('q quit', window.rendered_row(0))


class GraphHoverTests(fixtures.IsolatedTestCase):
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


class GraphMouseTests(fixtures.IsolatedTestCase):
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


class GraphWorkerTimestampTests(fixtures.IsolatedTestCase):
    def setUp(self):
        patcher = mock.patch.object(asmond.MemoryBandwidthSampler, "read", return_value=asmond.MemoryBandwidthStats())
        patcher.start()
        self.addCleanup(patcher.stop)

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
                original_put = asmond.SideMetricsQueue.put

                def enqueue(self, item, *args, **kwargs):
                    original_put(self, item, *args, **kwargs)
                    if item is failure:
                        original_put(self, baseline, *args, **kwargs)
                        original_put(self, recovery, *args, **kwargs)

                sample = asmond.MetricSample(telemetry_source='intel', cpu_usage_pct=24,
                    cpu_usage_source='powermetrics', cpu_power_mw=1000, received_at=now - 4.5)
                with mock.patch.object(asmond, 'History', CapturedHistory), mock.patch.object(asmond.SideMetricsQueue, 'put', enqueue):
                    fixtures.ProcessRunLoopSafetyTests().run_process_loop([
                        {'sample': sample, 'update': first}, {'update': failure, 'key': ord('q')},
                    ], with_sample=False)
                history = histories[0]
                values = list(history.cpu_usage if source == 'cpu' else history.net_in_io)
                timestamps = list(history.cpu_times if source == 'cpu' else history.io_times)
                # Coalescing retains one explicit gap for superseded failure /
                # baseline observations, followed by the real measured zero.
                self.assertEqual(values, [24, 80, None, 0] if source == 'cpu' else [123, None, 0])
                self.assertEqual(timestamps[-1], now - 1)
                self.assertTrue(all(a < b for a, b in zip(timestamps, timestamps[1:])))


class GraphSettingsTests(fixtures.IsolatedTestCase):
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
