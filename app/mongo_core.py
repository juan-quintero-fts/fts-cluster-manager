"""MongoDB Replica Set monitoring and explicitly-confirmed operations.

Monitoring is read-only and runs ``mongosh`` on every MongoDB host through SSH;
the manager container never needs a MongoDB client installed locally.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import paramiko

LAG_OK_SECONDS = 5
LAG_WARNING_SECONDS = 30
SSH_TIMEOUT_SECONDS = 5


def _bool(value: str, default: bool = False) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'} if value is not None else default


def _nodes(value: str) -> Dict[str, str]:
    result = {}
    for item in (value or '').split(','):
        name, sep, address = item.strip().partition('=')
        if sep and name and address:
            result[name] = address
    return result


@dataclass
class MongoSettings:
    enabled: bool = _bool(os.getenv('MONGO_ENABLED', 'false'))
    replica_set: str = os.getenv('MONGO_REPLICA_SET', 'rs0')
    database: str = os.getenv('MONGO_DATABASE', 'admin')
    service: str = os.getenv('MONGO_SERVICE', 'mongod')
    monitor_interval: int = max(5, int(os.getenv('MONGO_MONITOR_INTERVAL', '10')))
    user: str = os.getenv('MONGO_USER', '')
    password: str = os.getenv('MONGO_PASSWORD', '')
    auth_source: str = os.getenv('MONGO_AUTH_SOURCE', 'admin')
    ssh_user: str = os.getenv('MONGO_SSH_USER') or os.getenv('SSH_USER', 'ftsuser')
    ssh_port: int = int(os.getenv('MONGO_SSH_PORT') or os.getenv('SSH_PORT', '22'))
    ssh_key_path: str = os.getenv('MONGO_SSH_KEY_PATH') or os.getenv('SSH_KEY_PATH', '/run/secrets/ssh_key')
    ssh_password: str = os.getenv('SSH_PASSWORD', '')
    root_ssh_user: str = os.getenv('ROOT_SSH_USER', 'root')

    @property
    def nodes(self):
        return _nodes(os.getenv('MONGO_NODES', ''))

    @property
    def external_nodes(self):
        return _nodes(os.getenv('MONGO_EXTERNAL_NODES', ''))


mongo_settings = MongoSettings()


class MongoError(RuntimeError):
    pass


class MongoSSHAuthenticationError(MongoError):
    pass


class MongoRemote:
    def __init__(self, host: str, user: Optional[str] = None, password: Optional[str] = None, use_key=True):
        self.host, self.user, self.password, self.use_key = host, user or mongo_settings.ssh_user, password, use_key
        self.client = None

    def __enter__(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        options = dict(hostname=self.host, port=mongo_settings.ssh_port, username=self.user,
                       timeout=SSH_TIMEOUT_SECONDS, banner_timeout=SSH_TIMEOUT_SECONDS, auth_timeout=8,
                       allow_agent=False, look_for_keys=False)
        if self.password is not None:
            options['password'] = self.password
        elif mongo_settings.ssh_password:
            options['password'] = mongo_settings.ssh_password
        elif self.use_key and os.path.isfile(mongo_settings.ssh_key_path):
            options['key_filename'] = mongo_settings.ssh_key_path
        try:
            client.connect(**options)
        except paramiko.AuthenticationException as exc:
            raise MongoSSHAuthenticationError('Autenticación SSH rechazada.') from exc
        except Exception as exc:
            raise MongoError(f'SSH no disponible: {exc}') from exc
        self.client = client
        return self

    def __exit__(self, *args):
        if self.client:
            self.client.close()

    def run(self, command: str, timeout=25):
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        return code, stdout.read().decode(errors='replace').strip(), stderr.read().decode(errors='replace').strip()


def tcp_reachable(host: str, port: int = 27017, timeout=1.5) -> bool:
    try:
        with socket.create_connection((host.rsplit(':', 1)[0], int(host.rsplit(':', 1)[1]) if ':' in host else port), timeout=timeout):
            return True
    except OSError:
        return False


def _mongosh_prefix() -> str:
    parts = ['mongosh', '--quiet', '--host', '127.0.0.1', '--port', '27017']
    if mongo_settings.user:
        parts += ['--username', mongo_settings.user, '--authenticationDatabase', mongo_settings.auth_source]
        if mongo_settings.password:
            # Passed only to the remote process environment, never logged/audited.
            return 'MONGOSH_PASSWORD=' + shlex.quote(mongo_settings.password) + ' ' + ' '.join(shlex.quote(p) for p in parts) + ' --password "$MONGOSH_PASSWORD"'
    return ' '.join(shlex.quote(p) for p in parts)


READ_SCRIPT = """
const admin=db.getSiblingDB('admin');
const status=admin.runCommand({replSetGetStatus:1});
const hello=admin.runCommand({hello:1});
const rw=admin.runCommand({getDefaultRWConcern:1});
const ss=admin.runCommand({serverStatus:1});
const conf=admin.runCommand({replSetGetConfig:1});
const cleanMember=m=>({name:m.name,state:m.state,stateStr:m.stateStr,health:m.health,uptime:m.uptime,optimeDate:m.optimeDate?m.optimeDate.toISOString():null,ping:m.ping,syncSourceHost:m.syncSourceHost,lastHeartbeat:m.lastHeartbeat?m.lastHeartbeat.toISOString():null,lastHeartbeatRecv:m.lastHeartbeatRecv?m.lastHeartbeatRecv.toISOString():null,lastHeartbeatMessage:m.lastHeartbeatMessage});
print(JSON.stringify({set:status.set,term:status.term,ok:status.ok,members:(status.members||[]).map(cleanMember),hello:{primary:hello.primary,isWritablePrimary:hello.isWritablePrimary,setName:hello.setName},defaultWriteConcern:(rw.defaultWriteConcern||null),defaultRWConcern:rw,config:conf.config||null,connections:ss.connections||null,opcounters:ss.opcounters||null,version:ss.version||null,localTime:ss.localTime?ss.localTime.toISOString():null}));
"""


def _member_for(node_name, address, members):
    address_host = address.rsplit(':', 1)[0]
    for member in members or []:
        member_name = str(member.get('name', ''))
        if member_name.startswith(node_name + ':') or member_name.startswith(address_host + ':') or member_name == node_name or member_name == address_host:
            return member
    return {}


def _config_for(node_name, address, config):
    return _member_for(node_name, address, (config or {}).get('members', []))


def _seconds(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def inspect_mongo_node(name: str, address: str):
    row = {'name': name, 'host': address.rsplit(':', 1)[0], 'address': address, 'external_address': mongo_settings.external_nodes.get(name, 'N/A'),
           'ssh': False, 'tcp': False, 'service': 'unknown', 'mongosh': False, 'version': 'N/A', 'role': 'DOWN', 'health': 'N/A',
           'uptime': 'N/A', 'optime': 'N/A', 'optimeDate': None, 'ping': 'N/A', 'syncSourceHost': 'N/A', 'lastHeartbeat': 'N/A',
           'lastHeartbeatRecv': 'N/A', 'lastHeartbeatMessage': 'N/A', 'priority': 'N/A', 'votes': 'N/A', 'hidden': False,
           'arbiterOnly': False, 'lag_seconds': None, 'connections': 'N/A', 'error': '', 'last_seen': time.strftime('%Y-%m-%d %H:%M:%S'), 'raw': None}
    row['tcp'] = tcp_reachable(address)
    try:
        with MongoRemote(row['host']) as remote:
            row['ssh'] = True
            _, service, _ = remote.run(f'systemctl is-active {shlex.quote(mongo_settings.service)} 2>/dev/null || true')
            row['service'] = service or 'unknown'
            _, installed, _ = remote.run('command -v mongosh >/dev/null 2>&1 && echo yes || echo no')
            row['mongosh'] = installed == 'yes'
            if not row['mongosh']:
                row['error'] = 'mongosh no está instalado.'
                return row
            _, version, _ = remote.run('mongosh --version 2>/dev/null || true')
            row['version'] = version.splitlines()[-1] if version else 'N/A'
            if row['service'] != 'active':
                row['error'] = 'mongod está detenido o no está activo.'
                return row
            command = _mongosh_prefix() + ' --eval ' + shlex.quote(READ_SCRIPT)
            code, out, err = remote.run(command, timeout=30)
            if code != 0:
                row['error'] = f'Error consultando rs.status(): {err or "sin detalle"}'
                return row
            try:
                payload = json.loads(out)
            except json.JSONDecodeError:
                row['error'] = 'JSON inválido devuelto por mongosh.'
                return row
            row['raw'] = payload
            member = _member_for(name, address, payload.get('members'))
            config = _config_for(name, address, payload.get('config'))
            row.update({
                'role': member.get('stateStr', 'UNKNOWN'), 'health': member.get('health', 'N/A'), 'uptime': member.get('uptime', 'N/A'),
                'optimeDate': member.get('optimeDate'), 'optime': member.get('optimeDate') or 'N/A', 'ping': member.get('ping', 'N/A'),
                'syncSourceHost': member.get('syncSourceHost', 'N/A'), 'lastHeartbeat': member.get('lastHeartbeat', 'N/A'),
                'lastHeartbeatRecv': member.get('lastHeartbeatRecv', 'N/A'), 'lastHeartbeatMessage': member.get('lastHeartbeatMessage', 'N/A'),
                'priority': config.get('priority', 'N/A'), 'votes': config.get('votes', 'N/A'), 'hidden': config.get('hidden', False),
                'arbiterOnly': config.get('arbiterOnly', False), 'connections': (payload.get('connections') or {}).get('current', 'N/A'),
            })
    except Exception as exc:
        row['error'] = str(exc)
    return row


def _topology(rows):
    payload = next((r.get('raw') for r in rows if r.get('raw')), {}) or {}
    members = payload.get('members', [])
    primary = next((m.get('name') for m in members if m.get('stateStr') == 'PRIMARY'), None)
    primary_time = _seconds(next((m.get('optimeDate') for m in members if m.get('stateStr') == 'PRIMARY'), None))
    for row in rows:
        member = _member_for(row['name'], row['address'], members)
        row['lag_seconds'] = 0 if row['role'] == 'PRIMARY' else (max(0, int(primary_time - _seconds(row['optimeDate']))) if primary_time and _seconds(row['optimeDate']) else None)
        row['lag_level'] = lag_level(row['lag_seconds'])
    config = payload.get('config') or {}
    voting = [m for m in config.get('members', []) if m.get('votes', 1) > 0]
    majority = len(voting) // 2 + 1 if voting else max(1, len(config.get('members', [])) // 2 + 1)
    write_concern = (payload.get('defaultWriteConcern') or {}).get('w', 'N/A')
    primary_label = None
    for row in rows:
        if row['role'] == 'PRIMARY':
            primary_label = row['name']
    return {'replica_set': payload.get('set') or mongo_settings.replica_set, 'member_count': len(config.get('members', members)),
            'writableVotingMembersCount': len(voting), 'majorityVoteCount': majority, 'majority': f'{majority}/{len(voting) or len(rows)}',
            'term': payload.get('term', 'N/A'), 'primary': primary_label or primary, 'last_known_primary': primary_label or 'N/A',
            'defaultWriteConcern': write_concern, 'configured': bool(payload), 'database': mongo_settings.database,
            'connections': (payload.get('connections') or {}).get('current', 'N/A')}


def lag_level(seconds):
    if seconds is None:
        return 'N/A'
    if seconds <= LAG_OK_SECONDS:
        return 'OK'
    if seconds <= LAG_WARNING_SECONDS:
        return 'WARNING'
    return 'CRITICAL'


def classify_mongo(rows, topology):
    available = [r for r in rows if r['ssh'] and r['service'] == 'active' and r['role'] != 'DOWN']
    healthy = [r for r in rows if r['health'] == 1]
    primary_count = len([r for r in rows if r['role'] == 'PRIMARY'])
    secondaries = [r for r in rows if r['role'] == 'SECONDARY']
    required = topology['majorityVoteCount']
    majority_ok = len(healthy) >= required
    write_ok = topology.get('defaultWriteConcern') == 'majority'
    syncing = any(r['role'] in ('STARTUP', 'STARTUP2', 'RECOVERING') for r in rows)
    critical_role = any(r['role'] in ('ROLLBACK',) for r in rows)
    lag_bad = any(r.get('lag_level') == 'CRITICAL' for r in rows if r['role'] == 'SECONDARY')
    if not majority_ok and primary_count == 0:
        return 'DOWN', 'Replica Set sin PRIMARY ni mayoría.'
    if syncing:
        return 'SYNCING', 'Replica Set sincronizando un miembro.'
    if not majority_ok or primary_count != 1 or critical_role or lag_bad:
        return 'CRITICAL', 'Replica Set en estado crítico; requiere intervención manual.'
    if len(available) == len(rows) and primary_count == 1 and len(secondaries) == len(rows) - 1 and len(healthy) == len(rows) and write_ok:
        return 'HEALTHY', 'Replica Set saludable y protegido con majority.'
    return 'DEGRADED', 'Replica Set operativo, pero degradado o sin write concern majority.'


def can_controlled_stepdown(rows, topology, node_name):
    """Pure safety gate, intentionally shared by the HTTP route and unit tests."""
    target = next((row for row in rows if row['name'] == node_name), None)
    healthy_secondaries = [row for row in rows if row['role'] == 'SECONDARY' and row['health'] == 1 and row.get('lag_level') != 'CRITICAL']
    return bool(target and target['role'] == 'PRIMARY' and target['health'] == 1 and len(rows) == 3
                and len([r for r in rows if r['health'] == 1]) >= topology.get('majorityVoteCount', 99)
                and healthy_secondaries)


def mongo_status():
    if not mongo_settings.enabled:
        return {'enabled': False, 'level': 'DISABLED', 'summary': 'MongoDB no está habilitado.', 'nodes': [], 'topology': {}}
    configured = list(mongo_settings.nodes.items())
    if not configured:
        return {'enabled': True, 'level': 'DOWN', 'summary': 'No hay nodos MongoDB configurados.', 'nodes': [], 'topology': {}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(configured)) as pool:
        rows = list(pool.map(lambda item: inspect_mongo_node(*item), configured))
    topology = _topology(rows)
    level, summary = classify_mongo(rows, topology)
    return {'enabled': True, 'level': level, 'summary': summary, 'nodes': rows, 'topology': topology,
            'monitor_interval': mongo_settings.monitor_interval, 'last_updated': time.strftime('%Y-%m-%d %H:%M:%S')}


def _root_remote(address, root_password):
    if not root_password:
        raise MongoError('Debe ingresar la contraseña de root.')
    return MongoRemote(address.rsplit(':', 1)[0], user=mongo_settings.root_ssh_user, password=root_password, use_key=False)


def start_mongod(node, root_password):
    address = mongo_settings.nodes.get(node)
    if not address:
        raise MongoError('Nodo MongoDB no configurado.')
    with _root_remote(address, root_password) as remote:
        code, out, err = remote.run(f'systemctl --no-block start {shlex.quote(mongo_settings.service)}', timeout=15)
    return code == 0, out or err or 'Solicitud de inicio enviada.'


def _admin_action(node, root_password, script):
    address = mongo_settings.nodes.get(node)
    if not address:
        raise MongoError('Nodo MongoDB no configurado.')
    with _root_remote(address, root_password) as remote:
        command = _mongosh_prefix() + ' --eval ' + shlex.quote(script)
        code, out, err = remote.run(command, timeout=45)
    return code == 0, out or err or 'Comando enviado.'


def controlled_stepdown(node, root_password):
    return _admin_action(node, root_password, "db.getSiblingDB('admin').runCommand({replSetStepDown:60,secondaryCatchUpPeriodSecs:10,force:false})")


def set_majority_write_concern(node, root_password):
    return _admin_action(node, root_password, "db.getSiblingDB('admin').runCommand({setDefaultRWConcern:1,defaultWriteConcern:{w:'majority'}})")
