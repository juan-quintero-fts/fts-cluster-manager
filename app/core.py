from __future__ import annotations
import os, socket, shlex, time
from dataclasses import dataclass
from typing import Optional
import paramiko

@dataclass
class Settings:
    app_name: str = os.getenv('APP_NAME', 'FTS Galera Manager')
    # Usuario operativo de solo lectura / monitoreo.
    ssh_user: str = os.getenv('SSH_USER', 'ftsuser')
    ssh_port: int = int(os.getenv('SSH_PORT', '22'))
    ssh_key_path: str = os.getenv('SSH_KEY_PATH', '/run/secrets/ssh_key')
    ssh_password: str = os.getenv('SSH_PASSWORD', '')
    # Usuario privilegiado. La contraseña NUNCA se configura aquí: se solicita por operación.
    root_ssh_user: str = os.getenv('ROOT_SSH_USER', 'root')
    mysql_user: str = os.getenv('MYSQL_USER', 'root')
    mysql_password: str = os.getenv('MYSQL_PASSWORD', '')
    grastate: str = os.getenv('MYSQL_GRASTATE', '/ftscluster/mysql/grastate.dat')
    expected_cluster_size: int = int(os.getenv('EXPECTED_CLUSTER_SIZE', '3'))
    monitor_interval: int = max(5, int(os.getenv('MONITOR_INTERVAL', '10')))
    auto_monitor: bool = os.getenv('AUTO_MONITOR', 'true').strip().lower() in {'1','true','yes','on'}
    @property
    def nodes(self):
        return [x.strip() for x in os.getenv('NODES', '172.16.0.1,172.16.0.2,172.16.0.3').split(',') if x.strip()]

settings = Settings()

class SSHError(RuntimeError):
    pass

class SSHAuthenticationError(SSHError):
    """La conexión SSH alcanzó el nodo, pero rechazó las credenciales."""
    pass

class Remote:
    """Conexión SSH.

    Por defecto usa el usuario operativo (ftsuser) con la contraseña configurada
    o, si está vacía, con su llave. Para una operación privilegiada se pasa
    user='root' y password=<contraseña ingresada en el formulario>. Esa contraseña
    vive únicamente durante la petición.
    """
    def __init__(self, host: str, user: Optional[str] = None, password: Optional[str] = None, use_key: bool = True):
        self.host = host
        self.user = user or settings.ssh_user
        self.password = password
        self.use_key = use_key
        self.client = None

    def __enter__(self):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host,
            port=settings.ssh_port,
            username=self.user,
            timeout=5,
            banner_timeout=5,
            auth_timeout=8,
            allow_agent=False,
            look_for_keys=False,
        )
        if self.password is not None:
            # Conexión privilegiada: autenticación por contraseña escrita por el usuario.
            kwargs['password'] = self.password
        elif settings.ssh_password:
            # Monitoreo: una contraseña configurada tiene prioridad sobre la llave.
            kwargs['password'] = settings.ssh_password
        elif self.use_key and os.path.isfile(settings.ssh_key_path):
            kwargs['key_filename'] = settings.ssh_key_path
        try:
            c.connect(**kwargs)
        except paramiko.AuthenticationException as e:
            raise SSHAuthenticationError(
                f'Autenticación SSH rechazada para {self.user} en {self.host}.'
            ) from e
        except Exception as e:
            raise SSHError(f'No fue posible autenticar SSH como {self.user} en {self.host}: {e}')
        self.client = c
        return self

    def __exit__(self, *args):
        if self.client:
            self.client.close()

    def run(self, command: str, timeout: int = 30):
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors='replace').strip()
        err = stderr.read().decode(errors='replace').strip()
        return code, out, err


def root_remote(host: str, root_password: str) -> Remote:
    if not root_password:
        raise SSHError('Debe ingresar la contraseña de root para ejecutar esta operación.')
    return Remote(host, user=settings.root_ssh_user, password=root_password, use_key=False)


