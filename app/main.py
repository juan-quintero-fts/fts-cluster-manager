from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import urlencode
import markdown
import re

from .core import settings, inspect_node, classify, recover_position, service_action, bootstrap, promote_single_nonprimary, SSHAuthenticationError
from .audit import log, recent, init_db
from .mongo_core import (mongo_settings, mongo_status, start_mongod, controlled_stepdown,
                         set_majority_write_concern, MongoError, MongoSSHAuthenticationError)
from .mongo_core import can_controlled_stepdown
from .pacemaker_core import pacemaker_settings, pacemaker_status

app = FastAPI(title=settings.app_name)
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')
init_db()


def ctx(request, **extra):
    base = {
        'request': request,
        'app_name': settings.app_name,
        'ssh_user': settings.ssh_user,
        'root_ssh_user': settings.root_ssh_user,
        'nodes_cfg': settings.nodes,
        'monitor_interval': settings.monitor_interval,
        'auto_monitor': settings.auto_monitor,
        'mongo_enabled': mongo_settings.enabled,
    }
    base.update(extra)
    return base


def get_cluster_state():
    nodes = [inspect_node(h) for h in settings.nodes]
    level, summary = classify(nodes)
    return nodes, level, summary


def get_mongo_state():
    """MongoDB failures are deliberately isolated from the Galera dashboard."""
    try:
        return mongo_status()
    except Exception as exc:
        return {'enabled': mongo_settings.enabled, 'level': 'DOWN', 'summary': f'Error consultando MongoDB: {exc}', 'nodes': [], 'topology': {}}


def get_pacemaker_state():
    """A Pacemaker/QDevice failure must never break the main Dashboard."""
    try:
        return pacemaker_status()
    except Exception as exc:
        return {
            'enabled': pacemaker_settings.enabled,
            'level': 'DOWN',
            'summary': f'Error consultando Pacemaker/Corosync: {exc}',
            'nodes': [],
            'qnetd': {'name': pacemaker_settings.qnetd_name, 'host': pacemaker_settings.qnetd_host,
                      'ssh': False, 'qnetd': 'unknown', 'pcsd': 'unknown', 'error': str(exc)},
            'details': {
                'pcs': {'dc': 'N/A', 'resources': [], 'resource_owner': 'N/A', 'resource_nodes': []},
                'quorum': {'quorate': 'N/A', 'total_votes': 'N/A', 'quorum': 'N/A'},
                'qdevice': {'state': 'N/A', 'host': 'N/A', 'algorithm': 'N/A'},
                'device_votes': 'N/A',
                'no_quorum_policy': 'N/A',
            },
        }


def dashboard_context(request: Request, positions=None, best=None, uuid_warning=False, feedback=None):
    nodes, level, summary = get_cluster_state()
    ssh_up = [n for n in nodes if n['ssh']]
    maria_up = [n for n in nodes if n['mariadb'] == 'active']
    maria_down_accessible = [n for n in nodes if n['ssh'] and n['mariadb'] in ('inactive', 'failed')]
    primary_nodes = [n for n in maria_up if n['cluster'] == 'Primary']
    # Un único MariaDB activo sin Primary Component no puede aceptar escrituras
    # de forma segura. Se expone como advertencia visual, sin tomar acciones.
    single_node_read_only = len(maria_up) == 1 and not primary_nodes
    all_mariadb_down = len(ssh_up) > 0 and len(maria_down_accessible) == len(ssh_up)
    can_join_nodes = len(primary_nodes) > 0 and len(maria_down_accessible) > 0

    return ctx(
        request,
        nodes=nodes,
        level=level,
        summary=summary,
        expected=settings.expected_cluster_size,
        ssh_up_count=len(ssh_up),
        maria_up_count=len(maria_up),
        primary_nodes=primary_nodes,
        maria_down_accessible=maria_down_accessible,
        all_mariadb_down=all_mariadb_down,
        can_join_nodes=can_join_nodes,
        single_node_read_only=single_node_read_only,
        positions=positions,
        best=best,
        uuid_warning=uuid_warning,
        feedback=feedback, mongo=get_mongo_state(), pacemaker=get_pacemaker_state(),
    )


