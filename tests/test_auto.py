"""
Pruebas de la pasada «todo lo de Red.es». No hay red: DEHÚ se sustituye por
respuestas SOAP de mentira (se ejercita el cliente real, con un certificado
autofirmado creado aquí) y el gestor de AiKit por una sesión de mentira.

    python -m pytest tests -q
"""

import base64
import datetime
import io
import json
import os
import sys
import types
import zipfile

import pytest
from lxml import etree

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto          # noqa: E402
import descarga      # noqa: E402
from dehu_client import DEHuClient, DEHuConfig   # noqa: E402

PFX_PASSWORD = 'clave-del-certificado-de-prueba'
GESTOR_PASSWORD = 'clave-del-gestor-de-prueba'
RED_ES = ('EA0044367', 'Red.es')
AEAT = ('EA0028512', 'Agencia Tributaria')


# ── Certificado autofirmado (solo para firmar las peticiones de prueba) ──

@pytest.fixture(scope='session')
def pfx_b64():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PRUEBA SL')])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(ahora - datetime.timedelta(days=1))
            .not_valid_after(ahora + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(
        b'prueba', key, cert, None, serialization.BestAvailableEncryption(PFX_PASSWORD.encode()))
    return base64.b64encode(pfx).decode()


# ── DEHÚ de mentira ──

def _zip_bytes(nombre_pdf='documento.pdf'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(nombre_pdf, b'%PDF-1.4 prueba')
    return buf.getvalue()


def _item(ident, tipo, emisor=RED_ES, concepto=None):
    return {'identificador': ident, 'codigoOrigen': f'ORG-{ident}', 'tipoEnvio': tipo,
            'concepto': concepto or f'KC/00000{ident} - Requerimiento', 'emisor': emisor,
            'fechaPuestaDisposicion': '2026-11-10T09:00:00'}


class Respuesta:
    status_code = 200

    def __init__(self, content, content_type='text/xml'):
        self.content = content
        self.headers = {'Content-Type': content_type}


class DehuFalso:
    """Sustituye DEHuClient._soap_call: contesta como DEHÚ y apunta lo pedido."""

    def __init__(self, pendientes=(), realizadas=()):
        self.pendientes = list(pendientes)
        self.realizadas = list(realizadas)
        self.aceptadas = []        # identificadores con PeticionAcceso
        self.consultadas = []      # identificadores con ConsultaRealizadas
        self.codigo_consulta = {}  # ident -> código de error forzado
        self.fallo_red = set()     # ident -> lanza excepción

    def __call__(self, url, accion, xml_bytes):
        assert 'redsara.es' in url
        raiz = etree.fromstring(xml_bytes)
        ident = (raiz.xpath("string(.//*[local-name()='identificador'])") or '').strip()
        if accion == 'Localiza':
            return Respuesta(self._listado(self.pendientes))
        if accion == 'LocalizaRealizadas':
            return Respuesta(self._listado(self.realizadas))
        if ident in self.fallo_red:
            raise ConnectionResetError('corte de red de prueba')
        if accion == 'PeticionAcceso':
            self.aceptadas.append(ident)
            item = next(i for i in self.pendientes if i['identificador'] == ident)
            self.pendientes.remove(item)
            self.realizadas.append(item)
            return self._documento(ident)
        if accion == 'ConsultaRealizadas':
            self.consultadas.append(ident)
            if ident in self.codigo_consulta:
                return Respuesta(self._xml_codigo(self.codigo_consulta[ident], 'no disponible'))
            return self._documento(ident)
        raise AssertionError(f'acción inesperada: {accion}')

    @staticmethod
    def _xml_codigo(codigo, desc, extra=''):
        return (f'<r><codigoRespuesta>{codigo}</codigoRespuesta>'
                f'<descripcionRespuesta>{desc}</descripcionRespuesta>{extra}</r>').encode()

    def _listado(self, items):
        cuerpo = ''.join(
            f"<item><identificador>{i['identificador']}</identificador>"
            f"<codigoOrigen>{i['codigoOrigen']}</codigoOrigen><concepto>{i['concepto']}</concepto>"
            f"<tipoEnvio>{i['tipoEnvio']}</tipoEnvio>"
            f"<fechaPuestaDisposicion>{i['fechaPuestaDisposicion']}</fechaPuestaDisposicion>"
            f"<organismoEmisor><codigoOrganismo>{i['emisor'][0]}</codigoOrganismo>"
            f"<nombreOrganismo>{i['emisor'][1]}</nombreOrganismo></organismoEmisor></item>"
            for i in items)
        return self._xml_codigo('200', 'OK', cuerpo)

    def _documento(self, ident):
        xml = self._xml_codigo('200', 'OK', f'<nombre>{ident}.zip</nombre>')
        cuerpo = (b'--b\r\nContent-Type: application/xop+xml\r\n\r\n' + xml +
                  b'\r\n--b\r\nContent-Type: application/zip\r\nContent-Transfer-Encoding: base64\r\n\r\n' +
                  base64.b64encode(_zip_bytes(f'{ident}.pdf')) + b'\r\n--b--\r\n')
        return Respuesta(cuerpo, 'multipart/related; boundary="b"')


# ── Gestor de mentira ──

class RespuestaGestor:
    def __init__(self, datos, status=200):
        self._datos, self.status_code = datos, status

    def json(self):
        return self._datos


class GestorFalso:
    """Sustituye requests.Session en auto.py."""
    peticiones = []          # (método, url)
    recibidos = {}           # nombre -> bytes del ZIP
    cuerpos = []             # todo lo enviado, para buscar secretos
    login_ok = True
    error_claude = None

    def __init__(self):
        self._sondeos = 0

    @classmethod
    def reiniciar(cls):
        cls.peticiones, cls.recibidos, cls.cuerpos = [], {}, []
        cls.login_ok, cls.error_claude = True, None

    def post(self, url, json=None, files=None, timeout=None, allow_redirects=True):
        assert url.startswith(auto.GESTOR_URL + '/'), url
        assert allow_redirects is False
        type(self).peticiones.append(('POST', url))
        if url.endswith('/login-password'):
            type(self).cuerpos.append(repr(json).encode())
            ok = self.login_ok and json['password'] == GESTOR_PASSWORD
            return RespuestaGestor({'exito': ok, 'mensaje': 'Credenciales incorrectas'}, 200 if ok else 401)
        if url.endswith('/api/upload-zips'):
            self._lote = {nombre: f.read() for _campo, (nombre, f, _tipo) in files}
            type(self).cuerpos.extend(self._lote.values())
            return RespuestaGestor({'exito': True, 'carpeta': '/app/outputs/manual_uploads/manual_x'})
        if url.endswith('/procesar'):
            assert json == {'carpeta_input': '/app/outputs/manual_uploads/manual_x', 'origen': 'manual'}
            self._sondeos = 0
            return RespuestaGestor({'exito': True})
        raise AssertionError(url)

    def get(self, url, timeout=None, allow_redirects=True):
        assert url.startswith(auto.GESTOR_URL + '/'), url
        type(self).peticiones.append(('GET', url))
        if url.endswith('/progreso'):
            self._sondeos += 1
            if self._sondeos == 1:
                return RespuestaGestor({'procesando': True})
            if not self.error_claude:
                type(self).recibidos.update(self._lote)
            return RespuestaGestor({'procesando': False, 'exitosos': len(self._lote), 'errores': 0,
                                    'error_claude': self.error_claude})
        return RespuestaGestor({})


# ── Montaje ──

@pytest.fixture
def entorno(tmp_path, monkeypatch, pfx_b64):
    GestorFalso.reiniciar()
    # Solo auto.py ve el gestor de mentira; el cliente DEHÚ conserva su requests.
    monkeypatch.setattr(auto, 'requests', types.SimpleNamespace(
        Session=GestorFalso, RequestException=auto.requests.RequestException))
    monkeypatch.setattr(auto, 'ESPERA_SONDEO', 0)
    monkeypatch.setattr(auto, 'proteger', lambda t: 'dpapi:' + t[::-1])
    monkeypatch.setattr(auto, 'desproteger', lambda v: v[len('dpapi:'):][::-1])
    auto.guardar_config(str(tmp_path), {**auto.CONFIG_POR_DEFECTO, 'gestor_usuario': 'B00000000',
                                        'gestor_password_dpapi': auto.proteger(GESTOR_PASSWORD)})

    def montar(dehu):
        monkeypatch.setattr(DEHuClient, '_soap_call', lambda self, url, accion, xml: dehu(url, accion, xml))
        return DEHuClient(DEHuConfig(pfx_base64=pfx_b64, key_password=PFX_PASSWORD,
                                     nif='B00000000', nombre='PRUEBA SL'))

    class E:
        base = str(tmp_path)
        logs = []

        def pasada(self, dehu):
            return auto.ejecutar(montar(dehu), self.base, self.logs.append)

        def permitir_aceptar(self, valor=True):
            auto.guardar_config(self.base, {**auto.leer_config(self.base),
                                            'aceptar_notificaciones_pendientes': valor})

        def por_subir(self):
            return sorted(os.listdir(os.path.join(self.base, 'outputs', 'auto', 'por_subir')))

    return E()


# ── Pruebas ──

def test_solo_red_es_y_sin_aceptar_notificaciones_por_defecto(entorno):
    dehu = DehuFalso(
        pendientes=[_item('P1', '1'), _item('P2', '2'), _item('P3', '1', AEAT), _item('P4', '2', AEAT)],
        realizadas=[_item('R1', '2'), _item('R2', '1'), _item('R3', '2', AEAT)])
    r = entorno.pasada(dehu)

    assert dehu.aceptadas == ['P1']                    # la comunicación de Red.es, nada más
    assert sorted(dehu.consultadas) == ['R1', 'R2']    # nada de la AEAT
    assert r['notificaciones_sin_aceptar'] == 1 and r['bajados'] == 3 and r['entregados'] == 3
    assert sorted(GestorFalso.recibidos) == ['P1.zip', 'R1.zip', 'R2.zip']
    assert entorno.por_subir() == []


def test_con_permiso_se_aceptan_las_notificaciones_de_red_es(entorno):
    entorno.permitir_aceptar()
    dehu = DehuFalso(pendientes=[_item('P1', '1'), _item('P2', '2'), _item('P4', '2', AEAT)])
    entorno.pasada(dehu)
    assert dehu.aceptadas == ['P1', 'P2']


def test_el_permiso_solo_se_activa_con_true(entorno):
    entorno.permitir_aceptar('true')        # texto, no booleano
    assert auto.leer_config(entorno.base)['aceptar_notificaciones_pendientes'] is False
    dehu = DehuFalso(pendientes=[_item('P2', '2')])
    entorno.pasada(dehu)
    assert dehu.aceptadas == []


def test_un_envio_sin_tipo_se_trata_como_notificacion(entorno):
    dehu = DehuFalso(pendientes=[_item('P9', '')])
    entorno.pasada(dehu)
    assert dehu.aceptadas == []


def test_segunda_pasada_no_repite(entorno):
    dehu = DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')])
    entorno.pasada(dehu)
    subidas = len([p for p in GestorFalso.peticiones if p[1].endswith('/api/upload-zips')])
    r = entorno.pasada(dehu)                # P1 aparece ahora también en realizadas
    assert dehu.aceptadas == ['P1'] and dehu.consultadas == ['R1']
    assert r['bajados'] == 0 and r.get('entregados', 0) == 0
    assert len([p for p in GestorFalso.peticiones if p[1].endswith('/api/upload-zips')]) == subidas


def test_si_la_entrega_falla_se_guarda_y_se_reintenta_sin_volver_a_dehu(entorno):
    dehu = DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')])
    GestorFalso.login_ok = False
    r = entorno.pasada(dehu)
    assert 'error_entrega' in r and entorno.por_subir() == ['P1.zip', 'R1.zip']
    assert GestorFalso.recibidos == {}

    GestorFalso.login_ok = True
    r = entorno.pasada(dehu)
    assert r['bajados'] == 0 and r['entregados'] == 2 and entorno.por_subir() == []
    assert dehu.aceptadas == ['P1'] and dehu.consultadas == ['R1']


def test_lote_que_el_gestor_no_procesa_no_se_da_por_entregado(entorno):
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    GestorFalso.error_claude = 'Claude no responde'
    r = entorno.pasada(dehu)
    assert 'Claude no responde' in r['error_entrega'] and entorno.por_subir() == ['R1.zip']
    reg = auto.Registro(entorno.base)
    assert reg.envio('R1')['estado'] == 'descargado'


def test_todo_lo_enviado_queda_anotado_y_lleva_meta(entorno):
    dehu = DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')])
    entorno.pasada(dehu)
    lineas = open(os.path.join(entorno.base, 'outputs', 'auto', 'enviado.log'), encoding='utf-8').read().splitlines()
    assert len(lineas) == len(GestorFalso.recibidos) == 2
    import hashlib
    for linea in lineas:
        _cuando, destino, nombre, _bytes, sha, resultado = linea.split('\t')
        assert destino == f'POST {auto.GESTOR_URL}/api/upload-zips' and resultado == 'entregado'
        assert hashlib.sha256(GestorFalso.recibidos[nombre]).hexdigest() == sha
        meta = json.loads(zipfile.ZipFile(io.BytesIO(GestorFalso.recibidos[nombre])).read('meta.json'))
        assert meta['identificador'] == nombre[:-4]
        assert meta['organismoEmisor_codigoOrganismo'] == 'EA0044367'
        assert meta['fechaPuestaDisposicion'] == '2026-11-10T09:00:00'
    # Copia local de lo entregado
    assert sorted(os.listdir(os.path.join(entorno.base, 'outputs', 'auto', 'entregado'))) == ['P1.zip', 'R1.zip']


def test_el_certificado_y_su_clave_no_salen(entorno, pfx_b64):
    dehu = DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')])
    entorno.pasada(dehu)
    enviado = b''.join(GestorFalso.cuerpos)
    assert PFX_PASSWORD.encode() not in enviado
    assert base64.b64decode(pfx_b64)[:64] not in enviado
    assert all(url.startswith(auto.GESTOR_URL + '/') for _m, url in GestorFalso.peticiones)
    # En el disco tampoco queda la clave del gestor en claro
    assert GESTOR_PASSWORD not in open(os.path.join(entorno.base, 'auto_config.json'), encoding='utf-8').read()


def test_un_no_de_dehu_se_deja_de_pedir_a_la_tercera(entorno):
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    dehu.codigo_consulta['R1'] = '4300'
    for _ in range(5):
        entorno.pasada(dehu)
    assert dehu.consultadas == ['R1'] * auto.MAX_RESPUESTAS_NEGATIVAS
    assert auto.Registro(entorno.base).envio('R1')['estado'] == 'sin_documento'


def test_un_corte_de_red_no_cuenta_y_se_reintenta(entorno):
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    dehu.fallo_red.add('R1')
    for _ in range(5):
        r = entorno.pasada(dehu)
        assert r['errores'] == 1
    dehu.fallo_red.clear()
    r = entorno.pasada(dehu)
    assert r['bajados'] == 1 and r['entregados'] == 1


def test_limite_de_consultas_para_la_pasada(entorno):
    dehu = DehuFalso(realizadas=[_item('R1', '2'), _item('R2', '2')])
    dehu.codigo_consulta['R1'] = '4225'
    r = entorno.pasada(dehu)
    assert r['limite'] is True and dehu.consultadas == ['R1']
    assert auto.Registro(entorno.base).envio('R1') == {}      # no cuenta como «no»


def test_restos_de_una_pasada_interrumpida_se_entregan(entorno):
    resto = os.path.join(entorno.base, 'outputs', 'auto', 'tmp', 'X1')
    os.makedirs(resto)
    with open(os.path.join(resto, 'X1.zip'), 'wb') as f:
        f.write(_zip_bytes())
    r = entorno.pasada(DehuFalso())
    assert r['entregados'] == 1 and 'X1.zip' in GestorFalso.recibidos


def test_sin_usuario_del_gestor_se_baja_y_se_guarda(entorno):
    auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))
    r = entorno.pasada(DehuFalso(realizadas=[_item('R1', '2')]))
    assert r['bajados'] == 1 and 'error_entrega' in r and entorno.por_subir() == ['R1.zip']
    assert GestorFalso.peticiones == []


