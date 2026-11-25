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
        self.fechas_desde = []     # «Fecha Desde» de cada LocalizaRealizadas
        self.codigo_acceso = None  # código de error forzado en PeticionAcceso
        self.intentos_acceso = []  # identificadores con PeticionAcceso, salga bien o no

    def __call__(self, url, accion, xml_bytes):
        assert 'redsara.es' in url
        raiz = etree.fromstring(xml_bytes)
        ident = (raiz.xpath("string(.//*[local-name()='identificador'])") or '').strip()
        if accion == 'Localiza':
            return Respuesta(self._listado(self.pendientes))
        if accion == 'LocalizaRealizadas':
            # Como el DEHÚ real: una «Fecha Desde» de más de 30 días es un 4224.
            desde = datetime.datetime.fromisoformat(raiz.xpath("string(.//*[local-name()='fechaDesde'])"))
            self.fechas_desde.append(desde)
            if datetime.datetime.now() - desde > datetime.timedelta(days=30):
                return Respuesta(self._xml_codigo('4224', 'La "Fecha Desde" no puede ser anterior a "30" días'))
            return Respuesta(self._listado(self.realizadas))
        if ident in self.fallo_red:
            raise ConnectionResetError('corte de red de prueba')
        if accion == 'PeticionAcceso':
            self.intentos_acceso.append(ident)
            if self.codigo_acceso:
                return Respuesta(self._xml_codigo(
                    self.codigo_acceso, 'El número de peticiones diarias al método peticionAcceso ha superado el máximo permitido'))
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
            f"<nombreOrganismo>{i['emisor'][1]}</nombreOrganismo></organismoEmisor>"
            f"<vinculo>1</vinculo><titular><nifTitular>B11111111</nifTitular>"
            f"<nombreTitular>CLIENTE DE PRUEBA SL</nombreTitular></titular></item>"
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
    pendientes = []          # cada lista de pendientes recibida
    estado_pendientes = 200  # 404 = gestor que aún no tiene la ruta
    sin_red = False          # la entrada al gestor falla por la red
    entradas = 0             # veces que se ha entrado
    no_admite = set()        # un lote con uno de estos ZIP no se procesa (todo a cero)
    no_termina = set()       # un lote con uno de estos ZIP no termina nunca
    subidas_401 = 0          # próximas subidas que el gestor rechaza por sesión caducada
    subidas_sin_red = 0      # próximas subidas que se cortan
    sondeos_sin_red = 0      # próximas consultas de progreso que se cortan

    def __init__(self):
        self._sondeos = 0
        self._ocupada = False    # como el gestor real: una sesión con un lote en curso no admite otro

    @classmethod
    def reiniciar(cls):
        cls.peticiones, cls.recibidos, cls.cuerpos = [], {}, []
        cls.login_ok, cls.error_claude = True, None
        cls.pendientes, cls.estado_pendientes = [], 200
        cls.sin_red, cls.entradas = False, 0
        cls.no_admite, cls.no_termina = set(), set()
        cls.subidas_401 = cls.subidas_sin_red = cls.sondeos_sin_red = 0

    @classmethod
    def subidas(cls):
        return len([p for p in cls.peticiones if p[1].endswith('/api/upload-zips')])

    def post(self, url, json=None, files=None, timeout=None, allow_redirects=True):
        assert url.startswith(auto.GESTOR_URL + '/'), url
        assert allow_redirects is False
        type(self).peticiones.append(('POST', url))
        if url.endswith('/login-password'):
            if self.sin_red:
                raise auto.requests.RequestException('corte de red de prueba')
            type(self).cuerpos.append(repr(json).encode())
            ok = self.login_ok and json['password'] == GESTOR_PASSWORD
            type(self).entradas += ok
            return RespuestaGestor({'exito': ok, 'mensaje': 'Credenciales incorrectas'}, 200 if ok else 401)
        if url.endswith('/api/dehu-pendientes'):
            type(self).cuerpos.append(repr(json).encode())
            if self.estado_pendientes != 200:
                return RespuestaGestor({'exito': False, 'mensaje': 'fallo de prueba'}, self.estado_pendientes)
            type(self).pendientes.append(json)
            return RespuestaGestor({'exito': True})
        if url.endswith('/api/upload-zips'):
            if type(self).subidas_sin_red:
                type(self).subidas_sin_red -= 1
                raise auto.requests.RequestException('corte de red de prueba')
            if type(self).subidas_401:
                type(self).subidas_401 -= 1
                return RespuestaGestor({'error': 'Sesión no iniciada', 'redirect': '/login'}, 401)
            self._lote = {nombre: f.read() for _campo, (nombre, f, _tipo) in files}
            type(self).cuerpos.extend(self._lote.values())
            return RespuestaGestor({'exito': True, 'carpeta': '/app/outputs/manual_uploads/manual_x'})
        if url.endswith('/procesar'):
            assert json == {'carpeta_input': '/app/outputs/manual_uploads/manual_x', 'origen': 'manual'}
            if self._ocupada:
                return RespuestaGestor({'exito': False, 'mensaje': 'Ya hay un proceso de clasificación en ejecución'}, 400)
            self._sondeos = 0
            return RespuestaGestor({'exito': True})
        raise AssertionError(url)

    def get(self, url, timeout=None, allow_redirects=True):
        assert url.startswith(auto.GESTOR_URL + '/'), url
        type(self).peticiones.append(('GET', url))
        if url.endswith('/progreso'):
            if type(self).sondeos_sin_red:
                type(self).sondeos_sin_red -= 1
                raise auto.requests.RequestException('corte de red de prueba')
            self._sondeos += 1
            if self._sondeos == 1:
                return RespuestaGestor({'procesando': True})
            if self.no_termina & set(self._lote):
                self._ocupada = True
                return RespuestaGestor({'procesando': True, 'total': len(self._lote)})
            if self.no_admite & set(self._lote):     # como un gestor reiniciado a mitad de lote
                return RespuestaGestor({'procesando': False, 'total': 0, 'exitosos': 0, 'errores': 0,
                                        'error_claude': None})
            if not self.error_claude:
                type(self).recibidos.update(self._lote)
            return RespuestaGestor({'procesando': False, 'total': len(self._lote), 'exitosos': len(self._lote),
                                    'errores': 0, 'error_claude': self.error_claude})
        return RespuestaGestor({})


