# Copyright (c) 2026 Agence nationale de la cohésion des territoires
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
name: compact
type: stdout
short_description: Compact one-line-per-task output with a live pending line on a TTY
description:
  - This callback prints one line for each task and host, in place of the noisy default banners.
  - On a TTY it shows a live pending line for the task and host that run, and it rewrites the line in place when the task ends.
  - It prints a diff block after a changed task, and the full default-style error block after a failed or unreachable task.
  - It suppresses the result line for a dynamic include, such as C(include_tasks) or C(include_role).
version_added: "0.3.0"
author: Suite Territoriale (@suitenumerique)
requirements:
  - set as stdout in configuration
extends_documentation_fragment:
  - default_callback
  - result_format_callback
"""

import sys

from ansible import constants as C
from ansible.playbook.task_include import TaskInclude
from ansible.plugins.callback.default import CallbackModule as Default
from ansible.utils.color import stringc


class CallbackModule(Default):

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'suitenumerique.st.compact'

    HOST_COLOR = 'cyan'

    def _status_line(self, icon, task_name, host_label, state, color):
        line = stringc(u'%s %s' % (icon, task_name), color)
        line += u' ' + stringc(u'@ %s' % host_label, self.HOST_COLOR)
        if state:
            line += u' ' + stringc(u'— %s' % state, color)
        return line

    def __init__(self):
        super().__init__()
        self._pending = {}
        self._suffix = {}
        self._item_counts = {}
        self._diffs = {}
        self._visible_key = None
        self._internal_write = False
        self._is_tty = sys.stdout.isatty()
        self._orig_display = None
        # Display is a process-wide singleton; patch its bound method only on a TTY,
        # so a piped run never pays for the pending-line dance.
        if self._is_tty:
            self._orig_display = self._display.display
            self._display.display = self._display_hook

    @staticmethod
    def _key(task, host_name):
        return (task._uuid, host_name)

    def _current_text(self, key):
        return self._pending[key] + self._suffix.get(key, u'')

    def _raw(self, msg, newline):
        self._internal_write = True
        try:
            self._orig_display(msg, newline=newline, screen_only=True)
        finally:
            self._internal_write = False

    def _draw(self, text):
        # A wrapped line cannot be erased with a lone \r, so keep it within one row.
        self._raw(text[:self._display.columns], newline=False)

    def _clear(self):
        self._raw(u'\r\x1b[K', newline=False)

    def _display_hook(self, msg, *args, **kwargs):
        if self._internal_write or self._visible_key is None or kwargs.get('log_only'):
            self._orig_display(msg, *args, **kwargs)
            return
        self._clear()
        self._orig_display(msg, *args, **kwargs)
        self._draw(self._current_text(self._visible_key))

    def _finalize(self, key):
        self._pending.pop(key, None)
        self._suffix.pop(key, None)
        self._item_counts.pop(key, None)
        if self._is_tty and self._visible_key == key:
            self._clear()
            self._visible_key = None

    def _flush_diffs(self, key):
        diffs = self._diffs.pop(key, None)
        if not diffs:
            return
        for diff in diffs:
            self._display.display(diff)

    def _tick_item(self, result):
        if not self._is_tty:
            return
        key = self._key(result.task, result.host.get_name())
        if key not in self._pending:
            return
        count = self._item_counts.get(key, 0) + 1
        self._item_counts[key] = count
        length = result.result.get('ansible_loop', {}).get('length')
        if length:
            self._suffix[key] = u' (item %d/%d)' % (count, length)
        else:
            self._suffix[key] = u' (item %d)' % count
        if self._visible_key == key:
            self._clear()
            self._draw(self._current_text(key))

    def _tick_retry(self, result):
        if not self._is_tty:
            return
        key = self._key(result.task, result.host.get_name())
        if key not in self._pending:
            return
        attempts = result.result.get('attempts')
        retries = result.result.get('retries')
        if attempts and retries:
            self._suffix[key] = u' (retry %d/%d)' % (attempts, retries)
        if self._visible_key == key:
            self._clear()
            self._draw(self._current_text(key))

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._start_task(task, 'TASK')

    def v2_playbook_on_handler_task_start(self, task):
        self._start_task(task, 'RUNNING HANDLER')

    def _start_task(self, task, prefix):
        # The parent's result handlers lazily print a TASK banner unless _last_task_banner
        # already matches the task uuid. Pre-set it here so that path never fires.
        self._task_type_cache[task._uuid] = prefix
        self._last_task_name = task.get_name().strip()
        self._last_task_banner = task._uuid

    def _print_task_banner(self, task):
        # Under the free strategy an interleaved task start overwrites _last_task_banner,
        # so inherited result paths can still call this. Keep the bookkeeping, print nothing.
        self._last_task_banner = task._uuid

    def v2_runner_on_start(self, host, task):
        if isinstance(task, TaskInclude) or not self._is_tty:
            return
        key = self._key(task, host.get_name())
        self._pending[key] = u'▸ %s @ %s' % (task.get_name().strip(), host.get_name())
        self._suffix.pop(key, None)
        self._item_counts.pop(key, None)
        self._visible_key = key
        self._draw(self._current_text(key))

    def v2_runner_on_ok(self, result):
        key = self._key(result.task, result.host.get_name())

        if isinstance(result.task, TaskInclude):
            self._finalize(key)
            self._diffs.pop(key, None)
            return

        host_label = self.host_label(result)
        task_name = result.task.get_name().strip()
        changed = result.result.get('changed', False)

        if changed:
            msg = self._status_line(u'●', task_name, host_label, u'changed', C.COLOR_CHANGED)
        else:
            if not self.get_option('display_ok_hosts'):
                self._finalize(key)
                self._diffs.pop(key, None)
                return
            msg = self._status_line(u'✔', task_name, host_label, None, C.COLOR_OK)

        self._finalize(key)
        self._display.display(msg)
        self._flush_diffs(key)

        self._handle_warnings_and_exception(result)
        if 'results' in result.result:
            self._process_items(result)
        self._clean_results(result.result, result.task.action)

        if self._run_is_verbose(result):
            self._display.display(self._dump_results(result.result))

    def v2_runner_on_failed(self, result, ignore_errors=False):
        key = self._key(result.task, result.host.get_name())
        self._finalize(key)

        host_label = self.host_label(result)
        msg = self._status_line(u'✘', result.task.get_name().strip(), host_label, u'failed', C.COLOR_ERROR)
        self._display.display(msg, stderr=self.get_option('display_failed_stderr'))

        super().v2_runner_on_failed(result, ignore_errors=ignore_errors)

        self._flush_diffs(key)

    def v2_runner_on_unreachable(self, result):
        key = self._key(result.task, result.host.get_name())
        self._finalize(key)
        self._diffs.pop(key, None)

        host_label = self.host_label(result)
        msg = self._status_line(u'✘', result.task.get_name().strip(), host_label, u'unreachable', C.COLOR_UNREACHABLE)
        self._display.display(msg, stderr=self.get_option('display_failed_stderr'))

        super().v2_runner_on_unreachable(result)

    def v2_runner_on_skipped(self, result):
        key = self._key(result.task, result.host.get_name())
        self._finalize(key)
        self._diffs.pop(key, None)

        if not self.get_option('display_skipped_hosts'):
            return

        self._handle_warnings_and_exception(result)
        if result.task.loop is not None and 'results' in result.result:
            self._process_items(result)
        self._clean_results(result.result, result.task.action)

        host_label = self.host_label(result)
        msg = self._status_line(u'○', result.task.get_name().strip(), host_label, u'skipped', C.COLOR_SKIP)
        self._display.display(msg)

        if self._run_is_verbose(result):
            self._display.display(self._dump_results(result.result))

    def v2_on_file_diff(self, result):
        # The strategy fires this before v2_runner_on_ok (and mid-loop per item), so
        # buffer the rendered diff and flush it once the one-liner is on screen.
        key = self._key(result.task, result.host.get_name())
        buf = self._diffs.setdefault(key, [])
        if result.task.loop and 'results' in result.result:
            for res in result.result['results']:
                if 'diff' in res and res['diff'] and res.get('changed', False):
                    diff = self._get_diff(res['diff'])
                    if diff:
                        buf.append(diff)
        elif 'diff' in result.result and result.result['diff'] and result.result.get('changed', False):
            diff = self._get_diff(result.result['diff'])
            if diff:
                buf.append(diff)

    def v2_runner_item_on_ok(self, result):
        if isinstance(result.task, TaskInclude):
            return
        self._tick_item(result)
        if self._run_is_verbose(result):
            super().v2_runner_item_on_ok(result)

    def v2_runner_item_on_failed(self, result):
        self._tick_item(result)
        super().v2_runner_item_on_failed(result)

    def v2_runner_item_on_skipped(self, result):
        self._tick_item(result)
        if self.get_option('display_skipped_hosts'):
            super().v2_runner_item_on_skipped(result)

    def v2_runner_retry(self, result):
        if self._is_tty:
            self._tick_retry(result)
            return
        super().v2_runner_retry(result)

    def v2_playbook_on_include(self, included_file):
        pass

    def v2_playbook_on_stats(self, stats):
        if self._visible_key is not None:
            self._clear()
            self._visible_key = None
        if self._is_tty:
            try:
                del self._display.display
            except AttributeError:
                pass
        super().v2_playbook_on_stats(stats)
