# FTS Cluster Manager

Aplicación web para monitoreo y operación **manual** de MariaDB Galera + MongoDB Replica Set.

## Flujo actual

La recuperación está integrada directamente en el Dashboard:

1. Si existe un Primary activo, permite incorporar nodos faltantes uno por uno.
2. Si todos los MariaDB accesibles están detenidos, permite ejecutar manualmente `wsrep-recover`, revisar UUID/SEQNO, seleccionar el Primary y ejecutar el bootstrap con confirmaciones.
3. Después de cada acción vuelve a validar los estados del servicio y Galera.
4. Ninguna acción correctiva se ejecuta automáticamente.

MongoDB Replica Set se monitoriza por SSH ejecutando `mongosh` localmente en cada servidor. La vista dedicada detecta PRIMARY/SECONDARY, mayoría, write concern, lag y estado de cada miembro. Sólo permite iniciar un `mongod` detenido, stepdown controlado y configurar `w: majority`, siempre con contraseña root, confirmación escrita y auditoría.

## Acceso

Puerto externo predeterminado:

```text
http://IP_DEL_SERVIDOR:6060
```

## Instalación con Podman

```bash
chmod +x install.sh
bash install.sh
```

El instalador usa Podman puro (`podman build` y `podman run`) y `vim` para editar `.env`.

## Credenciales

- SSH de monitoreo: `ftsuser` + llave privada.
- Acciones privilegiadas: `root` + contraseña solicitada en cada operación.
- MariaDB: valores `MYSQL_USER` y `MYSQL_PASSWORD` de `.env` para las consultas de estado.

La contraseña root de Linux nunca se almacena.

## Ayuda

La documentación visible desde el panel **Ayuda** se encuentra en:

```text
docs/AYUDA.md
```

Incluye el diagrama Mermaid del flujo de operación.


## Interfaz simplificada

El Dashboard no expone opciones avanzadas de detener o reiniciar MariaDB. Sólo permite incorporar nodos detenidos cuando existe un Primary saludable y ejecutar el flujo manual de recuperación cuando todos los MariaDB están inactivos.

## MongoDB

Configure `MONGO_*` en `.env` usando el bloque de `.env.example`. La URI para aplicaciones se muestra en `/mongodb`; usa el Replica Set y soporta failover. Las URI `directConnection=true` son sólo para inspección administrativa puntual.