# ── Montaje ──

@pytest.fixture
def entorno(tmp_path, monkeypatch, pfx_b64):
    GestorFalso.reiniciar()
    # Solo auto.py ve el gestor de mentira; el cliente DEHÚ conserva su requests.
    monkeypatch.setattr(auto, 'requests', types.SimpleNamespace(
        Session=GestorFalso, RequestException=auto.requests.RequestException))
    monkeypatch.setattr(auto, 'ESPERA_SONDEO', 0)
    monkeypatch.setattr(auto, 'ESPERAS_TRAS_FALLO', (0, 0))
    monkeypatch.setattr(auto, 'proteger', lambda t: 'dpapi:' + t[::-1])
    monkeypatch.setattr(auto, 'desproteger', lambda v: v[len('dpapi:'):][::-1])
    # Un acceso que AiKit ya ha aceptado (lo que deja «Configurar acceso a AiKit»).
    auto.guardar_config(str(tmp_path), {**auto.CONFIG_POR_DEFECTO, 'gestor_usuario': 'B00000000',
                                        'gestor_password_dpapi': auto.proteger(GESTOR_PASSWORD),
                                        'gestor_comprobado_en': '2026-01-01T00:00:00'})

    def montar(dehu):
        monkeypatch.setattr(DEHuClient, '_soap_call', lambda self, url, accion, xml: dehu(url, accion, xml))
        return DEHuClient(DEHuConfig(pfx_base64=pfx_b64, key_password=PFX_PASSWORD,
                                     nif='B00000000', nombre='PRUEBA SL'))

    class E:
        base = str(tmp_path)
        logs = []
        pfx_b64 = None

        def pasada(self, dehu):
            return auto.ejecutar(montar(dehu), self.base, self.logs.append)

        def permitir_aceptar(self, valor=True):
            auto.guardar_config(self.base, {**auto.leer_config(self.base),
                                            'aceptar_notificaciones_pendientes': valor})

        def por_subir(self):
            return sorted(os.listdir(os.path.join(self.base, 'outputs', 'auto', 'por_subir')))

        def guardados(self, nombres):
            """Deja ZIP ya bajados en por_subir, como tras pasadas que no pudieron subir."""
            carpeta = auto.Registro(self.base).por_subir
            for nombre in nombres:
                with open(os.path.join(carpeta, nombre), 'wb') as f:
                    f.write(_zip_bytes())

        def subir(self, avance=None):
            return auto.subir_guardado(self.base, self.logs.append, avance)

    E.pfx_b64 = pfx_b64
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

    # Con el acceso rechazado no se insiste: hay que configurarlo de nuevo.
    GestorFalso.login_ok = True
    entradas = GestorFalso.entradas
    r = entorno.pasada(dehu)
    assert r['sin_acceso'] is True and entorno.por_subir() == ['P1.zip', 'R1.zip'] and GestorFalso.entradas == entradas

    assert auto.comprobar_y_guardar_acceso(entorno.base, 'B00000000', GESTOR_PASSWORD)['exito'] is True
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
    assert sum('/api/dehu-pendientes' in linea for linea in lineas) == 1     # la lista de pendientes, aparte
    lineas = [linea for linea in lineas if '/api/dehu-pendientes' not in linea]
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


