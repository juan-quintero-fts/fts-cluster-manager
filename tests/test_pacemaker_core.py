import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if 'paramiko' not in sys.modules:
    sys.modules['paramiko'] = SimpleNamespace(SSHClient=object, AutoAddPolicy=object, AuthenticationException=Exception)

from app import pacemaker_core


class PacemakerParsingTests(unittest.TestCase):
    def test_invalid_monitor_interval_uses_default(self):
        self.assertEqual(pacemaker_core._positive_int('', 10), 10)
        self.assertEqual(pacemaker_core._positive_int('2', 10), 5)

    def test_node_check_uses_configured_ssh_port(self):
        with patch.object(pacemaker_core, 'tcp_reachable', return_value=False) as reachable:
            pacemaker_core.inspect_pacemaker_node({'name': 'SERVER1', 'host': '172.16.0.1'})
        reachable.assert_called_once_with('172.16.0.1', pacemaker_core.settings.ssh_port)

    def test_parses_dc_online_and_resource_location(self):
        state = pacemaker_core._parse_pcs_status('''Current DC: SERVER1 (version 2.0) - partition with quorum\nOnline: [ SERVER1 SERVER2 ]\n  * vip: Started SERVER2\n''')
        self.assertEqual(state['dc'], 'SERVER1')
        self.assertEqual(state['online'], ['SERVER1', 'SERVER2'])
        self.assertEqual(state['resources'], [{'resource': 'vip', 'node': 'SERVER2'}])

    def test_parses_quorum_and_qdevice(self):
        quorum = pacemaker_core._parse_quorum('''Nodes: 2\nExpected votes: 3\nTotal votes: 3\nQuorum: 2\nQuorate: Yes\nFlags: Quorate Qdevice\n''')
        qdevice = pacemaker_core._parse_qdevice('''QNetd host: 172.16.0.9:5403\nAlgorithm: Fifty-Fifty split\nState: Connected\n''')
        self.assertEqual(quorum['quorate'], 'Yes')
        self.assertEqual(quorum['total_votes'], '3')
        self.assertEqual(qdevice['state'], 'Connected')

    def test_missing_quorum_is_critical(self):
        nodes = [{'pacemaker': 'active', 'corosync': 'active', 'qdevice': 'active'}]
        qnetd = {'qnetd': 'active'}
        level, _ = pacemaker_core.classify_pacemaker(nodes, qnetd, {'quorum': {'quorate': 'No'}})
        self.assertEqual(level, 'CRITICAL')
