"""
Pasada «todo lo de Red.es»: baja de DEHÚ todo lo que Red.es ha enviado al
titular del certificado y lo entrega a AiKit, sin que nadie elija.

Qué sale de este ordenador y a dónde (no hay más destinos que estos dos):
  1. DEHÚ (redsara.es): las mismas llamadas que la pantalla manual.
  2. GESTOR_URL: los ZIP de Red.es tal como los devuelve DEHÚ, con su
     meta.json, el usuario y la contraseña del gestor para entrar, y la
     lista de notificaciones de Red.es que siguen pendientes sin aceptar
     (solo los datos del listado de DEHÚ; no se abren).
El certificado y su contraseña no se envían a ningún sitio.

Qué queda en este ordenador (carpeta outputs/auto):
  por_subir/     ZIP bajados que aún no se han entregado
  entregado/     copia de los ZIP ya entregados
  registro.json  estado de cada envío (para no repetir y para reintentar)
  enviado.log    una línea por cada fichero enviado: cuándo, a dónde, sha256
  pendientes_informadas.json  la última lista de pendientes enviada, tal cual

Documento para revisión: REVISION_IT.md.
"""

import base64
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile

import requests

from descarga import (
    es_red_es, construir_meta, anadir_meta_a_zip, aceptar_con_reintento, guardar_documento,
)

# Único destino de AiKit. Es una constante a propósito: no se puede cambiar
# desde la configuración.
GESTOR_URL = 'https://clasificador-dehu-v2-production.up.railway.app'

MAX_POR_PASADA = 998       # mismo tope que la pantalla manual
# DEHÚ rechaza una «Fecha Desde» de más de 30 días (error 4224) y la pasada
# pide desde las 00:00: con 30 el listado entero falla (visto con el DEHÚ real
# el 18-11-2026). 29 días a las 00:00 cabe siempre.
DIAS_REALIZADAS = 29
MAX_RESPUESTAS_NEGATIVAS = 3   # tras 3 «no» de DEHÚ a un envío se deja de pedir
# 4225 («máximo de consultas») es de cada envío: DEHÚ admite un número limitado
# de consultas por envío. Ese envío cuenta un «no» y la pasada sigue; solo si
# llegan varios seguidos se para, por si el tope fuera de todo el certificado.
MAX_4225_SEGUIDOS = 5
# 4305: tope diario de PeticionAcceso del certificado. No es un «no» del envío:
# se deja de abrir pendientes hasta la próxima pasada y se sigue con lo realizado.
CODIGO_TOPE_DIARIO_ACCESO = '4305'
TAMANO_LOTE = 20
ESPERA_SONDEO = 10         # segundos entre consultas de progreso al gestor
MAX_ESPERA_LOTE = 2 * 3600
MAX_ESPERA_SUELTO = 15 * 60    # un fichero suelto, en el reintento de un lote fallido
MAX_SONDEOS_FALLIDOS = 6       # consultas de progreso seguidas sin respuesta (un minuto)
# El gestor cierra la sesión a las 12 horas de entrar, se use o no. La subida
# de mucho acumulado puede durar más: se entra de nuevo entre lotes mucho
# antes (6 h, más las 2 h que puede durar un lote, quedan lejos de las 12).
RENOVAR_SESION_SEGUNDOS = 6 * 3600
# Lotes seguidos sin entrar que paran la subida (algo general falla), y las
# pausas antes del siguiente intento (un reinicio del gestor dura un par de minutos).
MAX_FALLOS_SEGUIDOS = 3
ESPERAS_TRAS_FALLO = (30, 120)
NOMBRE_TAREA = 'DescargadorDehuQEV-TodoRedes'
VERSION = '2026-11-25'     # la que enseña la pantalla y viaja en el diagnóstico
# Paquete para enviar a mano: partes de este tamaño como mucho (un ZIP de
# DEHÚ pesa unos 200 KB; 1.400 son unos 300 MB, tres partes).
MAX_BYTES_PARTE_A_MANO = 100 * 1024 * 1024
CARPETA_A_MANO = 'para_enviar_a_aikit'
# Lo único que entra en el diagnóstico. La configuración (auto_config.json:
# el usuario, las contraseñas cifradas, la ruta del certificado) no entra nunca.
FICHEROS_DIAGNOSTICO = ('registro.json', 'enviado.log', 'pasadas.log', 'pendientes_informadas.json')

CONFIG_POR_DEFECTO = {
    # Aceptar una NOTIFICACIÓN pendiente es comparecer: tiene efecto legal y
    # abre su plazo. Apagado hasta que QEV lo active expresamente.
    'aceptar_notificaciones_pendientes': False,
    'gestor_usuario': '',
    'gestor_password_dpapi': '',
    # Cuándo se comprobó por última vez que AiKit acepta ese usuario y esa
    # contraseña. Vacío = guardado sin comprobar: el botón no arranca.
    'gestor_comprobado_en': '',
    # Solo para la tarea programada (sin nadie delante):
    'pfx_path': '',
    'pfx_password_dpapi': '',
}


class EntregaError(Exception):
    """El gestor no ha recibido o no ha procesado un lote."""


class AccesoRechazado(EntregaError):
    """El gestor no reconoce el usuario o la contraseña (HTTP 401)."""


class GestorParado(EntregaError):
    """El gestor avisa de que ahora no puede procesar nada (su comprobación
    de Claude falla): no tiene sentido seguir subiendo lotes."""


class LoteSinTerminar(EntregaError):
    """El gestor sigue con el lote pasado el tiempo máximo: no se reintenta en
    esta pasada, para no dárselo dos veces a la vez."""


# ══════════════════════════════════════════════════════════════════
# Secretos en reposo: DPAPI de Windows (ctypes, sin dependencias nuevas)
# ══════════════════════════════════════════════════════════════════
# CryptProtectData cifra con una clave ligada al usuario de Windows: el valor
# guardado solo se puede descifrar con esa misma cuenta en ese mismo ordenador.

def _dpapi(funcion: str, datos: bytes) -> bytes:
    if sys.platform != 'win32':
        raise RuntimeError('La protección de contraseñas (DPAPI) solo existe en Windows.')
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    entrada = BLOB(len(datos), ctypes.cast(ctypes.create_string_buffer(datos, len(datos)),
                                           ctypes.POINTER(ctypes.c_char)))
    salida = BLOB()
    llamada = getattr(ctypes.windll.crypt32, funcion)
    if not llamada(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(salida)):
        raise RuntimeError(f'{funcion} ha fallado (¿otra cuenta de Windows u otro ordenador?)')
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


