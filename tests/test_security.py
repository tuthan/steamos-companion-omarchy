"""Regressions derived from the author's marketplace security reviews."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import client_core as client

ROOT = Path(__file__).resolve().parents[1]


class StateSecurityTests(unittest.TestCase):
    def test_ancestor_symlinks_and_writable_directories_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / 'outside'
            outside.mkdir()
            (base / 'link').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(client.ClientError):
                client.ClientStore(base / 'link' / 'state')
            self.assertFalse((outside / 'state').exists())
            outside.chmod(0o777)
            with self.assertRaises(client.ClientError):
                client.ClientStore(outside / 'state')
            self.assertFalse((outside / 'state').exists())

    def test_parent_swap_during_write_cannot_redirect_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / 'parent'
            root = parent / 'state'
            client._safe_dir(root)
            outside = base / 'outside'
            (outside / 'state').mkdir(parents=True)
            victim = outside / 'state' / 'client.json'
            victim.write_text('untouched')
            real_replace = os.replace

            def swap(src, dst, **kwargs):
                parent.rename(base / 'moved')
                parent.symlink_to(outside, target_is_directory=True)
                return real_replace(src, dst, **kwargs)

            with mock.patch.object(client.os, 'replace', side_effect=swap):
                client._write_private(root, root / 'client.json', {'test': True}, 'test')
            self.assertEqual(victim.read_text(), 'untouched')
            self.assertEqual(json.loads((base / 'moved/state/client.json').read_text()), {'test': True})
            with self.assertRaises(client.ClientError):
                client._read_private(root / 'client.json', 'test')

    def test_leaf_swap_after_open_cannot_redirect_read_or_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'client.json'
            state.write_text('{"original":true}')
            outside = root / 'outside'
            outside.write_text('{"outside":true}')
            outside.chmod(0o644)
            real_fstat = os.fstat
            swapped = False

            def swap(fd):
                nonlocal swapped
                info = real_fstat(fd)
                if not swapped and info.st_ino == state.lstat().st_ino:
                    swapped = True
                    state.rename(root / 'original')
                    state.symlink_to(outside)
                return info

            with mock.patch.object(client.os, 'fstat', side_effect=swap):
                self.assertEqual(client._read_private(state, 'test'), {'original': True})
            self.assertTrue(swapped)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o644)

    def test_hardlinks_fifo_and_oversized_state_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'client.json'
            outside = root / 'outside'
            outside.write_text('{}')
            outside.chmod(0o644)
            os.link(outside, state)
            for operation in (
                lambda: client._read_private(state, 'test'),
                lambda: client._write_private(root, state, {}, 'test'),
                lambda: client._remove_private(state, 'test'),
            ):
                with self.assertRaises(client.ClientError):
                    operation()
            self.assertEqual(outside.stat().st_mode & 0o777, 0o644)
            state.unlink()
            os.mkfifo(state)
            with self.assertRaises(client.ClientError):
                client._read_private(state, 'test')
            state.unlink()
            state.write_bytes(b' ' * (client.MAX_STATE_BYTES + 1))
            with self.assertRaisesRegex(client.ClientError, 'too large'):
                client._read_private(state, 'test')


class ResponseSecurityTests(unittest.TestCase):
    def test_response_budgets_reject_instead_of_truncating_action_ids(self):
        deep = None
        for _ in range(18):
            deep = [deep]
        for value in (
            {'outputs': [{}] * 17}, {'output_keys': ['output:a'] * 17}, {'saved_output_keys': ['output:a'] * 17},
            {'modes': [{}] * 257},
            {'id': 'x' * 1025}, {'reason': 'x' * 1025},
            {'data': deep}, {'data': [[0] * 256] * 20},
            {'data': {str(i): 0 for i in range(129)}}, {'number': float('nan')},
        ):
            with self.subTest(value=str(value)[:60]), self.assertRaises(client.ClientError):
                client.validate_response_limits(value)
        for fixture in (ROOT / 'protocol/fixtures').glob('*.json'):
            client.validate_response_limits(json.loads(fixture.read_text()))
        client.validate_response_limits({'modes': [{'id': str(i)} for i in range(256)]})

    def test_transport_rejects_large_bodies_and_large_lists(self):
        for raw in (b' ' * (client.MAX_BODY_BYTES + 1), json.dumps({'protocol_version': 1, 'outputs': [{}] * 17}).encode()):
            connection = mock.MagicMock()
            connection.sock.getpeercert.return_value = b'certificate'
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = raw
            with mock.patch.object(client.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaises(client.ClientError) as caught:
                    client.PinnedTransport('https://host.example', client.fingerprint_der(b'certificate'), 'token').request('POST', '/v1/power', {'action': 'suspend'})
            self.assertTrue(caught.exception.unknown)
            response.read.assert_called_once_with(client.MAX_BODY_BYTES + 1)
            connection.close.assert_called_once()


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute the QML JavaScript regression harness')
class HelperOutputSecurityTests(unittest.TestCase):
    def test_chunk_limits_latch_kill_discard_and_do_not_accept_partial_success(self):
        panel = (ROOT / 'Panel.qml').read_text()
        def function(name):
            return panel.split('  function ' + name + '(', 1)[1].split('\n  }', 1)[0]
        script = '''
const assert = require('node:assert/strict');
let killed = 0, result = null;
const helper = {signal(n) { assert.equal(n, 9); killed++; }};
const helperWatchdog = {stop() {}};
const Qt = {callLater() {}};
let root;
function reset() {
  root = {helperStdout: '', helperStderr: '', helperStdoutBytes: 0, helperStderrBytes: 0,
    helperStdoutLimit: 64, helperStderrLimit: 8, helperOverflow: false,
    helperSettled: false, helperTimedOut: false, helperSpawnFailed: false,
    helperJob: {poll: false, callback(value) { result = value; }}};
}
''' + 'function collectHelper(' + function('collectHelper') + '\n}\n' + 'function settleHelper(' + function('settleHelper') + '\n}\n' + '''
reset(); collectHelper('{"ok":true}', false); settleHelper(); assert.equal(result.ok, true);
reset(); collectHelper('{"ok":true}', false); collectHelper('x'.repeat(65), false);
assert.equal(root.helperStdout, ''); assert.equal(killed, 1);
collectHelper('{"ok":true}', false); settleHelper();
assert.equal(result.ok, false); assert.equal(result.unknown, true);
reset(); collectHelper('12345678', true); assert.equal(root.helperOverflow, false);
collectHelper('x', true); assert.equal(root.helperOverflow, true); assert.equal(killed, 2);
reset(); collectHelper('éééé', true); assert.equal(root.helperStderrBytes, 8);
collectHelper('é', true); assert.equal(root.helperOverflow, true);
reset(); collectHelper('x'.repeat(32), false); collectHelper('x'.repeat(32), false);
assert.equal(root.helperOverflow, false); collectHelper('x', false); assert.equal(root.helperOverflow, true);
reset(); collectHelper('{"ok":true}', false); root.helperTimedOut = true; settleHelper();
assert.equal(result.ok, false); assert.equal(result.unknown, true);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('StdioCollector', panel)
        self.assertIn('splitMarker: ""; onRead: data => root.collectHelper(data, false)', panel)
        self.assertIn('splitMarker: ""; onRead: data => root.collectHelper(data, true)', panel)