def tcp_reachable(host: str, port: int, timeout=1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mysql_command(sql: str) -> str:
    # MYSQL_PWD evita incluir -pPASSWORD en los argumentos del proceso remoto.
    return f"MYSQL_PWD={shlex.quote(settings.mysql_password)} mysql -u{shlex.quote(settings.mysql_user)} -Nse {shlex.quote(sql)}"


def inspect_node(host: str):
    """Monitoreo de solo lectura usando las credenciales SSH configuradas."""
    row = {
        'host': host, 'ssh': False, 'mariadb': 'unknown', 'cluster': 'N/A', 'ready': 'N/A',
        'local_state': 'N/A', 'size': 'N/A', 'connected': 'N/A', 'wsrep_local_index': 'N/A',
        'flow_control_paused': 'N/A', 'recv_queue': 'N/A', 'send_queue': 'N/A', 'error': '',
        'syncing': False, 'last_seen': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    row['ssh'] = tcp_reachable(host, settings.ssh_port)
    if not row['ssh']:
        row['error'] = 'Puerto SSH no accesible'
        return row
    try:
        with Remote(host) as r:
            _, state, _ = r.run('systemctl is-active mariadb 2>/dev/null || true')
            row['mariadb'] = state or 'unknown'
            row['syncing'] = state in ('activating', 'reloading')
            if state != 'active':
                return row
            sql = "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_ready','wsrep_local_state_comment','wsrep_cluster_size','wsrep_connected','wsrep_local_index','wsrep_flow_control_paused','wsrep_local_recv_queue','wsrep_local_send_queue');"
            code, out, err = r.run(mysql_command(sql))
            if code != 0 or not out:
                row['error'] = err or 'No fue posible consultar wsrep'
                error_text = row['error'].lower()
                row['syncing'] = (
                    'wsrep has not yet prepared node for application use' in error_text
                    or 'error 1047' in error_text
                )
                return row
            values = {}
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    values[parts[0]] = parts[1]
            row.update({
                'cluster': values.get('wsrep_cluster_status', 'N/A'),
                'ready': values.get('wsrep_ready', 'N/A'),
                'local_state': values.get('wsrep_local_state_comment', 'N/A'),
                'size': values.get('wsrep_cluster_size', 'N/A'),
                'connected': values.get('wsrep_connected', 'N/A'),
                'wsrep_local_index': values.get('wsrep_local_index', 'N/A'),
                'flow_control_paused': values.get('wsrep_flow_control_paused', 'N/A'),
                'recv_queue': values.get('wsrep_local_recv_queue', 'N/A'),
                'send_queue': values.get('wsrep_local_send_queue', 'N/A'),
            })
            row['syncing'] = not (
                row['local_state'] == 'Synced'
                and row['ready'] in ('ON', '1')
                and row['connected'] in ('ON', '1')
            )
    except Exception as e:
        row['error'] = str(e)
    return row


def classify(nodes):
    ssh_up = [n for n in nodes if n['ssh']]
    maria_up = [n for n in nodes if n['mariadb'] == 'active']
    syncing = [n for n in nodes if n.get('syncing')]
    healthy = [n for n in nodes if n['cluster'] == 'Primary' and n['ready'] in ('ON','1') and n['connected'] in ('ON','1') and n['local_state'] == 'Synced']
    expected = settings.expected_cluster_size
    if len(healthy) == expected and all(str(n['size']) == str(expected) for n in healthy):
        return 'healthy', 'Clúster saludable'
    if syncing:
        return 'degraded', 'Sincronización de Galera en progreso'
    if maria_up and any(n['cluster'] == 'Primary' for n in maria_up):
        return 'degraded', 'Clúster operativo pero degradado'
    if maria_up:
        return 'nonprimary', 'MariaDB activo sin Primary Component'
    if ssh_up:
        return 'down', 'Todos los MariaDB accesibles están detenidos'
    return 'offline', 'Ningún nodo es accesible por SSH'


def recover_position(host: str, root_password: str):
    """Diagnóstico manual. Se autentica como root y no conserva la contraseña."""
    grastate = shlex.quote(settings.grastate)
    inner = f'''set -u
host=$(hostname -s)
BIN=$(command -v mariadbd || command -v mysqld || true)
if [ -z "$BIN" ]; then echo "$host|N/A|N/A|none"; exit 0; fi
out=$(runuser -u mysql -- "$BIN" --wsrep-recover 2>&1 || true)
pos=$(printf "%s\n" "$out" | awk -F"position: " '/Recovered position/ {{p=$2}} END{{print p}}')
if [ -n "$pos" ] && [ "$pos" != "${{pos%%:*}}" ]; then
  echo "$host|${{pos%%:*}}|${{pos##*:}}|wsrep-recover"
  exit 0
fi
g={grastate}
if [ -f "$g" ]; then
  u=$(awk -F": *" '/^uuid:/ {{print $2; exit}}' "$g")
  s=$(awk -F": *" '/^seqno:/ {{print $2; exit}}' "$g")
  echo "$host|${{u:-N/A}}|${{s:-N/A}}|grastate.dat"
else
  echo "$host|N/A|N/A|none"
fi'''
    cmd = "bash -lc " + shlex.quote(inner)
    with root_remote(host, root_password) as r:
        code, out, err = r.run(cmd, timeout=120)
    if code != 0 and not out:
        raise SSHError(err or 'wsrep-recover falló')
    line = out.splitlines()[-1] if out else 'N/A|N/A|N/A|none'
    parts = line.split('|')
    while len(parts) < 4:
        parts.append('N/A')
    return {'host': host, 'hostname': parts[0], 'uuid': parts[1], 'seqno': parts[2], 'source': parts[3]}


def service_action(host: str, action: str, root_password: str):
    """Inicia/incorpora MariaDB por SSH root + contraseña ingresada en ese momento."""
    if action != 'start':
        raise ValueError('Sólo se permite iniciar/incorporar nodos')
    with root_remote(host, root_password) as r:
        # Galera puede tardar bastante durante SST/IST. Se encola el inicio y el
        # dashboard sigue su progreso mediante el monitoreo de systemd y wsrep.
        code, out, err = r.run('systemctl --no-block start mariadb.service', timeout=15)
        return code == 0, out or err


def bootstrap(host: str, root_password: str):
    """Modifica grastate/safe_to_bootstrap y ejecuta galera_new_cluster como root."""
    g = shlex.quote(settings.grastate)
    inner = f'''set -e
test -f {g}
cp -a {g} {g}.bak.$(date +%F_%H%M%S)
if grep -q '^safe_to_bootstrap:' {g}; then
  sed -i 's/^safe_to_bootstrap:.*/safe_to_bootstrap: 1/' {g}
else
  echo 'safe_to_bootstrap: 1' >> {g}
fi
galera_new_cluster'''
    command = f"bash -lc {shlex.quote(inner)}"
    with root_remote(host, root_password) as r:
        code, out, err = r.run(command, timeout=120)
        return code == 0, out or err


def promote_single_nonprimary(host: str, root_password: str):
    """Manual recovery for the sole running node without Primary Component.

    This is intentionally never called by monitoring. The HTTP route validates
    that it is the only running MariaDB and requires a typed confirmation.
    """
    with root_remote(host, root_password) as r:
        code, out, err = r.run('systemctl stop mariadb.service', timeout=90)
        if code != 0:
            return False, out or err or 'No fue posible detener MariaDB para la promoción manual.'
    return bootstrap(host, root_password)
