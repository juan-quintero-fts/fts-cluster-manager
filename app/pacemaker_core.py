"""Read-only monitoring for a Pacemaker/Corosync cluster with QDevice/QNetd."""
from __future__ import annotations

import concurrent.futures
import os
import re
import time
from dataclasses import dataclass

from .core import Remote, settings, tcp_reachable


def _bool(value: str) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _positive_int(value: str, default: int) -> int:
    try:
        return max(5, int(value))
    except (TypeError, ValueError):
        return default


def _named_nodes(value: str):
    nodes = []
    for item in (value or '').split(','):
        name, separator, host = item.strip().partition('=')
        if separator and name and host:
            nodes.append({'name': name, 'host': host})
    return nodes


@dataclass
class PacemakerSettings:
    enabled: bool = _bool(os.getenv('PACEMAKER_ENABLED', 'false'))
    monitor_interval: int = _positive_int(os.getenv('PACEMAKER_MONITOR_INTERVAL', '10'), 10)
    qnetd_host: str = os.getenv('QNETD_HOST', '172.16.0.9').strip()
    qnetd_name: str = os.getenv('QNETD_NAME', 'server3').strip()

    @property
    def nodes(self):
        return _named_nodes(os.getenv(
            'PACEMAKER_NODES',
            'SERVER1=172.16.0.1,SERVER2=172.16.0.2',
        ))


pacemaker_settings = PacemakerSettings()


def _service(remote, name: str) -> str:
    _, output, _ = remote.run(f'systemctl is-active {name} 2>/dev/null || true')
    return output or 'unknown'


def _parse_pcs_status(output: str):
    online = []
    offline = []
    dc = 'N/A'
    resources = []
    for line in output.splitlines():
        match = re.search(r'Current DC:\s*([^\s(]+)', line, re.I)
        if match:
            dc = match.group(1)
        match = re.search(r'Online:\s*\[([^\]]*)\]', line, re.I)
        if match:
            online.extend(match.group(1).split())
        match = re.search(r'OFFLINE:\s*\[([^\]]*)\]', line, re.I)
        if match:
            offline.extend(match.group(1).split())
        match = re.search(
            r'^\s*\*\s+(.+?):\s+(Started|Stopped|FAILED|Failed|Master|Promoted|Unpromoted)(?:\s+(.+?))?\s*$',
            line,
        )
        if match:
            resources.append({
                'resource': match.group(1).strip(),
                'status': match.group(2).strip(),
                'node': match.group(3).strip() if match.group(3) else 'N/A',
            })
    resource_nodes = sorted({
        resource['node'] for resource in resources
        if resource['node'] != 'N/A' and resource['status'] in {'Started', 'Master', 'Promoted'}
    })
    if len(resource_nodes) == 1:
        resource_owner = resource_nodes[0]
    elif resource_nodes:
        resource_owner = 'Distribuidos'
    else:
        resource_owner = 'N/A'
    return {
        'dc': dc,
        'online': sorted(set(online)),
        'offline': sorted(set(offline)),
        'resources': resources,
        'resource_owner': resource_owner,
        'resource_nodes': resource_nodes,
    }


def _parse_quorum(output: str):
    values = {'nodes': 'N/A', 'expected_votes': 'N/A', 'total_votes': 'N/A', 'quorum': 'N/A', 'quorate': 'N/A', 'flags': 'N/A'}
    fields = {
        'Nodes': 'nodes', 'Expected votes': 'expected_votes', 'Total votes': 'total_votes',
        'Quorum': 'quorum', 'Quorate': 'quorate', 'Flags': 'flags',
    }
    for line in output.splitlines():
        key, separator, value = line.partition(':')
        if separator and key.strip() in fields:
            values[fields[key.strip()]] = value.strip()
    return values


def _parse_qdevice(output: str):
    values = {'host': 'N/A', 'algorithm': 'N/A', 'state': 'N/A'}
    fields = {'QNetd host': 'host', 'Algorithm': 'algorithm', 'State': 'state'}
    for line in output.splitlines():
        key, separator, value = line.partition(':')
        if separator and key.strip() in fields:
            values[fields[key.strip()]] = value.strip()
    return values


