import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


if 'paramiko' not in sys.modules:
    sys.modules['paramiko'] = SimpleNamespace(
        SSHClient=object, AutoAddPolicy=object, AuthenticationException=Exception
    )

from app import core


def state(active, substate, processes=()):
    return {
        'active_state': active,
        'sub_state': substate,
        'mariadbd_processes': list(processes),
    }


def row(host, uuid='cluster-a', seqno='10', safe='0'):
    return {'host': host, 'uuid': uuid, 'seqno': seqno, 'safe_to_bootstrap': safe}


class ProcessRecoveryStateTests(unittest.TestCase):
    def test_each_blocked_node_is_independently_recoverable(self):
        for active, substate in [('activating', 'start'), ('deactivating', 'stop'), ('failed', 'failed')]:
            action, _ = core.process_recovery_state(state(active, substate, ['123 mariadbd']))
            self.assertEqual(action, 'recover-process')

    def test_failed_without_process_only_cleans_systemd_state(self):
        action, _ = core.process_recovery_state(state('failed', 'failed'))
        self.assertEqual(action, 'reset-failed')

    def test_cleanly_stopped_node_does_not_offer_process_recovery(self):
        action, _ = core.process_recovery_state(state('inactive', 'dead'))
        self.assertEqual(action, 'none')

    def test_no_time_based_recovery_setting_exists(self):
        self.assertFalse(hasattr(core, 'RECOVERY_STUCK_TIMEOUT'))

    def test_process_that_already_disappeared_is_not_killed(self):
        class Remote:
            def __init__(self):
                self.commands = []
                self.replies = iter([
                    (0, 'ActiveState=failed\nSubState=failed\nMainPID=0\nResult=exit-code', ''),
                    (0, '', ''),
                    (0, '', ''),
                    (0, '', ''),
                    (0, 'ActiveState=inactive\nSubState=dead\nMainPID=0\nResult=success', ''),
                    (0, '', ''),
                ])
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def run(self, command, timeout=30):
                self.commands.append(command)
                return next(self.replies)

        remote = Remote()
        with patch.object(core, 'root_remote', return_value=remote):
            ok, _, _ = core.recover_mariadb_process('node1', 'secret')
        self.assertTrue(ok)
        self.assertNotIn('systemctl kill --kill-whom=all --signal=SIGKILL mariadb', remote.commands)
        self.assertNotIn('systemctl --no-block stop mariadb', remote.commands)
        self.assertIn('systemctl reset-failed mariadb', remote.commands)

    def test_live_blocked_process_is_stopped_before_kill_and_cleanup(self):
        class Remote:
            def __init__(self):
                self.commands = []
                self.replies = iter([
                    (0, 'ActiveState=deactivating\nSubState=stop\nMainPID=42\nResult=success', ''),
                    (0, '42 mariadbd', ''),
                    (0, '', ''),
                    (0, '42 mariadbd', ''),
                    (0, '', ''),
                    (0, '', ''),
                    (0, '', ''),
                    (0, 'ActiveState=inactive\nSubState=dead\nMainPID=0\nResult=success', ''),
                    (0, '', ''),
                ])
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def run(self, command, timeout=30):
                self.commands.append(command)
                return next(self.replies)

        remote = Remote()
        with patch.object(core, 'root_remote', return_value=remote):
            ok, _, _ = core.recover_mariadb_process('node1', 'secret')
        self.assertTrue(ok)
        stop = remote.commands.index('systemctl --no-block stop mariadb')
        kill = remote.commands.index('systemctl kill --kill-whom=all --signal=SIGKILL mariadb')
        reset = remote.commands.index('systemctl reset-failed mariadb')
        self.assertLess(stop, kill)
        self.assertLess(kill, reset)


class GaleraRecommendationTests(unittest.TestCase):
    def test_single_safe_node_is_recommended(self):
        result = core.galera_recommendation([
            row('node1', seqno='1500'), row('node2', seqno='1505', safe='1'), row('node3', seqno='1504'),
        ])
        self.assertEqual(result['host'], 'node2')
        self.assertEqual(result['warning'], '')

    def test_multiple_safe_nodes_never_get_an_automatic_recommendation(self):
        result = core.galera_recommendation([row('node1', safe='1'), row('node2', safe='1')])
        self.assertIsNone(result['host'])
        self.assertEqual(result['warning'], 'multiple-safe')

    def test_seqno_minus_one_requires_position_recovery(self):
        result = core.galera_recommendation([row('node1', seqno='-1'), row('node2', seqno='-1')])
        self.assertIsNone(result['host'])
        self.assertEqual(result['warning'], 'needs-position')

    def test_different_uuid_blocks_recommendation_even_if_seqno_is_minus_one(self):
        result = core.galera_recommendation([row('node1', uuid='A', seqno='-1'), row('node2', uuid='B', seqno='20')])
        self.assertIsNone(result['host'])
        self.assertEqual(result['warning'], 'uuid-mismatch')

    def test_recovered_highest_seqno_is_recommended(self):
        rows = [row('node1', seqno='-1'), row('node2', seqno='-1'), row('node3', seqno='-1')]
        recovered = [row('node1', seqno='18220'), row('node2', seqno='18245'), row('node3', seqno='18231')]
        result = core.galera_recommendation(rows, recovered)
        self.assertEqual(result['host'], 'node2')
        self.assertIn('SEQNO más alto', result['reason'])
