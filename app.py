"""
Descargador DEHÚ Lite — Servidor Flask mínimo.

Solo descarga notificaciones de DEHÚ y genera un ZIP consolidado.
Sin clasificación, sin Excel, sin SharePoint, sin base de datos.
"""

import os
import io
import json
import uuid
import time
import shutil
import zipfile
import threading
from datetime import datetime

from flask import (
    Flask, render_template, request, session, redirect,
    url_for, jsonify, send_file,
)

from auth import extraer_datos_certificado
from descarga import guardar_documento, aceptar_con_reintento
import auto

import sys

if getattr(sys, 'frozen', False):
    BUNDLE_DIR = sys._MEIPASS
    EXECUTABLE_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXECUTABLE_DIR = BUNDLE_DIR

# ── App Flask ──────────────────────────────────────────────────────
app = Flask(__name__, 
            template_folder=os.path.join(BUNDLE_DIR, 'templates'),
            static_folder=os.path.join(BUNDLE_DIR, 'static'))
app.secret_key = os.environ.get('SECRET_KEY', 'dehu-lite-secret-' + uuid.uuid4().hex[:8])
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 7  # 7 días
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

DEHU_CONFIG_PATH = os.path.join(EXECUTABLE_DIR, 'dehu_config.json')
OUTPUTS_DIR = os.path.join(EXECUTABLE_DIR, 'outputs')

# ── Almacén de certificados en memoria (por sesión) ───────────────
cert_storage = {}  # sid → {pfx_b64, pfx_pwd, cif_empresa}

# ── Estado global de proceso DEHÚ ─────────────────────────────────
dehu_estado = {
    'procesando': False,
    'logs': [],
    'progreso_actual': 0,
    'progreso_total': 0,
    'resultado': None,
    'zip_path': '',
    'fase': '',     # qué está haciendo la pasada «todo lo de Red.es», en palabras llanas
}
dehu_lock = threading.Lock()

# Límite máximo de notificaciones por descarga (restricción de DEHÚ)
MAX_DOWNLOAD_LIMIT = 998


# ══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DEHÚ
# ══════════════════════════════════════════════════════════════════

def _leer_dehu_config():
    if os.path.exists(DEHU_CONFIG_PATH):
        try:
            with open(DEHU_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'pfx_path': '', 'key_password': '', 'nif': '', 'nombre': '', 'env': 'produccion'}