def test_un_envio_sin_consultas_no_para_la_pasada(entorno):
    # 4225 es de ese envío: los demás se bajan, y a la tercera se deja de pedir.
    dehu = DehuFalso(realizadas=[_item('R1', '2'), _item('R2', '2')])
    dehu.codigo_consulta['R1'] = '4225'
    r = entorno.pasada(dehu)
    assert r['limite'] is False and r['consultas_agotadas'] == 1 and r['bajados'] == 1
    assert dehu.consultadas == ['R1', 'R2'] and 'R2.zip' in GestorFalso.recibidos
    for _ in range(4):
        entorno.pasada(dehu)
    assert dehu.consultadas.count('R1') == auto.MAX_RESPUESTAS_NEGATIVAS
    assert auto.Registro(entorno.base).envio('R1')['ultima_respuesta'].startswith('4225')


def test_muchos_4225_seguidos_paran_la_pasada(entorno):
    idents = [f'R{n}' for n in range(auto.MAX_4225_SEGUIDOS + 2)]
    dehu = DehuFalso(realizadas=[_item(i, '2') for i in idents])
    dehu.codigo_consulta = {i: '4225' for i in idents}
    r = entorno.pasada(dehu)
    assert r['limite'] is True and len(dehu.consultadas) == auto.MAX_4225_SEGUIDOS


def test_el_tope_diario_de_aperturas_no_cuenta_como_no_y_lo_realizado_sigue(entorno):
    dehu = DehuFalso(pendientes=[_item('P1', '1'), _item('P2', '1')], realizadas=[_item('R1', '2')])
    dehu.codigo_acceso = '4305'
    r = entorno.pasada(dehu)
    assert r['tope_diario_aperturas'] is True and dehu.aceptadas == []
    assert dehu.intentos_acceso == ['P1']                     # no se insiste con P2
    assert auto.Registro(entorno.base).envio('P1') == {}      # no cuenta como «no»
    assert dehu.consultadas == ['R1'] and r['bajados'] == 1
    dehu.codigo_acceso = None
    r = entorno.pasada(dehu)
    assert dehu.aceptadas == ['P1', 'P2'] and r['bajados'] == 2


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


def test_las_pendientes_sin_aceptar_se_informan_sin_abrirlas(entorno):
    dehu = DehuFalso(pendientes=[_item('P1', '1'), _item('P2', '2', concepto='KC/0000012345 - Requerimiento'),
                                 _item('P4', '2', AEAT)])
    r = entorno.pasada(dehu)

    assert dehu.aceptadas == ['P1'] and dehu.consultadas == []      # P2 ni se acepta ni se consulta
    assert r['pendientes_informadas'] == 1 and 'pendientes_lista' not in r
    informe = GestorFalso.pendientes[-1]
    assert informe['listado_completo'] is True
    assert informe['pendientes'] == [{
        'identificador': 'P2', 'codigoOrigen': 'ORG-P2', 'concepto': 'KC/0000012345 - Requerimiento',
        'fechaPuestaDisposicion': '2026-11-10T09:00:00', 'tipoEnvio': '2',
        'organismoEmisor_codigoOrganismo': 'EA0044367', 'organismoEmisor_nombreOrganismo': 'Red.es',
        'titular_nifTitular': 'B11111111', 'titular_nombreTitular': 'CLIENTE DE PRUEBA SL', 'vinculo': '1'}]
    # Lo enviado queda tal cual en disco y anotado.
    carpeta = os.path.join(entorno.base, 'outputs', 'auto')
    with open(os.path.join(carpeta, 'pendientes_informadas.json'), encoding='utf-8') as f:
        assert json.load(f) == informe
    with open(os.path.join(carpeta, 'enviado.log'), encoding='utf-8') as f:
        assert any('/api/dehu-pendientes\tpendientes_informadas.json' in linea and linea.rstrip().endswith('entregado')
                   for linea in f)


def test_sin_pendientes_se_informa_una_lista_vacia(entorno):
    r = entorno.pasada(DehuFalso(realizadas=[_item('R1', '2')]))
    assert r['pendientes_informadas'] == 0 and GestorFalso.pendientes[-1]['pendientes'] == []
    r = entorno.pasada(DehuFalso(realizadas=[_item('R1', '2')]))     # nada que subir: se informa igual
    assert r['pendientes_informadas'] == 0 and len(GestorFalso.pendientes) == 2


def test_con_permiso_solo_se_informa_la_que_no_se_pudo_aceptar(entorno):
    entorno.permitir_aceptar()
    dehu = DehuFalso(pendientes=[_item('P2', '2'), _item('P5', '2')])
    dehu.fallo_red = {'P5'}
    r = entorno.pasada(dehu)
    assert dehu.aceptadas == ['P2']
    assert [p['identificador'] for p in GestorFalso.pendientes[-1]['pendientes']] == ['P5']
    assert r['pendientes_informadas'] == 1