@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request):
    """Compact infrastructure overview; operational detail lives in its module."""
    return templates.TemplateResponse('overview.html', dashboard_context(request))


@app.get('/galera', response_class=HTMLResponse)
def galera_dashboard(request: Request, event: str = '', host: str = '', ok: str = ''):
    feedback = None
    if event and host:
        feedback = {'event': event, 'host': host, 'ok': ok == '1'}
    return templates.TemplateResponse('dashboard.html', dashboard_context(request, feedback=feedback))


@app.get('/api/status')
def api_status():
    """Monitoreo de solo lectura. Nunca ejecuta acciones correctivas."""
    nodes, level, summary = get_cluster_state()
    ssh_up = [n for n in nodes if n['ssh']]
    maria_up = [n for n in nodes if n['mariadb'] == 'active']
    primary_nodes = [n for n in maria_up if n['cluster'] == 'Primary']
    return JSONResponse({
        'level': level,
        'summary': summary,
        'expected': settings.expected_cluster_size,
        'ssh_up_count': len(ssh_up),
        'maria_up_count': len(maria_up),
        'has_primary': len(primary_nodes) > 0,
        'single_node_read_only': len(maria_up) == 1 and not primary_nodes,
        'nodes': nodes,
    })


@app.get('/mongodb', response_class=HTMLResponse)
def mongodb_page(request: Request):
    return templates.TemplateResponse('mongodb.html', ctx(request, mongo=get_mongo_state(), mongo_interval=mongo_settings.monitor_interval))


@app.get('/api/mongodb/status')
def api_mongodb_status():
    return JSONResponse(get_mongo_state())


@app.get('/pacemaker', response_class=HTMLResponse)
def pacemaker_page(request: Request):
    return templates.TemplateResponse(
        'pacemaker.html',
        ctx(request, pacemaker=get_pacemaker_state(), pacemaker_interval=pacemaker_settings.monitor_interval),
    )


@app.get('/api/pacemaker/status')
def api_pacemaker_status():
    """Endpoint strictly limited to read-only Pacemaker/Corosync monitoring."""
    return JSONResponse(get_pacemaker_state())


def _mongo_node_or_404(node):
    if node not in mongo_settings.nodes:
        raise HTTPException(404, 'Nodo MongoDB no configurado.')


@app.post('/mongodb/node/{node}/service')
def mongodb_node_service(node: str, action: str = Form(...), actor: str = Form('web'), root_password: str = Form(...)):
    _mongo_node_or_404(node)
    if action != 'start':
        raise HTTPException(400, 'Sólo se permite iniciar mongod.')
    state = get_mongo_state()
    target = next((row for row in state.get('nodes', []) if row['name'] == node), None)
    topology = state.get('topology', {})
    if not target or not target['ssh'] or target['service'] not in ('inactive', 'failed'):
        raise HTTPException(409, 'El nodo debe estar accesible por SSH y mongod detenido o fallido.')
    # Cuando dos miembros están detenidos, MongoDB puede retirar el PRIMARY al
    # perder mayoría. Aun así, iniciar manualmente un miembro configurado es la
    # acción no destructiva necesaria para recuperar el quórum. Esta ruta nunca
    # hace restart, stepdown, reconfiguración ni inicia nada automáticamente.
    try:
        ok, detail = start_mongod(node, root_password)
    except MongoSSHAuthenticationError:
        log(actor, node, 'mongodb:start', False, 'Autenticación SSH rechazada.')
        raise HTTPException(401, 'Autenticación SSH rechazada.')
    except MongoError as exc:
        log(actor, node, 'mongodb:start', False, str(exc))
        raise HTTPException(400, str(exc))
    after = get_mongo_state()
    result = {'ok': ok, 'detail': detail, 'status': after}
    log(actor, node, 'mongodb:start', ok, f'{detail}\nValidación posterior solicitada; estado={after.get("level")}')
    return JSONResponse(result)