def test_es_red_es():
    assert descarga.es_red_es({'organismoEmisor_codigoOrganismo': 'EA0044367'})
    assert descarga.es_red_es({'organismoEmisor_nombreOrganismo': 'Entidad Pública Empresarial Red.es'})
    assert not descarga.es_red_es({'organismoEmisor_codigoOrganismo': 'EA0028512',
                                   'organismoEmisor_nombreOrganismo': 'Agencia Tributaria'})
    assert not descarga.es_red_es({})


def test_zip_contenedor_se_extrae_y_cada_zip_lleva_meta(tmp_path):
    interior = _zip_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('a.zip', interior)
        zf.writestr('b.zip', interior)
    notif = _item('C1', '2')
    notif['organismoEmisor_codigoOrganismo'] = 'EA0044367'
    rutas = descarga.guardar_documento(
        {'documento_nombre': 'C1.zip', 'documento_bytes': buf.getvalue()},
        notif, notif['concepto'], str(tmp_path), lambda _m: None)
    assert sorted(os.path.basename(r) for r in rutas) == ['a.zip', 'b.zip']
    assert not os.path.exists(tmp_path / 'C1.zip')
    for r in rutas:
        assert json.loads(zipfile.ZipFile(r).read('meta.json'))['identificador'] == 'C1'