def inspect_pacemaker_node(node):
    row = {**node, 'ssh': False, 'pacemaker': 'unknown', 'corosync': 'unknown', 'qdevice': 'unknown', 'error': ''}
    if not tcp_reachable(node['host'], settings.ssh_port):
        row['error'] = 'Puerto SSH no accesible'
        return row
    try:
        with Remote(node['host']) as remote:
            row['ssh'] = True
            row['pacemaker'] = _service(remote, 'pacemaker')
            row['corosync'] = _service(remote, 'corosync')
            row['qdevice'] = _service(remote, 'corosync-qdevice')
    except Exception as exc:
        row['error'] = str(exc)
    return row


def inspect_qnetd():
    row = {'name': pacemaker_settings.qnetd_name, 'host': pacemaker_settings.qnetd_host,
           'ssh': False, 'qnetd': 'unknown', 'pcsd': 'unknown', 'error': ''}
    if not row['host'] or not tcp_reachable(row['host'], settings.ssh_port):
        row['error'] = 'Puerto SSH no accesible'
        return row
    try:
        with Remote(row['host']) as remote:
            row['ssh'] = True
            row['qnetd'] = _service(remote, 'corosync-qnetd')
            row['pcsd'] = _service(remote, 'pcsd')
    except Exception as exc:
        row['error'] = str(exc)
    return row


def _cluster_details(nodes):
    fallback = {'pcs': {}, 'quorum': _parse_quorum(''), 'qdevice': _parse_qdevice(''), 'device_votes': 'N/A', 'no_quorum_policy': 'N/A', 'error': ''}
    for node in nodes:
        if not node['ssh']:
            continue
        try:
            with Remote(node['host']) as remote:
                _, pcs, pcs_error = remote.run('pcs status --full 2>&1 || true', timeout=30)
                _, quorum, quorum_error = remote.run('pcs quorum status 2>&1 || true', timeout=30)
                _, qdevice, qdevice_error = remote.run('pcs quorum device status 2>&1 || true', timeout=30)
                _, votes, _ = remote.run('corosync-cmapctl -g quorum.device.votes 2>&1 || true')
                _, properties, _ = remote.run('pcs property show 2>&1 || true')
            device_match = re.search(r'quorum\.device\.votes\s*\([^)]*\)\s*=\s*(\S+)', votes)
            policy_match = re.search(r'no-quorum-policy:\s*(\S+)', properties, re.I)
            return {
                'pcs': _parse_pcs_status(pcs), 'quorum': _parse_quorum(quorum), 'qdevice': _parse_qdevice(qdevice),
                'device_votes': device_match.group(1) if device_match else 'N/A',
                'no_quorum_policy': policy_match.group(1) if policy_match else 'predeterminado',
                'error': '; '.join(part for part in (pcs_error, quorum_error, qdevice_error) if part),
            }
        except Exception as exc:
            fallback['error'] = str(exc)
    return fallback


def classify_pacemaker(nodes, qnetd, details):
    quorum = details['quorum']
    active_nodes = [node for node in nodes if node['pacemaker'] == 'active' and node['corosync'] == 'active']
    if not active_nodes:
        return 'DOWN', 'No hay nodos Pacemaker/Corosync disponibles.'
    if str(quorum['quorate']).lower() != 'yes':
        return 'CRITICAL', 'El clúster no tiene quorum; se muestra el estado sin ejecutar cambios.'
    if len(active_nodes) != len(nodes) or qnetd['qnetd'] != 'active' or any(node['qdevice'] != 'active' for node in nodes):
        return 'DEGRADED', 'El clúster conserva quorum, pero uno o más servicios no están activos.'
    return 'HEALTHY', 'Pacemaker, Corosync y QDevice están operativos con quorum.'


def pacemaker_status():
    if not pacemaker_settings.enabled:
        return {'enabled': False, 'level': 'DISABLED', 'summary': 'Monitoreo Pacemaker no habilitado.', 'nodes': [], 'qnetd': {}, 'details': {}}
    configured = pacemaker_settings.nodes
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(configured))) as executor:
        nodes = list(executor.map(inspect_pacemaker_node, configured))
    qnetd = inspect_qnetd()
    details = _cluster_details(nodes)
    level, summary = classify_pacemaker(nodes, qnetd, details)
    return {'enabled': True, 'level': level, 'summary': summary, 'nodes': nodes, 'qnetd': qnetd,
            'details': details, 'last_updated': time.strftime('%Y-%m-%d %H:%M:%S')}