def _safe_primary_state(node):
    state = get_mongo_state()
    rows, topology = state.get('nodes', []), state.get('topology', {})
    target = next((row for row in rows if row['name'] == node), None)
    if not target or target['role'] != 'PRIMARY' or target['health'] != 1:
        raise HTTPException(409, 'La operación sólo está permitida en el PRIMARY confirmado.')
    if not can_controlled_stepdown(rows, topology, node):
        raise HTTPException(409, 'No se cumplen las condiciones seguras: mayoría y SECONDARY saludable requeridos.')
    return state


@app.post('/mongodb/stepdown/{node}')
def mongodb_stepdown(node: str, confirm: str = Form(...), actor: str = Form('web'), root_password: str = Form(...)):
    _mongo_node_or_404(node)
    if confirm != 'CAMBIAR PRIMARY':
        raise HTTPException(400, 'Debe escribir CAMBIAR PRIMARY.')
    _safe_primary_state(node)
    try:
        ok, detail = controlled_stepdown(node, root_password)
    except MongoSSHAuthenticationError:
        log(actor, node, 'mongodb:stepdown', False, 'Autenticación SSH rechazada.')
        raise HTTPException(401, 'Autenticación SSH rechazada.')
    except MongoError as exc:
        log(actor, node, 'mongodb:stepdown', False, str(exc))
        raise HTTPException(400, str(exc))
    after = get_mongo_state()
    log(actor, node, 'mongodb:stepdown', ok, f'{detail}\nPRIMARY posterior={after.get("topology", {}).get("primary", "N/A")}')
    return JSONResponse({'ok': ok, 'detail': detail, 'status': after})


@app.post('/mongodb/write-concern/majority')
def mongodb_set_majority(node: str = Form(...), confirm: str = Form(...), actor: str = Form('web'), root_password: str = Form(...)):
    _mongo_node_or_404(node)
    if confirm != 'CONFIGURAR MAJORITY':
        raise HTTPException(400, 'Debe escribir CONFIGURAR MAJORITY.')
    _safe_primary_state(node)
    try:
        ok, detail = set_majority_write_concern(node, root_password)
    except MongoSSHAuthenticationError:
        log(actor, node, 'mongodb:set-majority', False, 'Autenticación SSH rechazada.')
        raise HTTPException(401, 'Autenticación SSH rechazada.')
    except MongoError as exc:
        log(actor, node, 'mongodb:set-majority', False, str(exc))
        raise HTTPException(400, str(exc))
    after = get_mongo_state()
    verified = after.get('topology', {}).get('defaultWriteConcern') == 'majority'
    log(actor, node, 'mongodb:set-majority', ok and verified, f'{detail}\nVerificación getDefaultRWConcern={after.get("topology", {}).get("defaultWriteConcern", "N/A")}')
    return JSONResponse({'ok': ok and verified, 'detail': detail, 'status': after})


@app.get('/recovery')
def recovery_redirect():
    # La recuperación quedó integrada al Dashboard.
    return RedirectResponse('/galera#recovery-panel', status_code=303)


@app.post('/recovery/analyze', response_class=HTMLResponse)
def analyze_recovery(request: Request, root_password: str = Form(...)):
    nodes, _, _ = get_cluster_state()
    positions = []

    # El diagnóstico sólo aplica a nodos accesibles con MariaDB detenido.
    for n in nodes:
        if n['ssh'] and n['mariadb'] in ('inactive', 'failed'):
            try:
                positions.append(recover_position(n['host'], root_password))
            except SSHAuthenticationError:
                positions.append({
                    'host': n['host'], 'hostname': 'N/A', 'uuid': 'N/A',
                    'seqno': 'N/A', 'source': 'Autenticación SSH rechazada. Verifica la contraseña de root.'
                })
            except Exception as e:
                positions.append({
                    'host': n['host'], 'hostname': 'N/A', 'uuid': 'N/A',
                    'seqno': 'N/A', 'source': str(e)
                })

    numeric = [
        p for p in positions
        if str(p['seqno']).lstrip('-').isdigit() and int(p['seqno']) >= 0
    ]
    best = max([int(p['seqno']) for p in numeric], default=None)
    uuids = sorted({p['uuid'] for p in numeric if p['uuid'] != 'N/A'})
    uuid_warning = len(uuids) > 1

    return templates.TemplateResponse(
        'dashboard.html',
        dashboard_context(
            request,
            positions=positions,
            best=best,
            uuid_warning=uuid_warning,
        ),
    )