def test_la_pantalla_manual_sigue_bajando_lo_marcado(tmp_path, monkeypatch, pfx_b64):
    """La descarga manual usa ahora las piezas comunes: mismo resultado que antes."""
    import app as aplicacion
    monkeypatch.setattr(aplicacion, 'OUTPUTS_DIR', str(tmp_path))
    dehu = DehuFalso(pendientes=[_item('P1', '1'), _item('P2', '2'), _item('P4', '2', AEAT)])
    monkeypatch.setattr(DEHuClient, '_soap_call', lambda self, url, accion, xml: dehu(url, accion, xml))
    ctx = {'pfx_b64': pfx_b64, 'pfx_pwd': PFX_PASSWORD, 'nif': 'B00000000', 'nombre': 'PRUEBA SL'}

    marcadas = [{k: v for k, v in i.items() if k != 'emisor'} for i in dehu.pendientes if i['identificador'] != 'P1']
    aplicacion.ejecutar_descarga_pendientes(marcadas, ctx)

    assert dehu.aceptadas == ['P2', 'P4']       # la pantalla manual baja lo que la persona marca
    assert aplicacion.dehu_estado['resultado'] == {'descargados': 2, 'errores': 0}
    with zipfile.ZipFile(aplicacion.dehu_estado['zip_path']) as zf:
        assert sorted(zf.namelist()) == ['P2.zip', 'P4.zip']
        assert json.loads(zipfile.ZipFile(io.BytesIO(zf.read('P2.zip'))).read('meta.json'))['identificador'] == 'P2'


def test_rutas_del_boton_piden_sesion():
    import app as aplicacion
    cliente = aplicacion.app.test_client()
    assert cliente.get('/api/auto/estado').status_code == 401
    assert cliente.post('/api/auto/ejecutar').status_code == 401