def test_un_gestor_sin_la_ruta_de_pendientes_no_estropea_la_entrega(entorno):
    for estado in (404, 500):
        GestorFalso.reiniciar()
        GestorFalso.estado_pendientes = estado
        dehu = DehuFalso(pendientes=[_item(f'P{estado}', '2')], realizadas=[_item(f'R{estado}', '2')])
        r = entorno.pasada(dehu)
        assert r['pendientes_informadas'] is None and r['entregados'] == 1
        assert 'error_entrega' not in r and ('error_pendientes' in r) == (estado == 500)
        assert GestorFalso.pendientes == []


def test_las_realizadas_se_piden_dentro_de_lo_que_dehu_admite(entorno):
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    r = entorno.pasada(dehu)
    assert r['bajados'] == 1 and r['listado_incompleto'] is False
    hace = datetime.datetime.now() - dehu.fechas_desde[0]
    assert datetime.timedelta(days=29) <= hace <= datetime.timedelta(days=30)


def test_un_listado_que_dehu_rechaza_no_pasa_por_bueno(entorno, monkeypatch, capsys):
    monkeypatch.setattr(auto, 'DIAS_REALIZADAS', 31)
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    r = entorno.pasada(dehu)
    assert r['listado_incompleto'] is True and r['bajados'] == 0
    assert any(linea.startswith('[ERROR] El listado de realizadas') for linea in entorno.logs)
    # La orden de la tarea programada termina con error, no en silencio.
    monkeypatch.setattr(auto, 'cert_context_guardado', lambda base: {})
    cliente = DEHuClient(DEHuConfig(pfx_base64=entorno.pfx_b64, key_password=PFX_PASSWORD,
                                    nif='B00000000', nombre='PRUEBA SL'))
    assert auto.main(['ejecutar'], entorno.base, lambda ctx, log: cliente) == 1


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


def test_rutas_del_boton_piden_sesion(entorno):
    import app as aplicacion
    cliente = aplicacion.app.test_client()
    assert cliente.get('/api/auto/estado').status_code == 401
    assert cliente.post('/api/auto/ejecutar').status_code == 401
    assert cliente.post('/api/auto/subir').status_code == 401
    assert cliente.post('/api/auto/acceso', json={'usuario': 'B00000000', 'password': 'x'}).status_code == 401
    assert GestorFalso.peticiones == []


# ── Acceso a AiKit: se comprueba antes de guardarlo ──

def _config_en_disco(base):
    with open(os.path.join(base, 'auto_config.json'), encoding='utf-8') as f:
        return f.read()


def _todo_el_disco(base):
    """Todo lo que el programa ha escrito en su carpeta, junto."""
    trozos = []
    for carpeta, _dirs, ficheros in os.walk(base):
        for nombre in ficheros:
            with open(os.path.join(carpeta, nombre), 'rb') as f:
                trozos.append(f.read())
    return b''.join(trozos)


@pytest.fixture
def pantalla(entorno, monkeypatch):
    """La pantalla (Flask) con sesión iniciada y trabajando en la carpeta de la prueba."""
    import app as aplicacion
    monkeypatch.setattr(aplicacion, 'EXECUTABLE_DIR', entorno.base)
    cliente = aplicacion.app.test_client()
    with cliente.session_transaction() as sesion:
        sesion['nif'] = 'B00000000'
    return cliente


def test_el_acceso_bueno_se_comprueba_y_se_guarda(entorno):
    auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))
    assert auto.estado(entorno.base)['acceso_listo'] is False
    # Los espacios y el salto de línea de los extremos son restos de pegar.
    r = auto.comprobar_y_guardar_acceso(entorno.base, '  B00000000 ', GESTOR_PASSWORD + '\n')
    assert r['exito'] is True and GESTOR_PASSWORD not in repr(r)
    assert [(m, u.rsplit('/', 1)[1]) for m, u in GestorFalso.peticiones] == [('POST', 'login-password'), ('GET', 'logout')]
    cfg = auto.leer_config(entorno.base)
    assert cfg['gestor_usuario'] == 'B00000000' and cfg['gestor_comprobado_en']
    assert auto.desproteger(cfg['gestor_password_dpapi']) == GESTOR_PASSWORD
    assert GESTOR_PASSWORD not in _config_en_disco(entorno.base)
    estado = auto.estado(entorno.base)
    assert estado['acceso_listo'] is True and estado['gestor_usuario'] == 'B00000000'


def test_un_acceso_que_aikit_rechaza_no_se_guarda(entorno):
    antes = _config_en_disco(entorno.base)
    r = auto.comprobar_y_guardar_acceso(entorno.base, 'B00000000', GESTOR_PASSWORD * 2)   # pegada dos veces
    assert r['exito'] is False and r['motivo'] == 'credenciales' and 'No se ha guardado nada' in r['mensaje']
    assert _config_en_disco(entorno.base) == antes


def test_si_no_se_llega_a_aikit_no_se_guarda(entorno):
    antes = _config_en_disco(entorno.base)
    GestorFalso.sin_red = True
    r = auto.comprobar_y_guardar_acceso(entorno.base, 'B11111111', GESTOR_PASSWORD)
    assert r['exito'] is False and r['motivo'] == 'conexion' and GESTOR_PASSWORD not in r['mensaje']
    assert _config_en_disco(entorno.base) == antes