@app.post('/node/{host}/service')
def node_service(
    host: str,
    action: str = Form(...),
    actor: str = Form('web'),
    root_password: str = Form(...),
):
    if host not in settings.nodes:
        raise HTTPException(404)
    if action != 'start':
        raise HTTPException(400, 'Sólo se permite iniciar/incorporar nodos desde esta operación')

    # Para incorporar un nodo detenido debe existir previamente un Primary activo.
    if action == 'start':
        current_nodes, _, _ = get_cluster_state()
        target = next((n for n in current_nodes if n['host'] == host), None)
        has_primary = any(
            n['mariadb'] == 'active' and n['cluster'] == 'Primary'
            for n in current_nodes
        )
        if not has_primary:
            raise HTTPException(409, 'No se puede incorporar el nodo porque no existe un Primary activo. Use primero el flujo de recuperación del Dashboard.')
        if not target or not target['ssh']:
            raise HTTPException(409, 'El nodo no está accesible por SSH.')
        if target['mariadb'] not in ('inactive', 'failed'):
            raise HTTPException(
                409,
                f'No se puede iniciar el nodo porque MariaDB está en estado {target["mariadb"]}. '
                'Espere a que termine el inicio o la sincronización.',
            )

    try:
        ok, detail = service_action(host, action, root_password)
    except SSHAuthenticationError:
        log(actor, host, f'mariadb:{action}', False, 'Autenticación SSH rechazada.')
        query = urlencode({'event': 'auth_failed', 'host': host, 'ok': '0'})
        return RedirectResponse(f'/galera?{query}', status_code=303)

    # Validación posterior de estado. No ejecuta ninguna acción adicional.
    after = inspect_node(host)
    detail = (
        f'{detail}\n'
        f'VALIDACIÓN POSTERIOR: MariaDB={after["mariadb"]}, '
        f'Cluster={after["cluster"]}, Ready={after["ready"]}, '
        f'Connected={after["connected"]}, State={after["local_state"]}, Size={after["size"]}'
    )
    # --no-block confirma que systemd aceptó el inicio. La sincronización y el
    # estado Synced se validan después mediante las consultas periódicas.
    action_ok = ok
    log(actor, host, f'mariadb:{action}', action_ok, detail)

    query = urlencode({'event': action, 'host': host, 'ok': '1' if action_ok else '0'})
    return RedirectResponse(f'/galera?{query}', status_code=303)


