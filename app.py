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
            if os.path.isfile(fpath) and f != 'dehu_metadata.json':
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
            origen = notif.get('codigoOrigen', '')
            concepto = notif.get('concepto', '')

            _dehu_log(f'[INFO] ({i}/{len(notificaciones)}) [{ident}] Aceptando: {concepto[:60]}...')

            try:
                result = client.peticion_acceso(
                    identificador=ident,
                    codigo_origen=origen,
                    concepto=concepto,
                )

                # Retry automático para error 4204 (concepto no coincide)
                xml_raw_resp = result.get('xml_raw', b'')
                fault_text = ''
                if isinstance(xml_raw_resp, bytes):
                    try:
                        fault_text = xml_raw_resp.decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                else:
                    fault_text = str(xml_raw_resp)

                if '4204' in fault_text:
                    _dehu_log(f'[WARN] Error 4204 (concepto no coincide). Reintentando con concepto fresco...')
                    import re as _re
                    try:
                        fresh = client.localiza_pendientes()
                        raw_xml = fresh.get('xml_raw', b'')
                        raw_xml_str = raw_xml.decode('utf-8', errors='ignore') if isinstance(raw_xml, bytes) else str(raw_xml)
                        ident_pattern = _re.escape(ident)
                        item_match = _re.search(
                            rf'<[^>]*item[^>]*>.*?{ident_pattern}.*?</[^>]*item[^>]*>',
                            raw_xml_str, _re.DOTALL
                        )
                        if item_match:
                            concepto_match = _re.search(
                                r'<[^>]*concepto[^>]*>(.*?)</[^>]*concepto[^>]*>',
                                item_match.group(0), _re.DOTALL
                            )
                            if concepto_match and concepto_match.group(1) != concepto:
                                raw_concepto = concepto_match.group(1)
                                _dehu_log(f'[INFO] Probando concepto del XML raw...')
                                result = client.peticion_acceso(
                                    identificador=ident,
                                    codigo_origen=origen,
                                    concepto=raw_concepto,
                                )
                                if result.get('codigo') == '200':
                                    _dehu_log(f'[OK] Retry exitoso con concepto fresco')
                                    concepto = raw_concepto
                    except Exception as e_retry:
                        _dehu_log(f'[WARN] Error en retry 4204: {e_retry}')

                if result['codigo'] == '200' and result.get('documento_bytes'):
                    doc_name = result.get('documento_nombre') or f'{ident}_documento.zip'
                    doc_name = doc_name.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
                    doc_path = os.path.join(dehu_dir, doc_name)
                    with open(doc_path, 'wb') as f:
                        f.write(result['documento_bytes'])
                    _dehu_log(f'[OK] [{ident}] Descargado: {doc_name} ({len(result["documento_bytes"]):,} bytes)')

                    # Extraer ZIPs internos
                    if doc_name.endswith('.zip'):
                        try:
                            with zipfile.ZipFile(doc_path, 'r') as zf:
                                inner_files = zf.namelist()
                                has_inner_zips = any(f.endswith('.zip') for f in inner_files)
                                if has_inner_zips:
                                    zf.extractall(dehu_dir)
                                    os.remove(doc_path)
                                    _dehu_log(f'[INFO] Extraídos {len(inner_files)} ficheros del ZIP contenedor')
                        except zipfile.BadZipFile:
                            _dehu_log(f'[WARN] {doc_name} no es un ZIP válido, se mantiene como está')

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
                    doc_name = result.get('documento_nombre') or f'{ident}_documento.zip'
                    doc_name = doc_name.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
                    doc_path = os.path.join(dehu_dir, doc_name)
                    with open(doc_path, 'wb') as f:
                        f.write(result['documento_bytes'])
                    _dehu_log(f'[OK] [{ident}] Descargado: {doc_name} ({len(result["documento_bytes"]):,} bytes)')

                    # Extraer ZIPs internos
                    if doc_name.endswith('.zip'):
                        try:
                            with zipfile.ZipFile(doc_path, 'r') as zf:
                                inner_files = zf.namelist()
                                has_inner_zips = any(f.endswith('.zip') for f in inner_files)
                                if has_inner_zips:
                                    zf.extractall(dehu_dir)
                                    os.remove(doc_path)
                                    _dehu_log(f'[INFO] Extraídos {len(inner_files)} ficheros del ZIP contenedor')
                        except zipfile.BadZipFile:
                            _dehu_log(f'[WARN] {doc_name} no es un ZIP válido')

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

        # Capturar contexto de certificado para el thread
        sid = session.get('session_id', '')
        cert_ctx = cert_storage.get(sid)
        if cert_ctx:
            cert_ctx = dict(cert_ctx)
            cert_ctx['nif'] = session.get('nif', '')
            cert_ctx['nombre'] = session.get('nombre', '')
            cert_ctx['cif_empresa'] = session.get('cif_empresa', '')

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

        sid = session.get('session_id', '')
        cert_ctx = cert_storage.get(sid)
        if cert_ctx:
            cert_ctx = dict(cert_ctx)
            cert_ctx['nif'] = session.get('nif', '')
            cert_ctx['nombre'] = session.get('nombre', '')
            cert_ctx['cif_empresa'] = session.get('cif_empresa', '')

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
# MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    print("=" * 60)
    print("  DESCARGADOR DEHÚ LITE")
    print("  http://localhost:60004")
    print("=" * 60)
    
    import threading
    import webbrowser
    def _abrir_navegador():
        time.sleep(1.5)
        webbrowser.open_new('http://localhost:60004')
        
    threading.Thread(target=_abrir_navegador, daemon=True).start()
    
    app.run(host='0.0.0.0', port=60004, debug=True, use_reloader=False)