@pytest.mark.parametrize('password', [GESTOR_PASSWORD + '\x16', 'clave\ncon salto', 'clave\u200bcon hueco'])
def test_un_caracter_que_no_se_ve_se_rechaza_sin_llamar_a_aikit(entorno, password):
    antes = _config_en_disco(entorno.base)
    r = auto.comprobar_y_guardar_acceso(entorno.base, 'B00000000', password)
    assert r['exito'] is False and r['motivo'] == 'datos'
    assert GestorFalso.peticiones == [] and _config_en_disco(entorno.base) == antes


def test_la_contrasena_del_gestor_no_se_devuelve_ni_se_anota(entorno, pantalla, capsys):
    auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))
    mala = pantalla.post('/api/auto/acceso', json={'usuario': 'B00000000', 'password': 'clave-equivocada-de-prueba'})
    assert mala.status_code == 400 and mala.get_json()['motivo'] == 'credenciales'
    buena = pantalla.post('/api/auto/acceso', json={'usuario': 'B00000000', 'password': GESTOR_PASSWORD})
    assert buena.status_code == 200 and buena.get_json()['acceso_listo'] is True
    entorno.pasada(DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')]))
    salida = capsys.readouterr()
    for clave in (GESTOR_PASSWORD, 'clave-equivocada-de-prueba'):
        assert clave not in mala.get_data(as_text=True) + buena.get_data(as_text=True)
        assert clave not in salida.out + salida.err + ''.join(entorno.logs)
        assert clave.encode() not in _todo_el_disco(entorno.base)


def _acceso_de_la_version_anterior(entorno):
    """Guardado pero sin comprobar: lo que deja la versión del 18-11."""
    auto.guardar_config(entorno.base, {**auto.leer_config(entorno.base), 'gestor_comprobado_en': ''})


def test_bajar_no_depende_del_acceso_y_subir_si(entorno, pantalla, monkeypatch):
    import app as aplicacion
    arrancadas = []
    monkeypatch.setattr(aplicacion, 'ejecutar_todo_redes', lambda ctx: arrancadas.append('bajar'))
    monkeypatch.setattr(aplicacion, '_cert_de_sesion', lambda: {})
    for preparar in (_acceso_de_la_version_anterior,
                     lambda e: auto.guardar_config(e.base, dict(auto.CONFIG_POR_DEFECTO))):
        preparar(entorno)
        r = pantalla.post('/api/auto/subir')
        assert r.status_code == 400 and r.get_json()['falta_acceso'] is True
        assert pantalla.post('/api/auto/ejecutar').get_json()['exito'] is True
    assert arrancadas == ['bajar', 'bajar'] and GestorFalso.peticiones == []


@pytest.mark.parametrize('caso, motivo', [
    ('sin_configurar', 'Falta configurar el acceso a AiKit.'),
    ('sin_comprobar', 'El acceso a AiKit está guardado pero sin comprobar.'),
])
def test_sin_un_acceso_comprobado_se_baja_se_guarda_y_se_dice(entorno, caso, motivo):
    if caso == 'sin_configurar':
        auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))
    else:
        _acceso_de_la_version_anterior(entorno)
    r = entorno.pasada(DehuFalso(pendientes=[_item('P2', '2')], realizadas=[_item('R1', '2'), _item('R2', '1')]))
    assert r['bajados'] == 2 and r['sin_acceso'] is True and r['error_entrega'] == motivo
    assert entorno.por_subir() == ['R1.zip', 'R2.zip'] and r['quedan_por_subir'] == 2
    assert GestorFalso.peticiones == []                     # ni entrada, ni lista de pendientes, ni ZIP
    # La lista de pendientes queda escrita aquí, para el paquete a mano.
    with open(os.path.join(entorno.base, 'outputs', 'auto', 'pendientes_informadas.json'), encoding='utf-8') as f:
        assert [p['identificador'] for p in json.load(f)['pendientes']] == ['P2']
    assert r['mensaje'].startswith(f'2 bajados de DEHÚ. No se ha subido nada a AiKit: {motivo} Hay 2 ficheros guardados')
    assert '«Configurar acceso a AiKit»' in r['mensaje'] and '«Preparar un ZIP para enviarlo a mano»' in r['mensaje']
    estado = auto.estado(entorno.base)
    assert estado['acceso_listo'] is False and estado['acceso_falta'] == motivo and estado['quedan_por_subir'] == 2