@app.post('/recovery/bootstrap/{host}')
def do_bootstrap(
    host: str,
    confirm: str = Form(...),
    actor: str = Form('web'),
    root_password: str = Form(...),
):
    if host not in settings.nodes:
        raise HTTPException(404)
    if confirm != 'RECUPERAR':
        raise HTTPException(400, 'Debe escribir RECUPERAR')

    # Protección: el bootstrap sólo está permitido cuando todos los MariaDB accesibles
    # están detenidos. Si existe un MariaDB activo, el operador debe revisar ese estado.
    current_nodes, _, _ = get_cluster_state()
    target = next((n for n in current_nodes if n['host'] == host), None)
    if not target or not target['ssh']:
        raise HTTPException(409, 'El nodo seleccionado no está accesible por SSH.')
    nodes_not_stopped = [
        n for n in current_nodes
        if n['ssh'] and n['mariadb'] not in ('inactive', 'failed')
    ]
    if nodes_not_stopped:
        raise HTTPException(
            409,
            'El bootstrap se bloquea porque existe al menos un MariaDB activo, '
            'iniciando o sincronizando.',
        )

    # Sólo bootstrap manual. Nunca se llama desde monitoreo ni en segundo plano.
    try:
        ok, detail = bootstrap(host, root_password)
    except SSHAuthenticationError:
        log(actor, host, 'galera:bootstrap', False, 'Autenticación SSH rechazada.')
        query = urlencode({'event': 'auth_failed', 'host': host, 'ok': '0'})
        return RedirectResponse(f'/galera?{query}', status_code=303)

    # Validar que el nodo quedó realmente como Primary y listo para operar.
    after = inspect_node(host)
    bootstrap_ok = (
        ok
        and after['mariadb'] == 'active'
        and after['cluster'] == 'Primary'
        and after['ready'] in ('ON', '1')
        and after['connected'] in ('ON', '1')
        and after['local_state'] == 'Synced'
    )
    detail = (
        f'{detail}\n'
        f'VALIDACIÓN POSTERIOR: MariaDB={after["mariadb"]}, '
        f'Cluster={after["cluster"]}, Ready={after["ready"]}, '
        f'Connected={after["connected"]}, State={after["local_state"]}, Size={after["size"]}'
    )
    log(actor, host, 'galera:bootstrap', bootstrap_ok, detail)

    query = urlencode({'event': 'bootstrap', 'host': host, 'ok': '1' if bootstrap_ok else '0'})
    return RedirectResponse(f'/galera?{query}', status_code=303)


@app.post('/recovery/promote/{host}')
def promote_single_node(
    host: str,
    confirm: str = Form(...),
    actor: str = Form('web'),
    root_password: str = Form(...),
):
    if host not in settings.nodes:
        raise HTTPException(404)
    if confirm != 'PROMOVER PRIMARY':
        raise HTTPException(400, 'Debe escribir PROMOVER PRIMARY')
    nodes, _, _ = get_cluster_state()
    active = [node for node in nodes if node['mariadb'] == 'active']
    target = next((node for node in active if node['host'] == host), None)
    if len(active) != 1 or not target or not target['ssh'] or target['cluster'] == 'Primary':
        raise HTTPException(409, 'La promoción sólo permite un único MariaDB activo, accesible y sin Primary Component.')
    try:
        ok, detail = promote_single_nonprimary(host, root_password)
    except SSHAuthenticationError:
        log(actor, host, 'galera:promote-primary', False, 'Autenticación SSH rechazada.')
        raise HTTPException(401, 'Autenticación SSH rechazada.')
    after = inspect_node(host)
    verified = ok and after['mariadb'] == 'active' and after['cluster'] == 'Primary'
    log(actor, host, 'galera:promote-primary', verified,
        f'{detail}\nVALIDACIÓN POSTERIOR: MariaDB={after["mariadb"]}, Cluster={after["cluster"]}, State={after["local_state"]}')
    query = urlencode({'event': 'bootstrap', 'host': host, 'ok': '1' if verified else '0'})
    return RedirectResponse(f'/galera?{query}', status_code=303)


@app.get('/audit', response_class=HTMLResponse)
def audit(request: Request):
    return templates.TemplateResponse('audit.html', ctx(request, rows=recent()))


@app.get('/help', response_class=HTMLResponse)
def help_page(request: Request):
    raw = open('docs/AYUDA.md', encoding='utf-8').read()
    mermaids = []

    def stash(m):
        mermaids.append(m.group(1).strip())
        return f'@@MERMAID_{len(mermaids)-1}@@'

    tmp = re.sub(r'```mermaid\s*(.*?)```', stash, raw, flags=re.S)
    html = markdown.markdown(tmp, extensions=['tables', 'fenced_code', 'toc'])
    for i, code in enumerate(mermaids):
        html = html.replace(f'@@MERMAID_{i}@@', f'<div class="mermaid">{code}</div>')
    return templates.TemplateResponse('help.html', ctx(request, content=html))