def _guardar_dehu_config(config):
    with open(DEHU_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _crear_dehu_client(log_callback=None, cert_context=None):
    """Crea un DEHuClient usando: cert_context > sesión > env var > fichero local."""
    from dehu_client import DEHuClient, DEHuConfig

    config_local = _leer_dehu_config()
    env = config_local.get('env', os.environ.get('DEHU_ENV', 'produccion'))

    # 1. cert_context explícito (thread-safe)
    if cert_context and cert_context.get('pfx_b64', '').strip():
        nif_dehu = cert_context.get('cif_empresa', '') or cert_context.get('nif', '')
        return DEHuClient(DEHuConfig(
            pfx_base64=cert_context['pfx_b64'],
            key_password=cert_context.get('pfx_pwd', ''),
            nif=nif_dehu,
            nombre=cert_context.get('nombre', ''),
            env=env,
        ), log_callback=log_callback)

    # 2. Certificado de sesión
    try:
        sid = session.get('session_id', '')
    except RuntimeError:
        sid = ''
    cert_data = cert_storage.get(sid) if sid else None
    if cert_data and cert_data.get('pfx_b64', '').strip():
        nif_dehu = session.get('cif_empresa', '') or session.get('nif', '')
        return DEHuClient(DEHuConfig(
            pfx_base64=cert_data['pfx_b64'],
            key_password=cert_data.get('pfx_pwd', ''),
            nif=nif_dehu,
            nombre=session.get('nombre', ''),
            env=env,
        ), log_callback=log_callback)

    # 3. Fichero local
    pfx_path = config_local.get('pfx_path', '').strip().strip("'\"")
    if not pfx_path:
        raise ValueError("No hay certificado cargado. Inicia sesión con tu .pfx.")
    if not os.path.exists(pfx_path):
        raise FileNotFoundError(f"El certificado no existe en: {pfx_path}")
    return DEHuClient(DEHuConfig(
        pfx_path=pfx_path,
        key_password=config_local.get('key_password', ''),
        nif=config_local.get('nif', ''),
        nombre=config_local.get('nombre', ''),
        env=env,
    ), log_callback=log_callback)


# ══════════════════════════════════════════════════════════════════
# LOGIN / LOGOUT
# ══════════════════════════════════════════════════════════════════

@app.route('/login', methods=['GET'])
def login_page():
    if 'nif' in session:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login_submit():
    try:
        if 'file' not in request.files:
            return jsonify({'exito': False, 'mensaje': 'No se ha enviado ningún archivo'}), 400

        file = request.files['file']
        password = request.form.get('password', '')

        if not file.filename:
            return jsonify({'exito': False, 'mensaje': 'No se ha seleccionado ningún archivo'}), 400

        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in ('pfx', 'p12'):
            return jsonify({'exito': False, 'mensaje': 'Solo se aceptan archivos .pfx o .p12'}), 400

        pfx_data = file.read()

        try:
            datos = extraer_datos_certificado(pfx_data, password)
        except ValueError as e:
            return jsonify({'exito': False, 'mensaje': str(e)}), 400

        # Crear sesión
        sid = str(uuid.uuid4())
        session.permanent = True
        session['session_id'] = sid
        session['nif'] = datos['nif']
        session['nombre'] = datos['nombre']
        session['cif_empresa'] = datos.get('cif_empresa', '')
        session['cert_expiry'] = datos['expiry_date']
        session['cert_days'] = datos['days_remaining']

        cert_storage[sid] = {
            'pfx_b64': datos['pfx_b64'],
            'pfx_pwd': password,
            'cif_empresa': datos.get('cif_empresa', ''),
            'nif': datos['nif'],
            'nombre': datos['nombre'],
        }

        print(f"[AUTH] Login: NIF={datos['nif']}, Nombre={datos['nombre']}, SID={sid[:8]}...")

        return jsonify({
            'exito': True,
            'nif': datos['nif'],
            'nombre': datos['nombre'],
        })

    except Exception as e:
        return jsonify({'exito': False, 'mensaje': f'Error: {str(e)}'}), 500


@app.route('/logout')
def logout():
    sid = session.get('session_id')
    if sid and sid in cert_storage:
        del cert_storage[sid]
    session.clear()
    return redirect(url_for('login_page'))


# ══════════════════════════════════════════════════════════════════
# PÁGINA PRINCIPAL
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'nif' not in session:
        return redirect(url_for('login_page'))
    return render_template(
        'index.html',
        nombre=session.get('nombre', ''),
        nif=session.get('nif', ''),
    )


# ══════════════════════════════════════════════════════════════════
# API DEHÚ
# ══════════════════════════════════════════════════════════════════

@app.route('/api/dehu/pendientes', methods=['GET'])
def dehu_pendientes():
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    try:
        client = _crear_dehu_client()
        kwargs = {}
        if request.args.get('fecha_desde'):
            kwargs['fecha_desde'] = request.args['fecha_desde']
        if request.args.get('fecha_hasta'):
            kwargs['fecha_hasta'] = request.args['fecha_hasta']
        result = client.localiza_todas_pendientes(**kwargs)
        items = result['items']
        errores_pag = result.get('errores_paginacion', [])

        if not items and errores_pag:
            primer_error = errores_pag[0]
            return jsonify({
                'exito': False,
                'mensaje': f'DEHÚ respondió con error: {primer_error.get("error", "?")}',
            }), 400

        return jsonify({'exito': True, 'items': items, 'total': len(items)})
    except Exception as e:
        return jsonify({'exito': False, 'mensaje': str(e)}), 500


@app.route('/api/dehu/realizadas', methods=['GET'])
def dehu_realizadas():
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    try:
        client = _crear_dehu_client()
        kwargs = {}
        if request.args.get('fecha_desde'):
            kwargs['fecha_desde'] = request.args['fecha_desde']
        if request.args.get('fecha_hasta'):
            kwargs['fecha_hasta'] = request.args['fecha_hasta']
        result = client.localiza_todas_realizadas(**kwargs)
        items = result['items']
        errores_pag = result.get('errores_paginacion', [])

        if not items and errores_pag:
            primer_error = errores_pag[0]
            return jsonify({
                'exito': False,
                'mensaje': f'DEHÚ respondió con error: {primer_error.get("error", "?")}',
            }), 400

        return jsonify({'exito': True, 'items': items, 'total': len(items)})
    except Exception as e:
        return jsonify({'exito': False, 'mensaje': str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# DESCARGA (thread)
# ══════════════════════════════════════════════════════════════════

def _dehu_log(msg):
    with dehu_lock:
        dehu_estado['logs'].append(msg)


def _limpiar_descargas():
    """Limpia la carpeta de descargas temporales."""
    dehu_dir = os.path.join(OUTPUTS_DIR, 'dehu_descargas')
    if os.path.exists(dehu_dir):
        shutil.rmtree(dehu_dir, ignore_errors=True)
    os.makedirs(dehu_dir, exist_ok=True)
    return dehu_dir


def _generar_zip_consolidado(dehu_dir):
    """Crea un ZIP con todos los archivos descargados."""
    fecha = datetime.now().strftime('%Y%m%d_%H%M')
    zip_name = f'Dehu_Descarga_{fecha}.zip'
    zip_path = os.path.join(OUTPUTS_DIR, zip_name)

    total = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in os.listdir(dehu_dir):
            fpath = os.path.join(dehu_dir, f)
            if os.path.isfile(fpath):
                zf.write(fpath, f)
                total += 1

    if total == 0:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return None

    _dehu_log(f'[INFO] 📦 ZIP consolidado: {zip_name} ({total} archivos, {os.path.getsize(zip_path):,} bytes)')
    return zip_path


def ejecutar_descarga_pendientes(notificaciones, cert_context):
    """Thread: acepta y descarga notificaciones pendientes."""
    try:
        with dehu_lock:
            dehu_estado['procesando'] = True
            dehu_estado['logs'] = []
            dehu_estado['progreso_actual'] = 0
            dehu_estado['progreso_total'] = len(notificaciones)
            dehu_estado['resultado'] = None
            dehu_estado['zip_path'] = ''

        dehu_dir = _limpiar_descargas()
        client = _crear_dehu_client(cert_context=cert_context)

        _dehu_log(f'[INFO] ========== 📥 DESCARGA DEHÚ — {len(notificaciones)} pendientes ==========')

        descargados = 0
        errores = 0

        for i, notif in enumerate(notificaciones, 1):
            ident = notif.get('identificador', '')
            concepto = notif.get('concepto', '')

            _dehu_log(f'[INFO] ({i}/{len(notificaciones)}) [{ident}] Aceptando: {concepto[:60]}...')

            try:
                result, concepto = aceptar_con_reintento(client, notif, _dehu_log)

                if result['codigo'] == '200' and result.get('documento_bytes'):
                    guardar_documento(result, notif, concepto, dehu_dir, _dehu_log)
                    descargados += 1
                else:
                    code = result.get('codigo', '?')
                    desc = result.get('descripcion', '?')
                    _dehu_log(f'[ERROR] Fallo al aceptar {ident}: {code} - {desc}')
                    errores += 1

            except Exception as e:
                _dehu_log(f'[ERROR] Excepción: {ident}: {str(e)}')
                errores += 1

            with dehu_lock:
                dehu_estado['progreso_actual'] = i

        _dehu_log(f'[INFO] Descarga completada: {descargados} OK, {errores} errores')

        # Generar ZIP consolidado
        if descargados > 0:
            _dehu_log(f'[INFO] ========== 📦 GENERANDO ZIP CONSOLIDADO ==========')
            zip_path = _generar_zip_consolidado(dehu_dir)
            if zip_path:
                with dehu_lock:
                    dehu_estado['zip_path'] = zip_path

        with dehu_lock:
            dehu_estado['resultado'] = {
                'descargados': descargados,
                'errores': errores,
            }

    except Exception as e:
        _dehu_log(f'[ERROR] Error crítico: {str(e)}')
    finally:
        with dehu_lock:
            dehu_estado['procesando'] = False


def ejecutar_descarga_realizadas(notificaciones, cert_context):
    """Thread: re-descarga notificaciones ya realizadas."""
    try:
        with dehu_lock:
            dehu_estado['procesando'] = True
            dehu_estado['logs'] = []
            dehu_estado['progreso_actual'] = 0
            dehu_estado['progreso_total'] = len(notificaciones)
            dehu_estado['resultado'] = None
            dehu_estado['zip_path'] = ''

        dehu_dir = _limpiar_descargas()
        client = _crear_dehu_client(cert_context=cert_context)

        _dehu_log(f'[INFO] ========== 📋 RE-DESCARGA DEHÚ — {len(notificaciones)} realizadas ==========')

        descargados = 0
        errores = 0

        for i, notif in enumerate(notificaciones, 1):
            ident = notif.get('identificador', '')
            origen = notif.get('codigoOrigen', '')
            concepto = notif.get('concepto', '')

            _dehu_log(f'[INFO] ({i}/{len(notificaciones)}) [{ident}] Consultando: {concepto[:60]}...')

            try:
                result = client.consulta_realizadas(
                    identificador=ident,
                    codigo_origen=origen,
                    concepto=concepto,
                )

                if result['codigo'] == '200' and result.get('documento_bytes'):
                    guardar_documento(result, notif, concepto, dehu_dir, _dehu_log)
                    descargados += 1
                else:
                    code = result.get('codigo', '?')
                    desc = result.get('descripcion', '?')
                    _dehu_log(f'[ERROR] Fallo al consultar {ident}: {code} - {desc}')
                    errores += 1

            except Exception as e:
                _dehu_log(f'[ERROR] Excepción: {ident}: {str(e)}')
                errores += 1

            with dehu_lock:
                dehu_estado['progreso_actual'] = i

        _dehu_log(f'[INFO] Descarga completada: {descargados} OK, {errores} errores')

        # Generar ZIP consolidado
        if descargados > 0:
            _dehu_log(f'[INFO] ========== 📦 GENERANDO ZIP CONSOLIDADO ==========')
            zip_path = _generar_zip_consolidado(dehu_dir)
            if zip_path:
                with dehu_lock:
                    dehu_estado['zip_path'] = zip_path

        with dehu_lock:
            dehu_estado['resultado'] = {
                'descargados': descargados,
                'errores': errores,
            }

    except Exception as e:
        _dehu_log(f'[ERROR] Error crítico: {str(e)}')
    finally:
        with dehu_lock:
            dehu_estado['procesando'] = False


def _cert_de_sesion():
    """Certificado de la sesión actual, para pasarlo al thread de descarga."""
    cert_ctx = cert_storage.get(session.get('session_id', ''))
    if cert_ctx:
        cert_ctx = dict(cert_ctx)
        cert_ctx['nif'] = session.get('nif', '')
        cert_ctx['nombre'] = session.get('nombre', '')
        cert_ctx['cif_empresa'] = session.get('cif_empresa', '')
    return cert_ctx


# ── Endpoints de control ──

@app.route('/api/dehu/aceptar', methods=['POST'])
def dehu_aceptar():
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    try:
        data = request.get_json()
        notificaciones = data.get('notificaciones', [])
        if not notificaciones:
            return jsonify({'exito': False, 'mensaje': 'No se seleccionaron notificaciones'}), 400

        # Safety net: truncar a MAX_DOWNLOAD_LIMIT
        if len(notificaciones) > MAX_DOWNLOAD_LIMIT:
            print(f"[WARN] Se recibieron {len(notificaciones)} notificaciones, truncando a {MAX_DOWNLOAD_LIMIT}")
            notificaciones = notificaciones[:MAX_DOWNLOAD_LIMIT]

        with dehu_lock:
            if dehu_estado['procesando']:
                return jsonify({'exito': False, 'mensaje': 'Ya hay un proceso en ejecución'}), 400

        cert_ctx = _cert_de_sesion()

        thread = threading.Thread(target=ejecutar_descarga_pendientes, args=(notificaciones, cert_ctx))
        thread.daemon = True
        thread.start()

        return jsonify({'exito': True, 'mensaje': 'Proceso iniciado'})
    except Exception as e:
        return jsonify({'exito': False, 'mensaje': str(e)}), 500


@app.route('/api/dehu/descargar-realizadas', methods=['POST'])
def dehu_descargar_realizadas():
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    try:
        data = request.get_json()
        notificaciones = data.get('notificaciones', [])
        if not notificaciones:
            return jsonify({'exito': False, 'mensaje': 'No se seleccionaron notificaciones'}), 400

        # Safety net: truncar a MAX_DOWNLOAD_LIMIT
        if len(notificaciones) > MAX_DOWNLOAD_LIMIT:
            print(f"[WARN] Se recibieron {len(notificaciones)} realizadas, truncando a {MAX_DOWNLOAD_LIMIT}")
            notificaciones = notificaciones[:MAX_DOWNLOAD_LIMIT]

        with dehu_lock:
            if dehu_estado['procesando']:
                return jsonify({'exito': False, 'mensaje': 'Ya hay un proceso en ejecución'}), 400

        cert_ctx = _cert_de_sesion()

        thread = threading.Thread(target=ejecutar_descarga_realizadas, args=(notificaciones, cert_ctx))
        thread.daemon = True
        thread.start()

        return jsonify({'exito': True, 'mensaje': 'Proceso iniciado'})
    except Exception as e:
        return jsonify({'exito': False, 'mensaje': str(e)}), 500


@app.route('/api/dehu/estado', methods=['GET'])
def dehu_estado_api():
    with dehu_lock:
        data = {
            'procesando': dehu_estado['procesando'],
            'logs': list(dehu_estado['logs']),
            'progreso_actual': dehu_estado['progreso_actual'],
            'progreso_total': dehu_estado['progreso_total'],
            'resultado': dehu_estado['resultado'],
            'fase': dehu_estado['fase'],
            'zip_listo': bool(dehu_estado.get('zip_path')),
        }
    return jsonify(data)


@app.route('/api/dehu/descargar-zip', methods=['GET'])
def dehu_descargar_zip():
    with dehu_lock:
        zip_path = dehu_estado.get('zip_path', '')
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({'exito': False, 'mensaje': 'No hay ZIP disponible'}), 404
    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=os.path.basename(zip_path),
    )


# ══════════════════════════════════════════════════════════════════
# TODO LO DE RED.ES (auto.py): un botón, sin elegir
# ══════════════════════════════════════════════════════════════════

def _auto_log(msg):
    """Lo que dice una pasada: a la pantalla y a outputs/auto/pasadas.log."""
    _dehu_log(msg)
    auto.anotar_pasada(EXECUTABLE_DIR, msg)


def _auto_avance(texto, hechos, total):
    """Lo que la pantalla enseña mientras dura una pasada."""
    with dehu_lock:
        dehu_estado.update(fase=texto, progreso_actual=hechos, progreso_total=total)


def _correr_auto(trabajo):
    """Thread: ejecuta `trabajo()` (una pasada de auto.py, que devuelve su
    resumen) y deja el resultado para la pantalla."""
    try:
        with dehu_lock:
            dehu_estado.update(procesando=True, logs=[], progreso_actual=0,
                               progreso_total=0, resultado=None, zip_path='', fase='Empezando…')
        resumen = trabajo()
        with dehu_lock:
            dehu_estado['resultado'] = {
                'descargados': resumen.get('bajados', 0),
                'entregados': resumen.get('entregados', 0),
                'errores': resumen.get('errores', 0) + (1 if resumen.get('error_entrega') else 0),
                'acceso_no_vale': bool(resumen.get('acceso_no_vale')),
                'sin_acceso': bool(resumen.get('sin_acceso')),
                'mensaje': resumen.get('mensaje', ''),
            }
    except Exception as e:
        _dehu_log(f'[ERROR] Error crítico: {str(e)}')
        with dehu_lock:
            dehu_estado['resultado'] = {'descargados': 0, 'errores': 1}
    finally:
        with dehu_lock:
            dehu_estado.update(procesando=False, fase='')


def ejecutar_todo_redes(cert_context):
    """Thread: baja todo lo de Red.es y lo entrega al gestor de AiKit."""
    _correr_auto(lambda: auto.ejecutar(_crear_dehu_client(cert_context=cert_context),
                                       EXECUTABLE_DIR, _auto_log, _auto_avance))


def ejecutar_subida_guardada():
    """Thread: entrega al gestor lo que ya está bajado. No pide nada a DEHÚ."""
    _correr_auto(lambda: auto.subir_guardado(EXECUTABLE_DIR, _auto_log, _auto_avance))


def _arrancar_auto(objetivo, *args, pide_acceso=False):
    """Arranca una pasada si la sesión y el estado lo permiten. Bajar no
    depende del acceso a AiKit; «subir lo guardado» (`pide_acceso`) sí."""
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    if pide_acceso and not auto.estado(EXECUTABLE_DIR)['acceso_listo']:
        return jsonify({'exito': False, 'falta_acceso': True,
                        'mensaje': 'Para subir hay que configurar antes el acceso a AiKit.'}), 400
    with dehu_lock:
        if dehu_estado['procesando']:
            return jsonify({'exito': False, 'mensaje': 'Ya hay un proceso en ejecución'}), 400
    thread = threading.Thread(target=objetivo, args=args)
    thread.daemon = True
    thread.start()
    return jsonify({'exito': True, 'mensaje': 'Proceso iniciado'})


@app.route('/api/auto/estado', methods=['GET'])
def auto_estado():
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    return jsonify({'exito': True, **auto.estado(EXECUTABLE_DIR)})


@app.route('/api/auto/acceso', methods=['POST'])
def auto_acceso():
    """Comprueba con AiKit el usuario y la contraseña del gestor y, solo si
    los acepta, los guarda. La contraseña no se devuelve ni se anota."""
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    r = auto.comprobar_y_guardar_acceso(EXECUTABLE_DIR, data.get('usuario'), data.get('password'))
    return jsonify({**auto.estado(EXECUTABLE_DIR), **r}), (200 if r['exito'] else 400)


@app.route('/api/auto/paquete', methods=['POST'])
def auto_paquete():
    """Salida de emergencia: prepara un ZIP con lo que espera subida para
    enviarlo a AiKit a mano, y abre su carpeta. No necesita acceso a AiKit."""
    if 'nif' not in session:
        return jsonify({'exito': False, 'mensaje': 'Sesión no iniciada'}), 401
    with dehu_lock:
        if dehu_estado['procesando']:
            return jsonify({'exito': False, 'mensaje': 'Hay una pasada en marcha. Espera a que termine.'}), 400
    try:
        r = auto.preparar_envio_a_mano(EXECUTABLE_DIR)
    except OSError as e:
        return jsonify({'exito': False, 'mensaje': f'No se ha podido preparar el ZIP ({e}).'}), 500
    if sys.platform == 'win32':
        try:
            os.startfile(r['carpeta'])      # el Explorador de Windows, en la carpeta del paquete
        except OSError:
            pass
    return jsonify({'exito': True, **r})


@app.route('/api/auto/ejecutar', methods=['POST'])
def auto_ejecutar():
    return _arrancar_auto(ejecutar_todo_redes, _cert_de_sesion())


@app.route('/api/auto/subir', methods=['POST'])
def auto_subir():
    return _arrancar_auto(ejecutar_subida_guardada, pide_acceso=True)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    # `app.py auto <orden>`: pasada «todo lo de Red.es» sin pantalla (auto.py).
    if len(sys.argv) > 1 and sys.argv[1] == 'auto':
        sys.exit(auto.main(sys.argv[2:], EXECUTABLE_DIR,
                           lambda ctx, log: _crear_dehu_client(cert_context=ctx)))

    # Una sola copia abierta: en Windows dos pueden quedarse con el mismo puerto
    # y trabajar a la vez sobre los mismos ficheros. La segunda abre la pantalla
    # de la primera y termina.
    import socket
    import webbrowser
    with socket.socket() as _prueba:
        _prueba.settimeout(1)
        if _prueba.connect_ex(('127.0.0.1', 60004)) == 0:
            print('El programa ya está abierto en este ordenador. Se abre su pantalla en el navegador.')
            webbrowser.open_new('http://localhost:60004')
            sys.exit(0)

    print("=" * 60)
    print("  DESCARGADOR DEHÚ LITE")
    print("  http://localhost:60004")
    print("=" * 60)
    
    import threading
    def _abrir_navegador():
        time.sleep(1.5)
        webbrowser.open_new('http://localhost:60004')
        
    threading.Thread(target=_abrir_navegador, daemon=True).start()
    
    # Solo este ordenador: la pantalla no se ofrece a la red local.
    app.run(host='127.0.0.1', port=60004, debug=False, use_reloader=False)