def proteger(texto: str) -> str:
    return base64.b64encode(_dpapi('CryptProtectData', texto.encode('utf-8'))).decode('ascii')


def desproteger(valor: str) -> str:
    return _dpapi('CryptUnprotectData', base64.b64decode(valor)).decode('utf-8')


# ══════════════════════════════════════════════════════════════════
# Configuración y registro local
# ══════════════════════════════════════════════════════════════════

def _ruta_config(base_dir):
    return os.path.join(base_dir, 'auto_config.json')


def leer_config(base_dir) -> dict:
    cfg = dict(CONFIG_POR_DEFECTO)
    try:
        with open(_ruta_config(base_dir), 'r', encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in CONFIG_POR_DEFECTO})
    except FileNotFoundError:
        pass
    # Solo un `true` escrito tal cual activa la aceptación.
    cfg['aceptar_notificaciones_pendientes'] = cfg['aceptar_notificaciones_pendientes'] is True
    return cfg


def guardar_config(base_dir, cfg: dict) -> None:
    _escribir_json(_ruta_config(base_dir), {k: cfg.get(k, v) for k, v in CONFIG_POR_DEFECTO.items()})


def _escribir_json(ruta, datos) -> None:
    """Escritura atómica: o queda el fichero nuevo entero o el anterior."""
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ruta)


def _ahora() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')


def anotar_pasada(base_dir, msg) -> None:
    """Añade a outputs/auto/pasadas.log lo que la pasada va diciendo, se
    lance desde la pantalla o sin ella: es lo que permite ver después qué pasó."""
    carpeta = os.path.join(base_dir, 'outputs', 'auto')
    os.makedirs(carpeta, exist_ok=True)
    with open(os.path.join(carpeta, 'pasadas.log'), 'a', encoding='utf-8') as f:
        f.write(f'{_ahora()} {msg}\n')


def _sha256(ruta) -> str:
    h = hashlib.sha256()
    with open(ruta, 'rb') as f:
        for trozo in iter(lambda: f.read(1 << 20), b''):
            h.update(trozo)
    return h.hexdigest()


class Registro:
    """Estado de cada envío de DEHÚ, por identificador. Se guarda tras cada cambio."""

    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, 'outputs', 'auto')
        self.por_subir = os.path.join(self.dir, 'por_subir')
        self.entregado = os.path.join(self.dir, 'entregado')
        self.tmp = os.path.join(self.dir, 'tmp')
        for d in (self.por_subir, self.entregado, self.tmp):
            os.makedirs(d, exist_ok=True)
        self.ruta = os.path.join(self.dir, 'registro.json')
        try:
            with open(self.ruta, 'r', encoding='utf-8') as f:
                self.datos = json.load(f)
        except FileNotFoundError:
            self.datos = {'envios': {}, 'pasadas': []}

    def guardar(self):
        _escribir_json(self.ruta, self.datos)

    def envio(self, ident) -> dict:
        return self.datos['envios'].get(ident, {})

    def hay_que_pedir(self, ident) -> bool:
        e = self.envio(ident)
        if e.get('estado') in ('descargado', 'entregado'):
            return False
        return e.get('respuestas_negativas', 0) < MAX_RESPUESTAS_NEGATIVAS

    def anotar_descarga(self, notif, via, ficheros):
        self.datos['envios'][notif['identificador']] = {
            **construir_meta(notif), 'via': via, 'estado': 'descargado',
            'descargado_en': _ahora(),
            'ficheros': [{'nombre': os.path.basename(r), 'bytes': os.path.getsize(r),
                          'sha256': _sha256(r)} for r in ficheros],
        }
        self.guardar()

    def anotar_negativa(self, notif, via, codigo, descripcion):
        e = self.datos['envios'].setdefault(notif['identificador'], {**construir_meta(notif), 'via': via})
        e['estado'] = 'sin_documento'
        e['respuestas_negativas'] = e.get('respuestas_negativas', 0) + 1
        e['ultima_respuesta'] = f'{codigo} - {descripcion}'
        self.guardar()

    def anotar_entrega(self, nombres):
        """Marca como entregados los envíos cuyos ficheros han salido ya todos."""
        nombres = set(nombres)
        for e in self.datos['envios'].values():
            if e.get('estado') != 'descargado':
                continue
            for f in e['ficheros']:
                if f['nombre'] in nombres:
                    f['entregado_en'] = _ahora()
            if all(f.get('entregado_en') for f in e['ficheros']):
                e['estado'] = 'entregado'
        self.guardar()

    def anotar_envio_log(self, url, ruta, resultado):
        with open(os.path.join(self.dir, 'enviado.log'), 'a', encoding='utf-8') as f:
            f.write(f'{_ahora()}\tPOST {url}\t{os.path.basename(ruta)}\t'
                    f'{os.path.getsize(ruta)}\t{_sha256(ruta)}\t{resultado}\n')


# ══════════════════════════════════════════════════════════════════
# 1. Bajar de DEHÚ
# ══════════════════════════════════════════════════════════════════

