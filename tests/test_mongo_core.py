import unittest
from unittest.mock import patch
import sys
from types import SimpleNamespace

# Unit tests run without network clients or real infrastructure dependencies.
if 'paramiko' not in sys.modules:
    sys.modules['paramiko'] = SimpleNamespace(
        SSHClient=object, AutoAddPolicy=object, AuthenticationException=Exception
    )

from app import mongo_core
from app.audit import sanitize_detail


def node(name, role='SECONDARY', health=1, lag=0, ssh=True, service='active'):
    return {'name': name, 'address': name + ':27017', 'ssh': ssh, 'service': service,
            'role': role if ssh and service == 'active' else 'DOWN', 'health': health if ssh else 'N/A',
            'lag_seconds': lag, 'lag_level': mongo_core.lag_level(lag), 'optimeDate': None}


def topology(w='majority', majority=2):
    return {'majorityVoteCount': majority, 'defaultWriteConcern': w, 'primary': 'mongodb1'}


class MongoClassificationTests(unittest.TestCase):
    def test_three_healthy_nodes(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', 'PRIMARY'), node('mongodb2'), node('mongodb3')], topology())[0], 'HEALTHY')

    def test_third_node_down_is_degraded(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', 'PRIMARY'), node('mongodb2'), node('mongodb3', ssh=False)], topology())[0], 'DEGRADED')

    def test_new_primary_after_primary_failure(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', ssh=False), node('mongodb2', 'PRIMARY'), node('mongodb3')], topology())[0], 'DEGRADED')

    def test_no_majority_is_down_without_primary(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', ssh=False), node('mongodb2', ssh=False), node('mongodb3')], topology())[0], 'DOWN')

    def test_recovering_is_syncing(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', 'PRIMARY'), node('mongodb2', 'RECOVERING'), node('mongodb3')], topology())[0], 'SYNCING')

    def test_high_lag_is_critical(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', 'PRIMARY'), node('mongodb2', lag=31), node('mongodb3')], topology())[0], 'CRITICAL')

    def test_majority_not_configured_is_degraded(self):
        self.assertEqual(mongo_core.classify_mongo([node('mongodb1', 'PRIMARY'), node('mongodb2'), node('mongodb3')], topology('1'))[0], 'DEGRADED')

    def test_lag_thresholds(self):
        self.assertEqual(mongo_core.lag_level(5), 'OK'); self.assertEqual(mongo_core.lag_level(6), 'WARNING'); self.assertEqual(mongo_core.lag_level(31), 'CRITICAL')

    def test_mongosh_failure_keeps_node(self):
        with patch.object(mongo_core, 'tcp_reachable', return_value=True), patch.object(mongo_core, 'MongoRemote') as remote:
            remote.return_value.__enter__.return_value.run.side_effect = [(0, 'active', ''), (0, 'yes', ''), (0, '7.0', ''), (1, '', 'denied')]
            result = mongo_core.inspect_mongo_node('mongodb1', '127.0.0.1:27017')
        self.assertIn('Error consultando', result['error'])

    def test_ssh_failure_keeps_node(self):
        with patch.object(mongo_core, 'MongoRemote') as remote:
            remote.return_value.__enter__.side_effect = mongo_core.MongoError('SSH no disponible')
            result = mongo_core.inspect_mongo_node('mongodb1', '127.0.0.1:27017')
        self.assertFalse(result['ssh'])

    def test_start_requires_configured_node(self):
        with self.assertRaises(mongo_core.MongoError): mongo_core.start_mongod('untrusted-host', 'password')

    def test_stepdown_not_allowed_without_majority(self):
        rows = [node('mongodb1', 'PRIMARY'), node('mongodb2', ssh=False), node('mongodb3', ssh=False)]
        self.assertFalse(mongo_core.can_controlled_stepdown(rows, topology(), 'mongodb1'))

    def test_passwords_are_redacted_from_audit_detail(self):
        detail = sanitize_detail('password=super-secret MONGOSH_PASSWORD: another-secret')
        self.assertNotIn('super-secret', detail); self.assertNotIn('another-secret', detail)