def test_la_pasada_sin_pantalla_tambien_baja_sin_acceso_y_sale_con_error(entorno, monkeypatch):
    _acceso_de_la_version_anterior(entorno)
    dehu = DehuFalso(realizadas=[_item('R1', '2')])
    monkeypatch.setattr(DEHuClient, '_soap_call', lambda self, url, accion, xml: dehu(url, accion, xml))
    monkeypatch.setattr(auto, 'cert_context_guardado', lambda base: {})
    cliente = DEHuClient(DEHuConfig(pfx_base64=entorno.pfx_b64, key_password=PFX_PASSWORD,
                                    nif='B00000000', nombre='PRUEBA SL'))
    assert auto.main(['ejecutar'], entorno.base, lambda ctx, log: cliente) == 1
    assert entorno.por_subir() == ['R1.zip'] and GestorFalso.peticiones == []
    with open(os.path.join(entorno.base, 'outputs', 'auto', 'pasadas.log'), encoding='utf-8') as f:
        assert 'El acceso a AiKit está guardado pero sin comprobar.' in f.read()


def test_tras_un_rechazo_se_configura_y_se_sube_lo_guardado_sin_pedir_nada_a_dehu(entorno, monkeypatch):
    dehu = DehuFalso(pendientes=[_item('P1', '1')], realizadas=[_item('R1', '2')])
    GestorFalso.login_ok = False
    r = entorno.pasada(dehu)
    assert r['acceso_no_vale'] is True and entorno.por_subir() == ['P1.zip', 'R1.zip']
    estado = auto.estado(entorno.base)
    assert estado['acceso_no_vale'] is True and estado['acceso_listo'] is False

    GestorFalso.login_ok = True
    assert auto.comprobar_y_guardar_acceso(entorno.base, 'B00000000', GESTOR_PASSWORD)['exito'] is True
    assert auto.estado(entorno.base)['acceso_listo'] is True

    def dehu_prohibido(self, url, accion, xml):
        raise AssertionError(f'subir lo guardado no debe llamar a DEHÚ: {accion}')
    monkeypatch.setattr(DEHuClient, '_soap_call', dehu_prohibido)
    r = auto.subir_guardado(entorno.base, entorno.logs.append)
    assert r['entregados'] == 2 and r['quedan_por_subir'] == 0 and 'error_entrega' not in r
    assert sorted(GestorFalso.recibidos) == ['P1.zip', 'R1.zip'] and entorno.por_subir() == []
    assert GestorFalso.pendientes == []         # sin listado nuevo no se envía lista de pendientes
    assert auto.Registro(entorno.base).envio('R1')['estado'] == 'entregado'


def test_una_contrasena_guardada_que_no_se_puede_leer_pide_configurar(entorno, monkeypatch):
    def otra_cuenta(_valor):
        raise RuntimeError('CryptUnprotectData ha fallado')
    monkeypatch.setattr(auto, 'desproteger', otra_cuenta)
    r = entorno.pasada(DehuFalso(realizadas=[_item('R1', '2')]))
    assert r['acceso_no_vale'] is True and entorno.por_subir() == ['R1.zip']


def test_auto_configurar_comprueba_el_acceso_antes_de_guardarlo(entorno, monkeypatch, capsys):
    import getpass
    antes = _config_en_disco(entorno.base)
    monkeypatch.setattr('builtins.input', lambda _pregunta: '')
    monkeypatch.setattr(getpass, 'getpass', lambda _pregunta: GESTOR_PASSWORD * 2)
    assert auto.main(['configurar'], entorno.base, None) == 0
    assert '[ERROR] AiKit no reconoce' in capsys.readouterr().out
    assert json.loads(_config_en_disco(entorno.base))['gestor_password_dpapi'] == json.loads(antes)['gestor_password_dpapi']

    monkeypatch.setattr(getpass, 'getpass', lambda _pregunta: GESTOR_PASSWORD)
    assert auto.main(['configurar'], entorno.base, None) == 0
    assert '[OK] AiKit acepta' in capsys.readouterr().out
    assert auto.estado(entorno.base)['acceso_listo'] is True


# ── Subidas largas y lotes que no entran ──

NUEVE = [f'{letra}.zip' for letra in 'abcdefghi']