def _a_por_subir(reg: Registro, rutas: list, ident: str) -> list:
    """Mueve los ficheros de un envío a por_subir. Lo que no es ZIP se mete en
    un ZIP (el gestor solo acepta ZIP). No pisa nunca un fichero existente."""
    finales = []
    for ruta in rutas:
        nombre = os.path.basename(ruta)
        if not nombre.lower().endswith('.zip'):
            envuelto = ruta + '.zip'
            with zipfile.ZipFile(envuelto, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(ruta, nombre)
            os.remove(ruta)
            ruta, nombre = envuelto, nombre + '.zip'
        destino = os.path.join(reg.por_subir, nombre)
        if os.path.exists(destino) or os.path.exists(os.path.join(reg.entregado, nombre)):
            seguro = ''.join(c if c.isalnum() else '_' for c in ident)
            destino = os.path.join(reg.por_subir, f'{seguro}_{nombre}')
        shutil.move(ruta, destino)
        finales.append(destino)
    return finales


def _bajar_uno(client, notif, via, reg: Registro, log) -> str:
    """Baja un envío y lo deja en por_subir. Devuelve 'ok', 'no', 'agotado'
    (4225: ese envío no admite más consultas) o 'tope_acceso' (4305: el
    certificado no puede abrir más pendientes hoy)."""
    ident = notif.get('identificador', '')
    if via == 'pendiente':
        result, concepto = aceptar_con_reintento(client, notif, log)
    else:
        concepto = notif.get('concepto', '')
        result = client.consulta_realizadas(
            identificador=ident, codigo_origen=notif.get('codigoOrigen', ''), concepto=concepto)

    if result.get('codigo') == '200' and result.get('documento_bytes'):
        carpeta = os.path.join(reg.tmp, ''.join(c if c.isalnum() else '_' for c in ident))
        os.makedirs(carpeta, exist_ok=True)
        rutas = guardar_documento(result, notif, concepto, carpeta, log)
        finales = _a_por_subir(reg, rutas, ident)
        for ruta in finales:    # un documento suelto envuelto en ZIP aún no lleva meta
            anadir_meta_a_zip(ruta, {**construir_meta(notif), 'concepto': concepto}, lambda _m: None)
        reg.anotar_descarga(notif, via, finales)
        shutil.rmtree(carpeta, ignore_errors=True)
        return 'ok'

    codigo, desc = result.get('codigo', '?'), result.get('descripcion', '?')
    xml_raw = result.get('xml_raw', b'')
    texto = xml_raw.decode('utf-8', errors='ignore') if isinstance(xml_raw, bytes) else str(xml_raw)
    if via == 'pendiente' and (codigo == CODIGO_TOPE_DIARIO_ACCESO or CODIGO_TOPE_DIARIO_ACCESO in texto):
        log(f'[WARN] DEHÚ: tope diario de aperturas alcanzado ({desc}). Las pendientes siguen en la próxima pasada.')
        return 'tope_acceso'
    log(f'[ERROR] [{ident}] DEHÚ no da el documento: {codigo} - {desc}')
    reg.anotar_negativa(notif, via, codigo, desc)
    return 'agotado' if codigo == '4225' else 'no'


def _dato_pendiente(notif: dict) -> dict:
    """Lo que se informa de una notificación pendiente: lo que trae el listado
    de DEHÚ (el mismo meta que acompaña a un ZIP, más a quién va dirigida)."""
    dato = construir_meta(notif)
    for clave in ('titular_nifTitular', 'titular_nombreTitular', 'vinculo'):
        if notif.get(clave):
            dato[clave] = notif[clave]
    return dato


def bajar_todo(client, cfg: dict, reg: Registro, log) -> dict:
    """Baja todo lo de Red.es que falte. No sube nada."""
    r = {'bajados': 0, 'sin_documento': 0, 'errores': 0, 'listado_incompleto': False,
         'notificaciones_sin_aceptar': 0, 'limite': False, 'consultas_agotadas': 0,
         # Notificaciones de Red.es que la pasada deja pendientes (no se han
         # abierto): se informan a AiKit con los datos del listado, nada más.
         'pendientes_lista': [], 'pendientes_listado_completo': True}

    # Restos de una pasada interrumpida: se entregan, no se borran.
    for carpeta in os.listdir(reg.tmp):
        ruta_carpeta = os.path.join(reg.tmp, carpeta)
        restos = [os.path.join(ruta_carpeta, f) for f in os.listdir(ruta_carpeta)]
        _a_por_subir(reg, [f for f in restos if os.path.isfile(f)], carpeta)
        shutil.rmtree(ruta_carpeta, ignore_errors=True)

    desde = (datetime.datetime.now() - datetime.timedelta(days=DIAS_REALIZADAS)).strftime('%Y-%m-%dT00:00:00')
    listados = (
        ('pendiente', lambda: client.localiza_todas_pendientes()),
        ('realizada', lambda: client.localiza_todas_realizadas(fecha_desde=desde)),
    )
    pedidos = 0
    agotados_seguidos = 0
    for via, listar in listados:
        listado = listar()
        if listado.get('errores_paginacion'):
            r['listado_incompleto'] = True
            if via == 'pendiente':
                r['pendientes_listado_completo'] = False
            log(f'[ERROR] El listado de {via}s no está completo: {listado["errores_paginacion"]}')
        de_redes = [i for i in listado.get('items', []) if es_red_es(i) and i.get('identificador')]
        log(f'[INFO] {via}s: {len(listado.get("items", []))} en DEHÚ, {len(de_redes)} de Red.es')

        for notif in de_redes:
            # tipoEnvio «1» es comunicación; todo lo demás se trata como
            # notificación, que pendiente solo se acepta con el permiso activo.
            notificacion_pendiente = via == 'pendiente' and notif.get('tipoEnvio') != '1'
            if notificacion_pendiente and not cfg['aceptar_notificaciones_pendientes']:
                r['notificaciones_sin_aceptar'] += 1
                r['pendientes_lista'].append(_dato_pendiente(notif))
                continue
            if not reg.hay_que_pedir(notif['identificador']):
                if notificacion_pendiente:      # DEHÚ ya dijo «no» tres veces: sigue pendiente
                    r['pendientes_lista'].append(_dato_pendiente(notif))
                continue
            if pedidos >= MAX_POR_PASADA:
                r['limite'] = True
                if via == 'pendiente':          # quedan pendientes sin mirar
                    r['pendientes_listado_completo'] = False
                break
            pedidos += 1
            try:
                resultado = _bajar_uno(client, notif, via, reg, log)
            except Exception as e:      # fallo de red o de DEHÚ: se reintenta otro día
                log(f'[ERROR] [{notif["identificador"]}] {e}')
                r['errores'] += 1
                if notificacion_pendiente:
                    r['pendientes_lista'].append(_dato_pendiente(notif))
                continue
            if resultado != 'ok' and notificacion_pendiente:
                r['pendientes_lista'].append(_dato_pendiente(notif))
            if resultado == 'tope_acceso':
                # No se abren más pendientes en esta pasada; lo realizado sigue.
                r['tope_diario_aperturas'] = True
                r['pendientes_listado_completo'] = False
                break
            if resultado == 'agotado':
                r['consultas_agotadas'] += 1
                agotados_seguidos += 1
                if agotados_seguidos >= MAX_4225_SEGUIDOS:
                    log(f'[WARN] DEHÚ: {agotados_seguidos} envíos seguidos sin consultas disponibles (4225). '
                        f'Se sigue en la próxima pasada.')
                    r['limite'] = True
                    break
                continue
            agotados_seguidos = 0
            r['bajados' if resultado == 'ok' else 'sin_documento'] += 1
        if r['limite']:
            break

    if r['notificaciones_sin_aceptar']:
        log(f'[INFO] {r["notificaciones_sin_aceptar"]} notificaciones pendientes de Red.es NO se han aceptado '
            f'(permiso apagado). Entrarán cuando alguien las acepte. AiKit recibe solo la lista.')
    return r


# ══════════════════════════════════════════════════════════════════
# 2. Entregar al gestor de AiKit
# ══════════════════════════════════════════════════════════════════

def _json(resp, que):
    if resp.status_code != 200:
        raise EntregaError(f'{que}: el gestor responde HTTP {resp.status_code}')
    try:
        datos = resp.json()
    except ValueError:
        raise EntregaError(f'{que}: el gestor no responde JSON')
    if que != 'progreso' and not datos.get('exito'):
        raise EntregaError(f'{que}: {datos.get("mensaje", "rechazado")}')
    return datos


def _entrar(s, usuario: str, password: str) -> None:
    """Entra en el gestor con la sesión `s`. Un 401 es AccesoRechazado."""
    resp = s.post(f'{GESTOR_URL}/login-password', timeout=60, allow_redirects=False,
                  json={'usuario': usuario, 'password': password})
    if resp.status_code == 401:
        raise AccesoRechazado('AiKit no reconoce el usuario o la contraseña.')
    _json(resp, 'entrada al gestor')


def _salir(s) -> None:
    try:
        s.get(f'{GESTOR_URL}/logout', timeout=30, allow_redirects=False)
    except requests.RequestException:
        pass


def comprobar_y_guardar_acceso(base_dir, usuario, password) -> dict:
    """Prueba el usuario y la contraseña del gestor entrando y saliendo, y solo
    si AiKit los acepta los guarda (la contraseña, cifrada con DPAPI). Si no,
    la configuración anterior queda como estaba. La contraseña no se escribe en
    ningún registro ni se devuelve. Devuelve {'exito', 'motivo', 'mensaje'};
    motivo: 'datos', 'credenciales', 'conexion' o 'guardar'."""
    def no(motivo, mensaje):
        return {'exito': False, 'motivo': motivo, 'mensaje': mensaje + ' No se ha guardado nada.'}

    if not isinstance(usuario, str) or not isinstance(password, str):
        return no('datos', 'Faltan el usuario o la contraseña.')
    # Los espacios y saltos de línea de los extremos son restos de copiar y pegar.
    usuario, password = usuario.strip(), password.strip()
    if not usuario or not password:
        return no('datos', 'Faltan el usuario o la contraseña.')
    # Un carácter que no se ve (el que deja Ctrl+V en algunas ventanas, un salto
    # de línea en medio) hace que AiKit rechace la contraseña sin que se note.
    if any(unicodedata.category(c)[0] == 'C' for c in usuario + password):
        return no('datos', 'El usuario o la contraseña llevan un carácter que no se ve '
                           '(un salto de línea o un resto de pegar). Bórralos y escríbelos de nuevo.')

    s = requests.Session()
    try:
        _entrar(s, usuario, password)
    except AccesoRechazado:
        return no('credenciales', 'AiKit no reconoce ese usuario o esa contraseña.')
    except (EntregaError, requests.RequestException) as e:
        detalle = str(e) if isinstance(e, EntregaError) else type(e).__name__
        return no('conexion', f'No se ha podido comprobar con AiKit ({detalle}). Vuelve a intentarlo.')
    finally:
        _salir(s)

    try:
        cfg = leer_config(base_dir)
        cfg.update(gestor_usuario=usuario, gestor_password_dpapi=proteger(password),
                   gestor_comprobado_en=_ahora())
        guardar_config(base_dir, cfg)
    except (RuntimeError, OSError) as e:
        return no('guardar', f'AiKit acepta el acceso, pero no se ha podido guardar en este ordenador ({e}).')
    return {'exito': True, 'motivo': '', 'mensaje': 'AiKit acepta el usuario y la contraseña. Guardados.'}


def informar_pendientes(s, reg: Registro, informe: dict, log) -> dict:
    """Envía al gestor la lista de notificaciones de Red.es que siguen
    pendientes. Solo datos del listado de DEHÚ: no se abre ni se acepta nada.
    Lo enviado queda tal cual en pendientes_informadas.json y anotado en
    enviado.log. Si falla, la pasada sigue: los ZIP se entregan igual."""
    ruta = os.path.join(reg.dir, 'pendientes_informadas.json')
    _escribir_json(ruta, informe)
    url = f'{GESTOR_URL}/api/dehu-pendientes'
    try:
        resp = s.post(url, timeout=60, allow_redirects=False, json=informe)
        if resp.status_code in (404, 405):
            reg.anotar_envio_log(url, ruta, f'no admitido (HTTP {resp.status_code})')
            log('[INFO] El gestor de AiKit aún no admite la lista de pendientes; no se ha registrado.')
            return {'pendientes_informadas': None}
        _json(resp, 'lista de pendientes')
    except (EntregaError, requests.RequestException) as e:
        reg.anotar_envio_log(url, ruta, 'fallido')
        log(f'[WARN] No se ha podido informar de las pendientes: {e}')
        return {'pendientes_informadas': None, 'error_pendientes': str(e)}
    reg.anotar_envio_log(url, ruta, 'entregado')
    log(f'[OK] Informadas a AiKit {len(informe["pendientes"])} notificaciones de Red.es pendientes (sin abrir).')
    return {'pendientes_informadas': len(informe['pendientes'])}


class _SesionGestor:
    """La sesión con el gestor durante una subida. Se entra de nuevo sola
    cuando lleva muchas horas (el gestor la cierra a las 12) y después de un
    lote fallido, para empezar el siguiente con una sesión limpia."""

    def __init__(self, usuario: str, password: str, log):
        self._usuario, self._password, self._log = usuario, password, log
        self.s = None
        self.entrar()

    def entrar(self, motivo: str = '') -> None:
        self.salir()
        s = requests.Session()
        _entrar(s, self._usuario, self._password)
        self.s, self._desde = s, time.monotonic()
        if motivo:
            self._log(f'[INFO] Se ha entrado de nuevo en AiKit ({motivo}).')

    def al_dia(self) -> None:
        if self.s is None:
            self.entrar('sesión nueva tras un lote que no ha entrado')
        elif time.monotonic() - self._desde > RENOVAR_SESION_SEGUNDOS:
            self.entrar('la sesión llevaba muchas horas abierta')

    def salir(self) -> None:
        if self.s is not None:
            _salir(self.s)
            self.s = None


def _entero(valor) -> int:
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def _subir(s, rutas: list):
    abiertos = [open(ruta, 'rb') for ruta in rutas]
    try:
        return s.post(f'{GESTOR_URL}/api/upload-zips', timeout=900, allow_redirects=False,
                      files=[('zips', (os.path.basename(f.name), f, 'application/zip')) for f in abiertos])
    finally:
        for f in abiertos:
            f.close()


def _entregar_lote(sesion: _SesionGestor, rutas: list, max_espera: int) -> dict:
    """Sube un lote y espera a que el gestor lo procese. Devuelve su progreso
    final. Lanza EntregaError si el gestor no confirma el lote entero."""
    resp = _subir(sesion.s, rutas)
    if resp.status_code == 401:
        # La sesión ya no vale (caducada, o el gestor la ha cerrado). Aún no se
        # ha procesado nada de este lote: se entra de nuevo y se repite la subida.
        sesion.entrar('AiKit había cerrado la sesión')
        resp = _subir(sesion.s, rutas)
    subida = _json(resp, 'subida')
    _json(sesion.s.post(f'{GESTOR_URL}/procesar', timeout=60, allow_redirects=False,
                        json={'carpeta_input': subida['carpeta'], 'origen': 'manual'}), 'procesar')
    fin = _esperar_lote(sesion.s, max_espera)
    if fin.get('error_claude'):
        # El texto del gestor está escrito para AiKit; a quien usa el programa se le dice de quién es el fallo.
        raise GestorParado(f'AiKit no puede procesar ficheros ahora mismo. Es un fallo de AiKit, no de este '
                           f'ordenador: avisad a AiKit. (Detalle: {fin["error_claude"]})')
    # Entregado = el gestor dice que ha pasado por todos los ficheros del lote.
    # Un gestor reiniciado a mitad responde «parado» con todo a cero: no vale.
    total = _entero(fin.get('total'))
    if not total or _entero(fin.get('exitosos')) + _entero(fin.get('errores')) < total:
        raise EntregaError('el gestor no confirma haber procesado el lote')
    return fin


def _esperar_lote(s, max_espera: int) -> dict:
    limite = time.time() + max_espera
    quieto = sin_respuesta = 0
    while time.time() < limite:
        time.sleep(ESPERA_SONDEO)
        try:
            p = _json(s.get(f'{GESTOR_URL}/progreso', timeout=60, allow_redirects=False), 'progreso')
        except requests.RequestException:
            # Un corte breve de la red no da el lote por perdido: se sigue preguntando.
            sin_respuesta += 1
            if sin_respuesta >= MAX_SONDEOS_FALLIDOS:
                raise
            continue
        sin_respuesta = 0
        # El gestor arranca el lote en un hilo: dos lecturas seguidas «parado»
        # evitan dar por terminado un lote que aún no había empezado.
        quieto = 0 if p.get('procesando') else quieto + 1
        if quieto >= 2:
            return p
    raise LoteSinTerminar('el gestor no termina el lote en el tiempo máximo')


def _duracion_llana(segundos: float) -> str:
    minutos = round(segundos / 60)
    if minutos < 2:
        return 'menos de 2 min'
    if minutos < 90:
        return f'unos {minutos} min'
    return f'unas {round(minutos / 60)} h'


def entregar_todo(cfg: dict, reg: Registro, log, password: str, informe_pendientes: dict = None,
                  avance=None) -> dict:
    """Sube al gestor todo lo que hay en por_subir, por lotes, y devuelve el
    resumen. Los fallos de entrega no se lanzan: quedan en `error_entrega`.

    Nada se da por entregado sin que el gestor confirme el lote entero. Un
    lote que el gestor no procesa se queda en por_subir y la subida sigue con
    los demás; al final se reintenta fichero a fichero, para que uno que el
    gestor no admite no retenga a los otros. Si fallan MAX_FALLOS_SEGUIDOS
    lotes seguidos, o el gestor avisa de que no puede procesar, se deja de
    subir: lo que falta sigue en por_subir para la próxima vez.
    Si se da `informe_pendientes`, se envía antes, en la misma sesión.
    `avance(texto, hechos, total)` recibe el progreso para la pantalla."""
    r = {'entregados': 0, 'por_entregar': 0, 'errores_gestor': 0}
    pendientes = sorted(f for f in os.listdir(reg.por_subir) if f.lower().endswith('.zip'))
    if not pendientes and informe_pendientes is None:
        return r
    total = r['por_entregar'] = len(pendientes)
    avance = avance or (lambda *_: None)
    inicio = time.monotonic()
    apartados = []      # ficheros de lotes fallidos, para reintentar uno a uno
    sin_procesar = []   # los que quedan sin entrar tras su último intento
    seguidos = 0
    sesion = None

    def informar_avance():
        texto = f'Subiendo a AiKit: {r["entregados"]} de {total} ficheros'
        if r['entregados']:
            ritmo = (time.monotonic() - inicio) / r['entregados']
            faltan = total - r['entregados'] - len(sin_procesar)
            if faltan > 0:
                texto += f' (faltan {_duracion_llana(ritmo * faltan)})'
        avance(texto + '. Si se cierra el programa no se pierde nada: «Subir ahora» sigue donde se quedó.',
               r['entregados'], total)

    def entregar(nombres: list, max_espera: int, primer_intento: bool) -> None:
        nonlocal seguidos
        rutas = [os.path.join(reg.por_subir, n) for n in nombres]
        try:
            sesion.al_dia()
            fin = _entregar_lote(sesion, rutas, max_espera)
        except (AccesoRechazado, GestorParado):
            raise
        except (EntregaError, requests.RequestException) as e:
            sin_red = isinstance(e, requests.RequestException)
            motivo = f'sin conexión con AiKit ({type(e).__name__})' if sin_red else str(e)
            # Lo que sigue en manos del gestor no se le vuelve a dar en esta pasada.
            if primer_intento and not isinstance(e, LoteSinTerminar):
                apartados.extend(nombres)
            else:
                sin_procesar.extend(nombres)
            log(f'[WARN] AiKit no ha procesado {len(nombres)} fichero(s): {motivo}. '
                f'Siguen guardados en este ordenador.')
            sesion.salir()      # el siguiente lote entra con una sesión nueva
            # En el reintento uno a uno, un fichero que el gestor no admite no
            # dice nada del siguiente: solo cuentan los cortes de conexión.
            if primer_intento or sin_red:
                seguidos += 1
                if seguidos >= MAX_FALLOS_SEGUIDOS:
                    raise GestorParado(f'AiKit no ha procesado {seguidos} intentos seguidos (el último: {motivo}). '
                                       f'Se deja de subir por ahora.')
                time.sleep(ESPERAS_TRAS_FALLO[min(seguidos, len(ESPERAS_TRAS_FALLO)) - 1])
            informar_avance()
            return
        seguidos = 0
        for ruta in rutas:
            reg.anotar_envio_log(f'{GESTOR_URL}/api/upload-zips', ruta, 'entregado')
            shutil.move(ruta, os.path.join(reg.entregado, os.path.basename(ruta)))
        reg.anotar_entrega(nombres)
        r['entregados'] += len(nombres)
        r['errores_gestor'] += _entero(fin.get('errores'))
        log(f'[OK] Subidos {r["entregados"]} de {total} ficheros '
            f'(de estos {len(nombres)}, AiKit clasifica {fin.get("exitosos", "?")} y falla {fin.get("errores", "?")})')
        informar_avance()

    try:
        sesion = _SesionGestor(cfg['gestor_usuario'], password, log)
        if informe_pendientes is not None:
            r.update(informar_pendientes(sesion.s, reg, informe_pendientes, log))
        informar_avance()
        for i in range(0, total, TAMANO_LOTE):
            entregar(pendientes[i:i + TAMANO_LOTE], MAX_ESPERA_LOTE, primer_intento=True)
        if apartados:
            log(f'[INFO] Reintento, uno a uno, de {len(apartados)} ficheros de lotes que no han entrado.')
        for nombre in apartados:
            entregar([nombre], MAX_ESPERA_SUELTO, primer_intento=False)
    except AccesoRechazado:
        r['acceso_no_vale'] = True
        r['error_entrega'] = 'AiKit no reconoce el usuario o la contraseña guardados.'
    except requests.RequestException as e:
        r['error_entrega'] = f'No hay conexión con AiKit ({type(e).__name__}).'
    except EntregaError as e:
        r['error_entrega'] = str(e)
    finally:
        if sesion is not None:
            sesion.salir()

    r['por_entregar'] = total - r['entregados']
    if sin_procesar:
        r['sin_procesar'] = sorted(sin_procesar)[:50]
        log(f'[WARN] AiKit no ha procesado estos {len(sin_procesar)} ficheros: '
            f'{", ".join(sorted(sin_procesar)[:20])}{"…" if len(sin_procesar) > 20 else ""}')
        r.setdefault('error_entrega', f'AiKit no ha procesado {len(sin_procesar)} fichero(s).')
    return r


# ══════════════════════════════════════════════════════════════════
# Pasada completa
# ══════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _sin_suspension():
    """Mientras dura una pasada, Windows no suspende el ordenador por
    inactividad: una subida larga se cortaría. No cambia ninguna configuración;
    al terminar, o al cerrar el programa, todo queda como estaba."""
    if sys.platform != 'win32':
        yield
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def ejecutar(client, base_dir, log, avance=None) -> dict:
    """Baja todo lo de Red.es y lo entrega. Devuelve el resumen de la pasada."""
    cfg = leer_config(base_dir)
    reg = Registro(base_dir)
    resumen = {'inicio': _ahora(), 'aceptar_notificaciones_pendientes': cfg['aceptar_notificaciones_pendientes']}
    log(f'[INFO] ========== TODO LO DE RED.ES — aceptar notificaciones pendientes: '
        f'{"SÍ" if cfg["aceptar_notificaciones_pendientes"] else "NO"} ==========')

    with _sin_suspension():
        if avance:
            avance('Bajando de DEHÚ lo que ha enviado Red.es…', 0, 0)
        try:
            bajado = bajar_todo(client, cfg, reg, log)
        except Exception as e:      # DEHÚ no responde, el certificado no entra…
            # Lo que ya estaba bajado se entrega igual. Sin listado no hay lista de pendientes que enviar.
            log(f'[ERROR] No se ha podido bajar de DEHÚ: {e}')
            resumen.update(error_bajada=str(e)[:300], errores=1)
            return _entregar_y_cerrar(cfg, reg, log, resumen, None, avance)
        # La lista no se guarda en el resumen de la pasada: va en su propio fichero.
        informe = {'generado_en': _ahora(), 'listado_completo': bajado.pop('pendientes_listado_completo'),
                   'pendientes': bajado.pop('pendientes_lista')}
        resumen.update(bajado)
        return _entregar_y_cerrar(cfg, reg, log, resumen, informe, avance)


def subir_guardado(base_dir, log, avance=None) -> dict:
    """Entrega lo que ya está bajado en por_subir. No pide nada a DEHÚ ni
    envía lista de pendientes (no hay listado nuevo del que sacarla)."""
    resumen = {'inicio': _ahora(), 'solo_entrega': True}
    log('[INFO] ========== SUBIR A AIKIT LO YA BAJADO (no se pide nada a DEHÚ) ==========')
    with _sin_suspension():
        return _entregar_y_cerrar(leer_config(base_dir), Registro(base_dir), log, resumen, None, avance)


def _entregar_y_cerrar(cfg: dict, reg: Registro, log, resumen: dict, informe, avance=None) -> dict:
    """Entrega lo que hay en por_subir y anota la pasada en el registro.

    Bajar no depende del acceso a AiKit: sin un acceso que AiKit haya
    aceptado, lo bajado se queda guardado y la pasada dice que no se ha
    subido y por qué. Tampoco se envía la lista de pendientes; queda escrita
    en pendientes_informadas.json."""
    if informe is not None:     # se envíe o no, la lista queda escrita en este ordenador
        _escribir_json(os.path.join(reg.dir, 'pendientes_informadas.json'), informe)
    acceso = _estado_acceso(cfg, reg)
    if not acceso['acceso_listo']:
        # `sin_acceso`: no se ha intentado subir. `acceso_no_vale` se conserva
        # para que la pantalla siga diciendo que AiKit rechazó lo guardado.
        resumen.update(sin_acceso=True, error_entrega=acceso['motivo'])
        if acceso['acceso_no_vale']:
            resumen['acceso_no_vale'] = True
    else:
        try:
            password = desproteger(cfg['gestor_password_dpapi'])
        except RuntimeError:        # DPAPI: guardada con otra cuenta de Windows o en otro ordenador
            resumen['acceso_no_vale'] = True
            resumen['error_entrega'] = ('La contraseña de AiKit guardada no se puede leer con esta cuenta '
                                        'de Windows en este ordenador.')
        else:
            resumen.update(entregar_todo(cfg, reg, log, password, informe, avance))

    resumen['fin'] = _ahora()
    resumen['quedan_por_subir'] = len(os.listdir(reg.por_subir))
    resumen['mensaje'] = _mensaje_final(resumen)
    if resumen.get('error_entrega'):
        log(f'[ERROR] Entrega: {resumen["error_entrega"]} Lo que no ha subido sigue guardado en este ordenador.')
    reg.datos['pasadas'] = (reg.datos.get('pasadas', []) + [resumen])[-60:]
    reg.guardar()
    log(f'[INFO] Pasada terminada: {resumen.get("bajados", 0)} bajados, '
        f'{resumen.get("entregados", 0)} entregados, {resumen["quedan_por_subir"]} por subir.')
    return resumen


def _mensaje_final(resumen: dict) -> str:
    """Lo que la pantalla dice al terminar, en palabras llanas."""
    quedan = resumen['quedan_por_subir']
    partes = [] if resumen.get('solo_entrega') else [f'{resumen.get("bajados", 0)} bajados de DEHÚ']
    if resumen.get('error_bajada'):
        partes = [f'No se ha podido bajar de DEHÚ ({resumen["error_bajada"]})']
    if not resumen.get('sin_acceso'):
        partes.append(f'{resumen.get("entregados", 0)} subidos a AiKit')
    texto = ', '.join(partes) + '.'
    if resumen.get('listado_incompleto'):
        texto += ' DEHÚ no ha dado el listado completo: puede quedar algo por bajar; se pide otra vez en la próxima pasada.'
    if resumen.get('errores') and not resumen.get('error_bajada'):
        texto += f' {resumen["errores"]} envíos de DEHÚ han dado error y se piden otra vez en la próxima pasada.'
    if resumen.get('sin_acceso'):
        texto += (f' No se ha subido nada a AiKit: {resumen["error_entrega"]} '
                  f'Hay {quedan} ficheros guardados en este ordenador esperando. Para subirlos: '
                  f'«Configurar acceso a AiKit» y después «Subir ahora». Si corre prisa, '
                  f'«Preparar un ZIP para enviarlo a mano».')
    elif quedan:
        texto += (f' {quedan} ficheros siguen guardados en este ordenador sin subir: '
                  f'{resumen.get("error_entrega") or "no ha dado tiempo."} '
                  f'No se ha perdido nada; «Subir ahora» lo intenta de nuevo. Si corre prisa, '
                  f'«Preparar un ZIP para enviarlo a mano».')
    elif resumen.get('error_entrega'):
        texto += ' ' + resumen['error_entrega']
    return texto


# ══════════════════════════════════════════════════════════════════
# Salida de emergencia: un paquete para enviar a mano
# ══════════════════════════════════════════════════════════════════

def preparar_envio_a_mano(base_dir) -> dict:
    """Copia todo lo que espera en por_subir a uno o varios ZIP que QEV puede
    enviar a AiKit por otro camino cuando la subida no funciona, más un ZIP
    de diagnóstico. No llama a DEHÚ ni al gestor y no necesita acceso.

    No cambia nada de lo guardado: los ficheros siguen en por_subir y la
    próxima subida los envía igual (el gestor admite recibir dos veces la
    misma notificación). Cada vez se escribe en una carpeta nueva.
    Devuelve {'carpeta', 'partes', 'diagnostico', 'ficheros', 'bytes'}."""
    reg = Registro(base_dir)
    nombres = sorted(f for f in os.listdir(reg.por_subir) if f.lower().endswith('.zip'))
    sello = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
    carpeta = os.path.join(base_dir, CARPETA_A_MANO, sello)
    os.makedirs(carpeta)

    # Reparto en partes por tamaño. Los ZIP de DEHÚ van en la raíz de cada
    # parte, sin recomprimir, para poder cargarla tal cual en el gestor.
    partes, peso = [[]], 0
    for nombre in nombres:
        tam = os.path.getsize(os.path.join(reg.por_subir, nombre))
        if partes[-1] and peso + tam > MAX_BYTES_PARTE_A_MANO:
            partes.append([])
            peso = 0
        partes[-1].append(nombre)
        peso += tam
    partes = [p for p in partes if p]

    contenido = [f'Descargador DEHÚ {VERSION}. Paquete preparado el {_ahora()}.',
                 f'{len(nombres)} ficheros de notificaciones en {len(partes)} parte(s). '
                 f'Son copias: los originales siguen en este ordenador, sin marcar como subidos.', '',
                 'parte\tfichero\tbytes\tsha256']
    escritas, total_bytes = [], 0
    for n, parte in enumerate(partes, start=1):
        sufijo = f'_parte_{n}_de_{len(partes)}' if len(partes) > 1 else ''
        ruta_parte = os.path.join(carpeta, f'QEV_para_AiKit_{sello}{sufijo}.zip')
        with zipfile.ZipFile(ruta_parte, 'w', zipfile.ZIP_STORED) as zf:
            for nombre in parte:
                origen = os.path.join(reg.por_subir, nombre)
                zf.write(origen, nombre)
                contenido.append(f'{n}\t{nombre}\t{os.path.getsize(origen)}\t{_sha256(origen)}')
        escritas.append(os.path.basename(ruta_parte))
        total_bytes += os.path.getsize(ruta_parte)

    diagnostico = f'QEV_diagnostico_{sello}.zip'
    with zipfile.ZipFile(os.path.join(carpeta, diagnostico), 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('CONTENIDO.txt', '\n'.join(contenido) + '\n')
        for nombre in FICHEROS_DIAGNOSTICO:
            ruta = os.path.join(reg.dir, nombre)
            if os.path.isfile(ruta):
                zf.write(ruta, nombre)

    r = {'carpeta': carpeta, 'partes': escritas, 'diagnostico': diagnostico,
         'ficheros': len(nombres), 'bytes': total_bytes}
    reg.datos['paquetes_a_mano'] = (reg.datos.get('paquetes_a_mano', []) + [
        {'preparado_en': _ahora(), 'carpeta': carpeta, 'ficheros': len(nombres), 'partes': escritas}])[-60:]
    reg.guardar()
    anotar_pasada(base_dir, f'[INFO] Paquete para enviar a mano: {len(nombres)} ficheros en {len(escritas)} parte(s), '
                            f'en {carpeta}. No se ha marcado nada como subido.')
    return r


def _estado_acceso(cfg: dict, reg: Registro) -> dict:
    """Si hay un acceso a AiKit con el que se puede subir, y si no, por qué."""
    pasadas = reg.datos.get('pasadas', [])
    ultima = pasadas[-1] if pasadas else {}
    configurado = bool(cfg['gestor_usuario'] and cfg['gestor_password_dpapi'])
    comprobado = cfg['gestor_comprobado_en'] if configurado else ''
    # No vale: la última pasada no pudo entrar, después de la última comprobación.
    no_vale = bool(ultima.get('acceso_no_vale')) and ultima.get('fin', '') > comprobado
    if not configurado:
        motivo = 'Falta configurar el acceso a AiKit.'
    elif no_vale:
        motivo = 'AiKit no ha aceptado el usuario o la contraseña guardados.'
    elif not comprobado:
        motivo = 'El acceso a AiKit está guardado pero sin comprobar.'
    else:
        motivo = ''
    return {
        'gestor_configurado': configurado,
        'acceso_comprobado_en': comprobado,
        'acceso_no_vale': no_vale,
        # Solo se sube con un acceso que AiKit ha aceptado.
        'acceso_listo': not motivo,
        'motivo': motivo,
    }


def estado(base_dir) -> dict:
    """Lo que la pantalla enseña junto al botón."""
    cfg = leer_config(base_dir)
    reg = Registro(base_dir)
    pasadas = reg.datos.get('pasadas', [])
    acceso = _estado_acceso(cfg, reg)
    return {
        **{k: v for k, v in acceso.items() if k != 'motivo'},
        'acceso_falta': acceso['motivo'],
        'gestor_usuario': cfg['gestor_usuario'],
        'aceptar_notificaciones_pendientes': cfg['aceptar_notificaciones_pendientes'],
        'quedan_por_subir': len(os.listdir(reg.por_subir)),
        'ultima_pasada': pasadas[-1] if pasadas else None,
        'destino': GESTOR_URL,
        'version': VERSION,
    }


# ══════════════════════════════════════════════════════════════════
# Línea de órdenes: configurar, ejecutar, programar
# ══════════════════════════════════════════════════════════════════

def cert_context_guardado(base_dir) -> dict:
    """Certificado para la tarea programada: el .pfx sigue en su sitio y su
    contraseña se descifra con DPAPI en el momento de usarla."""
    from auth import extraer_datos_certificado
    cfg = leer_config(base_dir)
    if not cfg['pfx_path'] or not cfg['pfx_password_dpapi']:
        raise RuntimeError('Falta configurar el certificado para la pasada sola (auto configurar).')
    password = desproteger(cfg['pfx_password_dpapi'])
    with open(cfg['pfx_path'], 'rb') as f:
        datos = extraer_datos_certificado(f.read(), password)
    return {'pfx_b64': datos['pfx_b64'], 'pfx_pwd': password, 'nif': datos['nif'],
            'nombre': datos['nombre'], 'cif_empresa': datos.get('cif_empresa', '')}


def _configurar(base_dir):
    import getpass
    cfg = leer_config(base_dir)
    print('Configuración de la pasada «todo lo de Red.es». Intro deja el valor como está.\n')
    print('El acceso a AiKit se configura mejor en la pantalla («Configurar acceso a AiKit»).')
    usuario = input(f'Usuario del gestor de AiKit [{cfg["gestor_usuario"]}]: ').strip() or cfg['gestor_usuario']
    password = getpass.getpass('Contraseña del gestor (no se muestra; pégala una sola vez): ')
    if password:
        r = comprobar_y_guardar_acceso(base_dir, usuario, password)
        print(('[OK] ' if r['exito'] else '[ERROR] ') + r['mensaje'])
        cfg = leer_config(base_dir)     # con el acceso recién guardado, si se ha aceptado
    elif usuario != cfg['gestor_usuario']:
        print('[ERROR] Para cambiar el usuario hay que escribir también la contraseña. No se ha cambiado.')
    print('\nSolo si se va a programar la pasada diaria sin nadie delante:')
    pfx = input(f'Ruta del certificado .pfx [{cfg["pfx_path"]}]: ').strip().strip('"')
    if pfx:
        cfg['pfx_path'] = pfx
        cfg['pfx_password_dpapi'] = proteger(getpass.getpass('Contraseña del certificado (no se muestra): '))
    print('\nAceptar una notificación pendiente es comparecer: tiene efecto legal y abre su plazo.')
    r = input('¿Aceptar solas las NOTIFICACIONES pendientes de Red.es? Escribe ACEPTO para activarlo, '
              'NO para apagarlo, Intro para no cambiarlo: ').strip()
    if r == 'ACEPTO':
        cfg['aceptar_notificaciones_pendientes'] = True
    elif r.upper() == 'NO':
        cfg['aceptar_notificaciones_pendientes'] = False
    guardar_config(base_dir, cfg)
    print(f'\nGuardado en {_ruta_config(base_dir)}. Aceptar notificaciones pendientes: '
          f'{"SÍ" if cfg["aceptar_notificaciones_pendientes"] else "NO"}')


def _orden_de_arranque() -> str:
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" auto ejecutar'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" auto ejecutar'


def main(argv, base_dir, crear_cliente) -> int:
    """`crear_cliente(cert_context, log)` devuelve el cliente DEHÚ (lo da app.py)."""
    orden = argv[0] if argv else ''
    if orden == 'configurar':
        _configurar(base_dir)
    elif orden == 'ejecutar':
        def log(msg):
            if sys.stdout:          # un .exe sin consola no tiene salida estándar
                print(msg)
            anotar_pasada(base_dir, msg)
        try:
            resumen = ejecutar(crear_cliente(cert_context_guardado(base_dir), log), base_dir, log)
        except Exception as e:
            log(f'[ERROR] La pasada no ha podido empezar: {e}')
            return 1
        return 1 if (resumen.get('error_entrega') or resumen.get('errores')
                     or resumen.get('listado_incompleto') or resumen.get('consultas_agotadas')) else 0
    elif orden == 'programar':
        hora = argv[1] if len(argv) > 1 else '08:00'
        return subprocess.call(['schtasks', '/Create', '/F', '/TN', NOMBRE_TAREA, '/SC', 'DAILY',
                                '/ST', hora, '/TR', _orden_de_arranque()])
    elif orden == 'desprogramar':
        return subprocess.call(['schtasks', '/Delete', '/F', '/TN', NOMBRE_TAREA])
    elif orden == 'estado':
        print(json.dumps(estado(base_dir), indent=2, ensure_ascii=False))
    else:
        print('Uso: auto configurar | ejecutar | programar [HH:MM] | desprogramar | estado')
        return 2
    return 0
