#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fts-cluster-manager"
IMAGE_NAME="fts-cluster-manager:latest"
APP_PORT="6060"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

say(){ printf '%s\n' "$*"; }
ask_yn(){
  local p="$1" a
  while true; do
    read -r -p "$p (y/n): " a
    case "${a:-}" in y|Y) return 0;; n|N) return 1;; *) say "Opcion invalida.";; esac
  done
}

install_systemd_unit(){
  local service_name service_file
  service_name="${APP_NAME}.service"
  service_file="${ROOT_DIR}/${service_name}"
  local engine_bin unit_after
  if [[ "$engine" == "docker" ]]; then
    engine_bin="$(command -v docker)"
    unit_after="docker.service"
  else
    engine_bin="$(command -v podman)"
    unit_after="network-online.target"
  fi

  cat > "$service_file" <<EOF
[Unit]
Description=FTS Cluster Manager container
Wants=network-online.target
After=network-online.target ${unit_after}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${engine_bin} start ${APP_NAME}
ExecStop=${engine_bin} stop --time 30 ${APP_NAME}
TimeoutStartSec=60
TimeoutStopSec=60
Restart=no

[Install]
WantedBy=multi-user.target
EOF
  say "Unidad generada: ${service_file}"

  if ! ask_yn "Desea instalar la unidad en /etc/systemd/system"; then
    say "La unidad quedó generada localmente. No se instaló ni se habilitó."
    return
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    install -m 0644 "$service_file" "/etc/systemd/system/${service_name}"
    systemctl daemon-reload
  elif command -v sudo >/dev/null 2>&1; then
    sudo install -m 0644 "$service_file" "/etc/systemd/system/${service_name}"
    sudo systemctl daemon-reload
  else
    say "No hay privilegios para instalar la unidad. Copia ${service_file} a /etc/systemd/system/ y ejecuta systemctl daemon-reload."
    return
  fi

  say "Unidad instalada. No se ejecutó systemctl enable: HA conserva el control de arranque."
  if [[ "$engine" == "docker" ]]; then
    if ! docker container inspect "$APP_NAME" >/dev/null 2>&1; then
      say "El contenedor todavía no existe; la unidad queda instalada y detenida para HA."
      return
    fi
  elif ! podman container exists "$APP_NAME" >/dev/null 2>&1; then
    say "El contenedor todavía no existe; la unidad queda instalada y detenida para HA."
    return
  fi
  if ask_yn "Desea iniciar la unidad ahora (sin habilitarla al arranque)"; then
    if [[ "${EUID}" -eq 0 ]]; then systemctl start "$service_name"; else sudo systemctl start "$service_name"; fi
    say "Unidad iniciada manualmente. Sigue sin estar habilitada."
  else
    say "Unidad instalada pero detenida; HA puede gestionarla cuando corresponda."
  fi
}

say "==============================================="
say "       INSTALADOR FTS CLUSTER MANAGER"
say "==============================================="
say ""

has_docker=0; has_podman=0
command -v docker >/dev/null 2>&1 && has_docker=1
command -v podman >/dev/null 2>&1 && has_podman=1

if (( has_docker == 0 && has_podman == 0 )); then
  say "ERROR: No se encontro Docker ni Podman instalado."
  exit 1
fi

if (( has_docker == 1 && has_podman == 1 )); then
  say "1) Docker + Docker Compose"
  say "2) Podman puro"
  while true; do
    read -r -p "Seleccione motor [1/2]: " opt
    case "$opt" in 1) engine=docker; break;; 2) engine=podman; break;; *) say "Opcion invalida.";; esac
  done
elif (( has_docker == 1 )); then
  engine=docker
  say "Docker detectado."
else
  engine=podman
  say "Podman detectado."
fi

mkdir -p data secrets docs
chmod 700 secrets

prepare_env(){
  if [[ ! -f .env ]]; then
    cp .env.example .env
    say "Creado .env como copia de .env.example."
  fi
}

if [[ -f .env ]]; then
  say ".env ya existe; no se sobrescribe."
else
  say ".env se creara desde .env.example antes de editarlo."
fi

if [[ -f secrets/id_rsa ]]; then
  say "Ya existe secrets/id_rsa."
  if ask_yn "Desea reemplazar la llave SSH privada de ftsuser?"; then
    read -r -p "Ruta de la llave SSH privada de ftsuser: " key_path
    [[ -f "$key_path" ]] || { say "ERROR: No existe $key_path"; exit 1; }
    cp "$key_path" secrets/id_rsa
    chmod 600 secrets/id_rsa
  fi