def test_la_sesion_se_renueva_entre_lotes(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    monkeypatch.setattr(auto, 'RENOVAR_SESION_SEGUNDOS', -1)     # toca renovar antes de cada lote
    entorno.guardados(NUEVE)
    r = entorno.subir()
    assert r['entregados'] == 9 and 'error_entrega' not in r and entorno.por_subir() == []
    assert GestorFalso.entradas == 4 and GestorFalso.subidas() == 3     # la entrada inicial y una por lote


def test_una_sesion_que_el_gestor_ha_cerrado_no_pierde_el_lote(entorno):
    entorno.guardados(NUEVE)
    GestorFalso.subidas_401 = 1
    r = entorno.subir()
    assert r['entregados'] == 9 and 'error_entrega' not in r and 'acceso_no_vale' not in r
    assert GestorFalso.entradas == 2 and sorted(GestorFalso.recibidos) == NUEVE


def test_un_lote_que_no_entra_no_retiene_a_los_demas(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    entorno.guardados(NUEVE)
    GestorFalso.no_admite = {'b.zip'}
    r = entorno.subir()
    # El lote a-b-c no entra; los otros dos sí; al final a y c entran solos.
    assert r['entregados'] == 8 and entorno.por_subir() == ['b.zip'] and r['quedan_por_subir'] == 1
    assert sorted(GestorFalso.recibidos) == [n for n in NUEVE if n != 'b.zip']
    assert r['sin_procesar'] == ['b.zip'] and 'no ha procesado 1 fichero' in r['error_entrega']
    assert GestorFalso.subidas() == 3 + 3
    enviado = open(os.path.join(entorno.base, 'outputs', 'auto', 'enviado.log'), encoding='utf-8').read()
    assert 'b.zip' not in enviado and enviado.count('entregado') == 8


def test_si_ningun_lote_entra_se_deja_de_subir(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 1)
    entorno.guardados(NUEVE)
    GestorFalso.no_admite = set(NUEVE)
    r = entorno.subir()
    assert r['entregados'] == 0 and entorno.por_subir() == NUEVE and GestorFalso.recibidos == {}
    assert GestorFalso.subidas() == auto.MAX_FALLOS_SEGUIDOS      # ni más lotes ni reintentos
    assert 'Se deja de subir' in r['error_entrega'] and 'No se ha perdido nada' in r['mensaje']


def test_un_corte_de_red_en_una_subida_se_reintenta_en_la_misma_pasada(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    entorno.guardados(NUEVE)
    GestorFalso.subidas_sin_red = 1
    GestorFalso.sondeos_sin_red = 2          # y un corte breve mientras se espera a otro lote
    r = entorno.subir()
    assert r['entregados'] == 9 and 'error_entrega' not in r and entorno.por_subir() == []


def test_un_lote_que_el_gestor_no_termina_no_se_le_da_dos_veces(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    monkeypatch.setattr(auto, 'MAX_ESPERA_LOTE', 0.3)
    entorno.guardados(NUEVE)
    GestorFalso.no_termina = {'a.zip'}
    r = entorno.subir()
    # a-b-c se queda para la próxima vez; los demás entran por una sesión nueva.
    assert r['entregados'] == 6 and entorno.por_subir() == ['a.zip', 'b.zip', 'c.zip']
    assert GestorFalso.subidas() == 3 and GestorFalso.entradas == 2


def test_la_pantalla_recibe_el_avance_y_un_final_en_palabras_llanas(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    entorno.guardados(NUEVE)
    avances = []
    r = entorno.subir(lambda texto, hechos, total: avances.append((texto, hechos, total)))
    assert [(h, t) for _x, h, t in avances] == [(0, 9), (3, 9), (6, 9), (9, 9)]
    assert avances[1][0].startswith('Subiendo a AiKit: 3 de 9 ficheros (faltan ')
    assert 'no se pierde nada' in avances[1][0]
    assert r['mensaje'] == '9 subidos a AiKit.'


def test_si_cambian_la_contrasena_a_mitad_se_dice_y_cuenta_lo_ya_subido(entorno, monkeypatch):
    monkeypatch.setattr(auto, 'TAMANO_LOTE', 3)
    monkeypatch.setattr(auto, 'RENOVAR_SESION_SEGUNDOS', -1)
    entorno.guardados(NUEVE)
    original = GestorFalso.get

    def progreso_y_cambio(self, url, **kw):
        respuesta = original(self, url, **kw)
        if url.endswith('/progreso') and len(GestorFalso.recibidos) == 3:
            GestorFalso.login_ok = False        # tras el primer lote, AiKit cambia la contraseña
        return respuesta
    monkeypatch.setattr(GestorFalso, 'get', progreso_y_cambio)
    r = entorno.subir()
    assert r['entregados'] == 3 and r['acceso_no_vale'] is True and len(entorno.por_subir()) == 6
    assert r['mensaje'].startswith('3 subidos a AiKit. 6 ficheros siguen guardados')


# ── Salida de emergencia: el paquete para enviar a mano ──

def _zips_de(carpeta):
    return {n: zipfile.ZipFile(os.path.join(carpeta, n)) for n in sorted(os.listdir(carpeta))}


def test_el_paquete_a_mano_lleva_todo_lo_que_espera_y_no_cambia_nada(entorno, monkeypatch):
    # Sin acceso configurado y con la subida rota: para eso está.
    GestorFalso.login_ok = False
    r = entorno.pasada(DehuFalso(pendientes=[_item('P2', '2')], realizadas=[_item(f'R{i}', '2') for i in range(1, 6)]))
    assert r['acceso_no_vale'] is True
    auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))
    antes = {n: open(os.path.join(entorno.base, 'outputs', 'auto', 'por_subir', n), 'rb').read()
             for n in entorno.por_subir()}
    assert len(antes) == 5
    auto.anotar_pasada(entorno.base, '[ERROR] Entrega: lo que la pantalla dijo en la pasada')
    peticiones, enviado = list(GestorFalso.peticiones), _envio_log(entorno)

    r = auto.preparar_envio_a_mano(entorno.base)
    assert r['ficheros'] == 5 and len(r['partes']) == 1 and os.path.dirname(r['carpeta']).endswith(auto.CARPETA_A_MANO)
    zips = _zips_de(r['carpeta'])
    assert sorted(zips) == sorted(r['partes'] + [r['diagnostico']])
    parte = zips[r['partes'][0]]
    assert sorted(parte.namelist()) == sorted(antes)                     # en la raíz, sin carpetas
    assert all(parte.read(n) == antes[n] for n in antes)                 # byte a byte
    diag = zips[r['diagnostico']]
    assert set(diag.namelist()) == {'CONTENIDO.txt', 'registro.json', 'pendientes_informadas.json', 'pasadas.log'}
    assert json.loads(diag.read('pendientes_informadas.json'))['pendientes'][0]['identificador'] == 'P2'
    contenido = diag.read('CONTENIDO.txt').decode('utf-8')
    assert all(n in contenido for n in antes) and auto.VERSION in contenido

    # Nada se ha dado por subido, nada se ha movido y no se ha llamado a nadie.
    assert entorno.por_subir() == sorted(antes) and GestorFalso.peticiones == peticiones
    assert _envio_log(entorno) == enviado
    reg = auto.Registro(entorno.base)
    assert all(reg.envio(f'R{i}')['estado'] == 'descargado' for i in range(1, 6))
    assert reg.datos['paquetes_a_mano'][-1]['ficheros'] == 5


def _envio_log(entorno):
    ruta = os.path.join(entorno.base, 'outputs', 'auto', 'enviado.log')
    return open(ruta, encoding='utf-8').read() if os.path.exists(ruta) else ''


def test_el_paquete_a_mano_se_parte_por_tamano(entorno, monkeypatch):
    entorno.guardados(NUEVE)
    tam = os.path.getsize(os.path.join(entorno.base, 'outputs', 'auto', 'por_subir', 'a.zip'))
    monkeypatch.setattr(auto, 'MAX_BYTES_PARTE_A_MANO', tam * 4)
    r = auto.preparar_envio_a_mano(entorno.base)
    assert len(r['partes']) == 3 and all(f'_parte_{n}_de_3.zip' in p for n, p in enumerate(r['partes'], 1))
    zips = _zips_de(r['carpeta'])
    assert [len(zips[p].namelist()) for p in r['partes']] == [4, 4, 1]
    assert sorted(n for p in r['partes'] for n in zips[p].namelist()) == NUEVE
    assert zips[r['diagnostico']].read('CONTENIDO.txt').decode('utf-8').count('.zip\t') == 9


def test_el_paquete_a_mano_no_lleva_la_configuracion_ni_secretos(entorno):
    entorno.guardados(NUEVE)
    auto.guardar_config(entorno.base, {**auto.leer_config(entorno.base), 'pfx_path': 'C:\\certificados\\qev.pfx',
                                       'pfx_password_dpapi': auto.proteger('clave-del-pfx-de-prueba')})
    cfg = auto.leer_config(entorno.base)
    r = auto.preparar_envio_a_mano(entorno.base)
    todo = b''
    for z in _zips_de(r['carpeta']).values():
        assert 'auto_config.json' not in z.namelist()
        todo += b''.join(z.read(n) for n in z.namelist())
    for secreto in (GESTOR_PASSWORD, 'clave-del-pfx-de-prueba', cfg['gestor_password_dpapi'],
                    cfg['pfx_password_dpapi'], 'qev.pfx'):
        assert secreto.encode() not in todo


def test_sin_nada_que_subir_el_paquete_es_solo_el_diagnostico(entorno, pantalla):
    r = pantalla.post('/api/auto/paquete').get_json()
    assert r['exito'] is True and r['ficheros'] == 0 and r['partes'] == []
    assert os.listdir(r['carpeta']) == [r['diagnostico']]


def test_la_pantalla_prepara_el_paquete_sin_acceso_y_pide_sesion(entorno, pantalla):
    import app as aplicacion
    assert aplicacion.app.test_client().post('/api/auto/paquete').status_code == 401
    auto.guardar_config(entorno.base, dict(auto.CONFIG_POR_DEFECTO))       # sin acceso a AiKit
    entorno.guardados(NUEVE)
    r = pantalla.post('/api/auto/paquete').get_json()
    assert r['exito'] is True and r['ficheros'] == 9 and GestorFalso.peticiones == []
    assert entorno.por_subir() == NUEVE
    with aplicacion.dehu_lock:
        aplicacion.dehu_estado['procesando'] = True
    try:
        assert pantalla.post('/api/auto/paquete').status_code == 400      # con una pasada en marcha, no
    finally:
        with aplicacion.dehu_lock:
            aplicacion.dehu_estado['procesando'] = False