else
  if ask_yn "Desea copiar ahora la llave SSH privada de ftsuser?"; then
    read -r -p "Ruta de la llave SSH privada de ftsuser: " key_path
    [[ -f "$key_path" ]] || { say "ERROR: No existe $key_path"; exit 1; }
    cp "$key_path" secrets/id_rsa
    chmod 600 secrets/id_rsa
    say "Llave copiada a secrets/id_rsa"
  fi
fi

if ask_yn "Desea editar .env ahora?"; then
  prepare_env
  editor="vim"
  if ! command -v "$editor" >/dev/null 2>&1; then
    say "ERROR: vim no esta instalado. Instala vim o edita .env manualmente."
    exit 1
  fi
  "$editor" .env
fi

# El contenedor siempre necesita .env, incluso si el usuario decide no editarlo.
prepare_env

if [[ ! -f secrets/id_rsa ]]; then
  say "No se configuro una llave SSH. Verifique que SSH_PASSWORD tenga valor en .env."
else
  chmod 600 secrets/id_rsa
fi

say ""
say "Motor seleccionado: $engine"
say "IMPORTANTE:"
say "- Monitoreo: ftsuser + SSH_PASSWORD; si esta vacio, usa la llave SSH."
say "- Acciones privilegiadas: root + contrasena solicitada en cada operacion."
say "- La contrasena root no se almacena."
say "- Ninguna accion correctiva se ejecuta automaticamente."
say "- La imagen NO usa apt-get ni instala mariadb-client/openssh-client/ping."
say "- MongoDB se consulta con mongosh existente en cada nodo remoto; no se instala en esta imagen."
say ""

build_and_start=0
if ask_yn "Desea construir e iniciar FTS Cluster Manager ahora?"; then
  build_and_start=1
else
  say "No se construirá ni iniciará el contenedor ahora."
fi

if [[ "$build_and_start" -eq 1 && "$engine" == "docker" ]]; then
  if ! docker compose version >/dev/null 2>&1; then
    say "ERROR: Docker Compose no esta disponible."
    exit 1
  fi
  docker compose up -d --build
  docker compose ps || true
elif [[ "$build_and_start" -eq 1 ]]; then
  if podman container exists "$APP_NAME" >/dev/null 2>&1; then
    say "Ya existe el contenedor $APP_NAME."
    if ask_yn "Desea reemplazarlo?"; then
      podman stop "$APP_NAME" >/dev/null 2>&1 || true
      podman rm "$APP_NAME" >/dev/null 2>&1 || true
    else
      say "Cancelado para no modificar el contenedor existente."
      exit 0
    fi
  fi

  say "Construyendo imagen con Podman..."
  podman build --pull=true -t "$IMAGE_NAME" .

  say "Iniciando contenedor con Podman..."
  podman_volumes=(
    -v "${ROOT_DIR}/data:/app/data:Z"
    -v "${ROOT_DIR}/docs:/app/docs:ro,Z"
  )
  if [[ -f "${ROOT_DIR}/secrets/id_rsa" ]]; then
    podman_volumes+=(-v "${ROOT_DIR}/secrets/id_rsa:/run/secrets/ssh_key:ro,Z")
  fi
  podman run -d \
    --name "$APP_NAME" \
    --restart=no \
    -p "${APP_PORT}:8080" \
    --env-file .env \
    "${podman_volumes[@]}" \
    "$IMAGE_NAME"

  podman ps --filter "name=$APP_NAME" || true
fi

if ask_yn "Desea generar la unidad systemd para FTS Cluster Manager"; then
  install_systemd_unit
fi

server_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
say ""
say "==============================================="
say "       INSTALACION FINALIZADA"
say "==============================================="
if [[ -n "${server_ip:-}" ]]; then
  say "Aplicacion: http://${server_ip}:${APP_PORT}"
else
  say "Aplicacion: http://IP_DEL_SERVIDOR:${APP_PORT}"
fi
say ""
if [[ "$engine" == "podman" ]]; then
  say "Comandos utiles:"
  say "  podman ps"
  say "  podman logs -f $APP_NAME"
  say "  podman restart $APP_NAME"
  say "  podman stop $APP_NAME"
  say "  podman start $APP_NAME"
fi
