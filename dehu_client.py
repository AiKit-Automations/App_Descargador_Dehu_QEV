"""
Cliente Python para la API DEHu-LEMA (Dirección Electrónica Habilitada Única).

Servicios web SOAP para Gran Destinatario:
  - Localiza: Buscar notificaciones pendientes
  - PeticionAcceso: Acceder/aceptar una notificación pendiente (tiene validez jurídica)
  - LocalizaRealizadas: Buscar notificaciones ya gestionadas
  - ConsultaRealizadas: Consultar detalle de una notificación gestionada

Autenticación:
  - mTLS: Certificado electrónico presentado a nivel TLS
  - WS-Security: Firma XML del body SOAP con X.509
"""

from __future__ import annotations

import base64
import hashlib
import uuid
import datetime
import email
import re
import time
import logging
from pathlib import Path
from dataclasses import dataclass, field

import requests
from lxml import etree
from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    Encoding,
    pkcs12,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("dehu_client")


# ── Excepciones tipadas ────────────────────────────────────────────────────

class DEHuApiError(Exception):
    """Error genérico de la API DEHÚ."""
    def __init__(self, message, codigo=None, descripcion=None):
        self.codigo = codigo
        self.descripcion = descripcion
        super().__init__(message)

class DEHuAuthError(DEHuApiError):
    """Error de autenticación (certificado, mTLS, firma)."""
    pass

class DEHuServerError(DEHuApiError):
    """Error del servidor DEHÚ (5xx)."""
    pass

class DEHuRateLimitError(DEHuApiError):
    """Límite de consultas alcanzado (4225)."""
    pass

class DEHuConceptMismatchError(DEHuApiError):
    """Concepto no coincide con el registro (4204)."""
    pass

# ── Namespaces ──────────────────────────────────────────────────────────────

NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "ec": "http://www.w3.org/2001/10/xml-exc-c14n#",
    # Operaciones pendientes (v2)
    "loc": "http://administracion.gob.es/punto-unico-notificaciones/localiza",
    "pet": "http://administracion.gob.es/punto-unico-notificaciones/peticionAcceso",
    "anexos": "http://administracion.gob.es/punto-unico-notificaciones/consultaAnexos",
    "acuse": "http://administracion.gob.es/punto-unico-notificaciones/consultaAcusePdf",
    # Operaciones realizadas (v1)
    "locR": "http://administracion.gob.es/punto-unico-notificaciones/localizaRealizadas",
    "conR": "http://administracion.gob.es/punto-unico-notificaciones/consultaRealizadas",
}

# Constantes WS-Security
BST_ENCODING = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
BST_VALUE_TYPE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3"


# ── Configuración ──────────────────────────────────────────────────────────

@dataclass
class DEHuConfig:
    """Configuración del cliente DEHu."""

    # Certificado electrónico
    cert_pem: str = ""        # Ruta al certificado PEM
    key_pem: str = ""         # Ruta a la clave privada PEM
    pfx_path: str = ""        # Ruta al fichero PFX/P12 (alternativa a PEM)
    pfx_base64: str = ""      # PFX en base64 (para despliegues en la nube)
    key_password: str = None   # Contraseña de la clave privada (si tiene)

    # Identificación
    nif: str = ""            # NIF del titular/receptor
    nombre: str = ""         # Nombre del titular

    # Entorno
    env: str = "produccion"  # "produccion" o "testing"

    # Timeouts y reintentos
    timeout: int = 60        # Timeout de conexión en segundos
    max_retries: int = 3     # Reintentos para errores transitorios

    @property
    def base_url_lema(self) -> str:
        if self.env == "testing":
            return "https://se-dehuws.redsara.es/ws/v2/lema"
        return "https://gd-dehuws.redsara.es/ws/v2/lema"

    @property
    def base_url_realizadas(self) -> str:
        if self.env == "testing":
            return "https://se-dehuws.redsara.es/ws/v1/realizadas"
        return "https://gd-dehuws.redsara.es/ws/v1/realizadas"


# ── Cliente DEHu ───────────────────────────────────────────────────────────

class DEHuClient:
    """Cliente para los servicios web SOAP de DEHu-LEMA."""

    def __init__(self, config: DEHuConfig, log_callback=None):
        self.config = config
        self._cert_b64 = None
        self._private_key = None
        self._session = None
        self._log_callback = log_callback
        self._load_credentials()
        self._init_session()

    def _ui_log(self, msg):
        """Envía un mensaje al callback de UI si está configurado."""
        if self._log_callback:
            try:
                self._log_callback(msg)
            except Exception:
                pass

    # ── Carga de credenciales ──

    def _load_credentials(self):
        """Carga certificado y clave privada desde PEM, PFX fichero, o PFX base64."""
        password = self.config.key_password.encode() if self.config.key_password else None

        # Determinar origen del PFX
        pfx_data = None
        if self.config.pfx_base64:
            logger.info("Cargando credenciales desde PFX (base64 env var)")
            pfx_data = base64.b64decode(self.config.pfx_base64)
        elif self.config.pfx_path:
            logger.info("Cargando credenciales desde PFX: %s", self.config.pfx_path)
            pfx_data = Path(self.config.pfx_path).read_bytes()

        if pfx_data is not None:
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                pfx_data, password
            )
            self._private_key = private_key
            self._cert_b64 = base64.b64encode(
                certificate.public_bytes(Encoding.DER)
            ).decode()

            # Comprobar caducidad del certificado
            self._cert_expiry_warning(certificate)

            # Extraer PEM temporales para requests (mTLS)
            from cryptography.hazmat.primitives.serialization import (
                NoEncryption,
                PrivateFormat,
            )
            import tempfile

            self._temp_cert = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
            self._temp_key = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)

            self._temp_cert.write(certificate.public_bytes(Encoding.PEM))
            self._temp_cert.flush()

            self._temp_key.write(
                private_key.private_bytes(
                    Encoding.PEM,
                    PrivateFormat.TraditionalOpenSSL,
                    NoEncryption(),
                )
            )
            self._temp_key.flush()

            self.config.cert_pem = self._temp_cert.name
            self.config.key_pem = self._temp_key.name

        else:
            logger.info("Cargando credenciales desde PEM: %s", self.config.cert_pem)
            cert = load_pem_x509_certificate(Path(self.config.cert_pem).read_bytes())
            self._private_key = load_pem_private_key(
                Path(self.config.key_pem).read_bytes(), password=password
            )
            self._cert_b64 = base64.b64encode(
                cert.public_bytes(Encoding.DER)
            ).decode()
            # Comprobar caducidad del certificado
            self._cert_expiry_warning(cert)

    def _init_session(self):
        """Inicializa sesión requests con mTLS."""
        self._session = requests.Session()
        # Certificado cliente para mTLS
        self._session.cert = (self.config.cert_pem, self.config.key_pem)
        # Verificar certificado del servidor
        self._session.verify = True
        self._session.headers.update({
            "Content-Type": "text/xml; charset=utf-8",
            "User-Agent": "DEHu-Client-Python/1.0",
        })

    # ── Firma WS-Security ──

    def _build_signed_envelope(self, body_filler, op_name: str, op_ns: str, include_timestamp: bool = False) -> bytes:
        """Construye un sobre SOAP firmado con WS-Security."""
        # Crear estructura SOAP
        env = etree.Element(etree.QName(NS["soapenv"], "Envelope"), nsmap=NS)
        header = etree.SubElement(env, etree.QName(NS["soapenv"], "Header"))
        body = etree.SubElement(
            env,
            etree.QName(NS["soapenv"], "Body"),
            {etree.QName(NS["wsu"], "Id"): "Body"},
        )

        # Rellenar el body con la operación
        operation = etree.SubElement(body, etree.QName(op_ns, op_name))
        body_filler(operation)

        # WS-Security Header
        security = etree.SubElement(header, etree.QName(NS["wsse"], "Security"))

        # Timestamp (requerido por v1/realizadas, omitido en v2/lema)
        timestamp_el = None
        ts_id = None
        if include_timestamp:
            ts_id = f"TS-{uuid.uuid4().hex.upper()}"
            timestamp_el = etree.SubElement(
                security,
                etree.QName(NS["wsu"], "Timestamp"),
                {etree.QName(NS["wsu"], "Id"): ts_id},
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            etree.SubElement(
                timestamp_el, etree.QName(NS["wsu"], "Created")
            ).text = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            expires = now + datetime.timedelta(minutes=5)
            etree.SubElement(
                timestamp_el, etree.QName(NS["wsu"], "Expires")
            ).text = expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # BinarySecurityToken (certificado X.509)
        bst_id = f"X509-{uuid.uuid4().hex.upper()}"
        bst = etree.SubElement(
            security,
            etree.QName(NS["wsse"], "BinarySecurityToken"),
            {
                etree.QName(NS["wsu"], "Id"): bst_id,
                "EncodingType": BST_ENCODING,
                "ValueType": BST_VALUE_TYPE,
            },
        )
        bst.text = self._cert_b64

        # Signature
        signature = etree.SubElement(security, etree.QName(NS["ds"], "Signature"))

        # SignedInfo
        signed_info = etree.SubElement(signature, etree.QName(NS["ds"], "SignedInfo"))
        etree.SubElement(
            signed_info,
            etree.QName(NS["ds"], "CanonicalizationMethod"),
            Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
        )
        etree.SubElement(
            signed_info,
            etree.QName(NS["ds"], "SignatureMethod"),
            Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        )

        # Reference al Body
        ref = etree.SubElement(
            signed_info, etree.QName(NS["ds"], "Reference"), URI="#Body"
        )
        transforms = etree.SubElement(ref, etree.QName(NS["ds"], "Transforms"))
        etree.SubElement(
            transforms,
            etree.QName(NS["ds"], "Transform"),
            Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
        )
        etree.SubElement(
            ref,
            etree.QName(NS["ds"], "DigestMethod"),
            Algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
        )

        # Calcular digest del Body canonicalizado
        body_c14n = etree.tostring(body, method="c14n", exclusive=True)
        digest = base64.b64encode(hashlib.sha256(body_c14n).digest()).decode()
        etree.SubElement(ref, etree.QName(NS["ds"], "DigestValue")).text = digest

        # Reference al Timestamp (firmarlo también, solo si incluido)
        if include_timestamp and timestamp_el is not None:
            ref_ts = etree.SubElement(
                signed_info, etree.QName(NS["ds"], "Reference"), URI=f"#{ts_id}"
            )
            transforms_ts = etree.SubElement(ref_ts, etree.QName(NS["ds"], "Transforms"))
            etree.SubElement(
                transforms_ts,
                etree.QName(NS["ds"], "Transform"),
                Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
            )
            etree.SubElement(
                ref_ts,
                etree.QName(NS["ds"], "DigestMethod"),
                Algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
            )
            ts_c14n = etree.tostring(timestamp_el, method="c14n", exclusive=True)
            ts_digest = base64.b64encode(hashlib.sha256(ts_c14n).digest()).decode()
            etree.SubElement(ref_ts, etree.QName(NS["ds"], "DigestValue")).text = ts_digest

        # Firmar el SignedInfo
        signed_info_c14n = etree.tostring(signed_info, method="c14n", exclusive=True)
        signature_value = self._private_key.sign(
            signed_info_c14n, padding.PKCS1v15(), hashes.SHA256()
        )
        etree.SubElement(
            signature, etree.QName(NS["ds"], "SignatureValue")
        ).text = base64.b64encode(signature_value).decode()

        # KeyInfo -> SecurityTokenReference
        key_info = etree.SubElement(signature, etree.QName(NS["ds"], "KeyInfo"))
        str_ref = etree.SubElement(
            key_info, etree.QName(NS["wsse"], "SecurityTokenReference")
        )
        etree.SubElement(
            str_ref,
            etree.QName(NS["wsse"], "Reference"),
            URI=f"#{bst_id}",
            ValueType=BST_VALUE_TYPE,
        )

        return etree.tostring(env, xml_declaration=True, encoding="utf-8")

    # ── Monitoreo de certificado ──

    def _cert_expiry_warning(self, certificate) -> int:
        """Comprueba cuántos días de validez le quedan al certificado."""
        try:
            # not_valid_after_utc disponible en cryptography >= 42.x
            try:
                expiry = certificate.not_valid_after_utc
            except AttributeError:
                expiry = certificate.not_valid_after.replace(
                    tzinfo=datetime.timezone.utc
                )

            now = datetime.datetime.now(datetime.timezone.utc)
            days_remaining = (expiry - now).days
            self._cert_days_remaining = days_remaining
            self._cert_expiry_date = expiry.strftime("%Y-%m-%d")

            if days_remaining <= 0:
                logger.error(
                    "⛔ El certificado EXPIRÓ hace %d días (fecha: %s)",
                    abs(days_remaining), self._cert_expiry_date,
                )
            elif days_remaining <= 30:
                logger.warning(
                    "⚠️ El certificado expira en %d días (fecha: %s)",
                    days_remaining, self._cert_expiry_date,
                )
            else:
                logger.info(
                    "Certificado válido, expira en %d días (%s)",
                    days_remaining, self._cert_expiry_date,
                )
            return days_remaining
        except Exception as e:
            logger.warning("No se pudo verificar caducidad del certificado: %s", e)
            self._cert_days_remaining = None
            self._cert_expiry_date = None
            return -1

    def get_cert_info(self) -> dict:
        """Devuelve información del certificado para exposición en la UI."""
        return {
            "days_remaining": getattr(self, "_cert_days_remaining", None),
            "expiry_date": getattr(self, "_cert_expiry_date", None),
        }

    # ── Llamada SOAP ──

    def _soap_call(self, url: str, soap_action: str, xml_bytes: bytes) -> requests.Response:
        """Ejecuta una llamada SOAP con mTLS. Valida HTTP status."""
        headers = {"SOAPAction": f'"{ soap_action}"'}
        logger.debug("POST %s (SOAPAction: %s)", url, soap_action)

        # ── Log: REQUEST ──
        self._ui_log(f'[INFO] 🌐 SOAP Request → {soap_action}')
        self._ui_log(f'[INFO]    Endpoint: {url}')
        # Extraer info clave del XML (NIF, fechas, página)
        try:
            req_root = etree.fromstring(xml_bytes)
            nif_el = req_root.xpath(".//*[local-name()='nifTitular']")
            if nif_el:
                self._ui_log(f'[INFO]    NIF Titular: {nif_el[0].text}')
            fd_el = req_root.xpath(".//*[local-name()='fechaDesde']")
            fh_el = req_root.xpath(".//*[local-name()='fechaHasta']")
            if fd_el and fh_el:
                self._ui_log(f'[INFO]    Periodo: {fd_el[0].text} → {fh_el[0].text}')
            pag_el = req_root.xpath(".//*[local-name()='opcion' and @tipo='dehu.paginador.pagina']")
            if pag_el:
                self._ui_log(f'[INFO]    Página: {pag_el[0].text}')
            # Para PeticionAcceso
            id_el = req_root.xpath(".//*[local-name()='identificadorDestinatario']")
            if id_el:
                self._ui_log(f'[INFO]    Identificador: {id_el[0].text}')
            conc_el = req_root.xpath(".//*[local-name()='concepto']")
            if conc_el:
                self._ui_log(f'[INFO]    Concepto: {conc_el[0].text}')
        except Exception:
            pass

        import time as _time
        t0 = _time.time()
        response = self._session.post(
            url, data=xml_bytes, headers=headers, timeout=self.config.timeout
        )
        elapsed = _time.time() - t0

        logger.debug("HTTP %d - %d bytes", response.status_code, len(response.content))

        # ── Log: RESPONSE ──
        self._ui_log(f'[INFO] 📡 SOAP Response ← HTTP {response.status_code} ({elapsed:.1f}s, {len(response.content)} bytes)')

        # Parsear código/descripción de la respuesta SOAP
        try:
            r_code, r_desc = self._check_response_code(response.content)
            if r_code == '200':
                self._ui_log(f'[INFO]    ✅ Código: {r_code} — {r_desc}')
            else:
                self._ui_log(f'[WARNING]    ⚠️ Código: {r_code} — {r_desc}')
        except Exception:
            pass

        # Validar HTTP status code
        if response.status_code >= 500:
            self._ui_log(f'[ERROR] ❌ DEHÚ Error HTTP {response.status_code}: {response.text[:200]}')
            raise DEHuServerError(
                f"DEHÚ devolvió HTTP {response.status_code}",
                codigo=str(response.status_code),
                descripcion=response.text[:300],
            )
        if response.status_code == 403:
            self._ui_log('[ERROR] ❌ Certificado rechazado (HTTP 403) — ¿certificado válido? ¿acceso configurado?')
            raise DEHuAuthError(
                "Certificado rechazado por DEHÚ (HTTP 403)",
                codigo="403",
                descripcion="Comprueba que el certificado es válido y tiene acceso.",
            )
        if response.status_code != 200:
            self._ui_log(f'[WARNING] ⚠️ HTTP {response.status_code} — intentando parsear respuesta SOAP')
            logger.warning(
                "HTTP %d (no fatal, se intentará parsear respuesta SOAP)",
                response.status_code,
            )

        return response

    def _soap_call_with_retry(
        self, url: str, soap_action: str, xml_bytes: bytes
    ) -> requests.Response:
        """
        Ejecuta _soap_call con reintentos exponenciales para errores transitorios.
        Reintenta: Timeout, ConnectionError, DEHuServerError.
        NO reintenta: DEHuAuthError, DEHuRateLimitError, DEHuConceptMismatchError.
        """
        max_retries = self.config.max_retries
        for attempt in range(max_retries):
            try:
                return self._soap_call(url, soap_action, xml_bytes)
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    DEHuServerError) as e:
                if attempt == max_retries - 1:
                    self._ui_log(f'[ERROR] ❌ Todos los reintentos agotados ({max_retries}). Último error: {e}')
                    raise
                wait = 2 ** attempt  # 1s, 2s, 4s
                self._ui_log(f'[WARNING] ⚠️ Intento {attempt+1}/{max_retries} falló ({type(e).__name__}). Reintentando en {wait}s...')
                logger.warning(
                    "Intento %d/%d falló (%s: %s). Reintentando en %ds...",
                    attempt + 1, max_retries, type(e).__name__, e, wait,
                )
                time.sleep(wait)
        # Safety fallback
        return self._soap_call(url, soap_action, xml_bytes)


    # ── Utilidades de parseo ──

    @staticmethod
    def _xpath_text(element, query: str) -> str | None:
        nodes = element.xpath(query)
        if nodes and nodes[0].text:
            return nodes[0].text.strip()
        return None

    @staticmethod
    def _parse_items(xml_bytes: bytes) -> list[dict]:
        """Parsea los items de una respuesta Localiza/LocalizaRealizadas."""
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            logger.error("Error parseando XML: %s", e)
            return []

        items = []
        for it in root.xpath("//*[local-name()='item']"):
            rec = {}
            for ch in it.iterchildren():
                if not isinstance(ch.tag, str):
                    continue
                key = etree.QName(ch.tag).localname
                if len(ch):
                    # Tiene hijos: extraer sub-elementos
                    for sub in ch.iterchildren():
                        if isinstance(sub.tag, str) and sub.text and sub.text.strip():
                            rec[f"{key}_{etree.QName(sub.tag).localname}"] = sub.text.strip()
                    # También guardar el texto completo (mixed content: texto + hijos + tail)
                    # Esto cubre casos donde el elemento tiene texto directo además de hijos
                    full_text = ''.join(ch.itertext()).strip()
                    if full_text and key not in rec:
                        rec[key] = full_text
                else:
                    # Sin hijos: extraer texto completo (incluyendo tail de sub-nodos si los hay)
                    full_text = ''.join(ch.itertext()).strip()
                    if full_text:
                        rec[key] = full_text
            if rec:
                items.append(rec)
        return items

    @staticmethod
    def _check_response_code(xml_bytes: bytes) -> tuple[str, str]:
        """
        Extrae código y descripción de la respuesta SOAP.
        Busca tanto codigoRespuesta como SOAP Faults (faultstring).
        """
        try:
            root = etree.fromstring(xml_bytes)

            # Buscar SOAP Fault primero (la API devuelve algunos errores así)
            faultstring = DEHuClient._xpath_text(root, ".//*[local-name()='faultstring']")
            if faultstring:
                # Intentar extraer código numérico del faultstring (ej: "4225: max consultas")
                code_match = re.search(r'(\d{4})', faultstring)
                code = code_match.group(1) if code_match else "FAULT"
                return code, faultstring

            # Respuesta normal: codigoRespuesta + descripcionRespuesta
            code = DEHuClient._xpath_text(root, ".//*[local-name()='codigoRespuesta']")
            desc = DEHuClient._xpath_text(root, ".//*[local-name()='descripcionRespuesta']")
            return code or "?", desc or "?"
        except etree.XMLSyntaxError:
            return "XML_ERROR", "Respuesta no es XML válido"

    @staticmethod
    def _parse_multipart(response: requests.Response) -> tuple[bytes | None, bytes | None]:
        """Separa la parte XML y el fichero adjunto de una respuesta MIME multipart."""
        content_type = response.headers.get("Content-Type", "")
        if "multipart" not in content_type:
            return response.content, None

        mime_msg = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + response.content
        )
        msg = email.message_from_bytes(mime_msg)

        xml_part = None
        file_part = None
        for part in msg.get_payload():
            ct = part.get_content_type()
            if "xml" in ct or "xop+xml" in ct:
                xml_part = part.get_payload(decode=True)
            else:
                file_part = part.get_payload(decode=True)

        return xml_part, file_part

    # ── Operaciones de la API ──

    def localiza_pendientes(
        self,
        fecha_desde: str = None,
        fecha_hasta: str = None,
        pagina: int = 1,
        tipo_envio: str = None,
    ) -> dict:
        """
        Localiza notificaciones pendientes.

        Args:
            fecha_desde: Fecha inicio ISO (ej: "2024-01-01T00:00:00"). Default: 2020-01-01
            fecha_hasta: Fecha fin ISO. Default: ahora
            pagina: Número de página (paginación)
            tipo_envio: "1" = Comunicaciones, "2" = Notificaciones, None = ambas

        Returns:
            dict con claves: 'codigo', 'descripcion', 'items', 'total_paginas', 'xml_raw'
        """
        if not fecha_desde:
            fecha_desde = "2020-01-01T00:00:00"
        if not fecha_hasta:
            # DEHÚ rechaza fecha_hasta >= hoy, usar ayer
            ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
            fecha_hasta = ayer
        else:
            # Si fecha_hasta >= hoy, caparlo a ayer
            try:
                fh = datetime.datetime.fromisoformat(fecha_hasta.replace("Z", "+00:00"))
                hoy = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if fh.tzinfo:
                    hoy = hoy.replace(tzinfo=fh.tzinfo)
                if fh >= hoy:
                    ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
                    fecha_hasta = ayer
            except (ValueError, TypeError):
                pass

        # Si después del ajuste fecha_desde >= fecha_hasta (ej: "solo hoy"),
        # omitir fechas y traer todo
        use_dates = True
        try:
            fd = datetime.datetime.fromisoformat(fecha_desde.replace("Z", "+00:00"))
            fh2 = datetime.datetime.fromisoformat(fecha_hasta.replace("Z", "+00:00"))
            if fd.tzinfo and not fh2.tzinfo:
                fh2 = fh2.replace(tzinfo=fd.tzinfo)
            elif fh2.tzinfo and not fd.tzinfo:
                fd = fd.replace(tzinfo=fh2.tzinfo)
            if fd >= fh2:
                use_dates = False
        except (ValueError, TypeError):
            pass

        def filler(op):
            etree.SubElement(op, etree.QName(NS["loc"], "nifTitular")).text = self.config.nif
            etree.SubElement(op, etree.QName(NS["loc"], "nifDestinatario")).text = self.config.nif
            if use_dates:
                etree.SubElement(op, etree.QName(NS["loc"], "fechaDesde")).text = fecha_desde
                etree.SubElement(op, etree.QName(NS["loc"], "fechaHasta")).text = fecha_hasta
            if tipo_envio:
                etree.SubElement(op, etree.QName(NS["loc"], "tipoEnvio")).text = tipo_envio
            opts = etree.SubElement(op, etree.QName(NS["loc"], "opcionesLocaliza"))
            etree.SubElement(
                opts, etree.QName(NS["loc"], "opcion"), tipo="dehu.paginador.pagina"
            ).text = str(pagina)

        xml_bytes = self._build_signed_envelope(filler, "Localiza", NS["loc"])
        resp = self._soap_call_with_retry(self.config.base_url_lema, "Localiza", xml_bytes)

        code, desc = self._check_response_code(resp.content)
        items = self._parse_items(resp.content) if code == "200" else []

        total_paginas = 1
        if code == "200":
            try:
                root = etree.fromstring(resp.content)
                tp_nodes = root.xpath(
                    ".//*[local-name()='opcion' and @tipo='dehu.paginador.totalPag']"
                )
                if tp_nodes and tp_nodes[0].text:
                    total_paginas = int(tp_nodes[0].text)
            except Exception:
                pass

        return {
            "codigo": code,
            "descripcion": desc,
            "items": items,
            "total_paginas": total_paginas,
            "xml_raw": resp.content,
        }

    def localiza_todas_pendientes(self, **kwargs) -> dict:
        """
        Localiza TODAS las notificaciones pendientes (con paginación automática).

        Returns:
            dict con: 'items', 'errores_paginacion' (lista de errores por página)
        """
        all_items = []
        errores_paginacion = []
        page = 1

        self._ui_log('[INFO] ═══ DEHÚ API: Localiza Pendientes (paginación automática) ═══')

        while True:
            logger.info("Consultando pendientes - página %d...", page)
            self._ui_log(f'[INFO] 📄 Solicitando página {page}...')
            try:
                result = self.localiza_pendientes(pagina=page, **kwargs)
            except (DEHuServerError, requests.exceptions.RequestException) as e:
                msg = f"Error en página {page}: {e}"
                logger.warning(msg)
                self._ui_log(f'[ERROR] ❌ {msg}')
                errores_paginacion.append({"pagina": page, "error": str(e)})
                break

            if result["codigo"] != "200":
                msg = f"Código {result['codigo']} en página {page}: {result['descripcion']}"
                logger.warning(msg)
                self._ui_log(f'[ERROR] ❌ DEHÚ respondió: {msg}')
                errores_paginacion.append({
                    "pagina": page,
                    "codigo": result["codigo"],
                    "error": result["descripcion"],
                })
                break

            if not result["items"]:
                logger.info("Sin resultados en página %d. Fin.", page)
                self._ui_log(f'[INFO]    Página {page} vacía — fin de resultados')
                break

            all_items.extend(result["items"])
            total = result["total_paginas"]
            logger.info(
                "Página %d/%d - %d items (acumulado: %d)",
                page, total, len(result["items"]), len(all_items),
            )
            self._ui_log(f'[INFO]    Página {page}/{total}: +{len(result["items"])} items (acumulado: {len(all_items)})')

            if page >= total:
                break
            page += 1

        self._ui_log(f'[INFO] ═══ Total: {len(all_items)} notificaciones pendientes ═══')
        return {"items": all_items, "errores_paginacion": errores_paginacion}

    def peticion_acceso(
        self,
        identificador: str,
        codigo_origen: str,
        concepto: str,
        evento: str = "1",
    ) -> dict:
        """
        Accede/acepta una notificación pendiente.

        ⚠️  ADVERTENCIA: Esta operación tiene VALIDEZ JURÍDICA.
        El evento "1" equivale a aceptar la notificación (comparecencia electrónica).

        Args:
            identificador: ID de la notificación
            codigo_origen: Código de origen del envío
            concepto: Concepto/descripción
            evento: "1" = Aceptar (default)

        Returns:
            dict con: 'codigo', 'descripcion', 'csv_resguardo', 'documento_nombre',
                       'documento_bytes', 'xml_raw'
        """
        def filler(op):
            etree.SubElement(op, etree.QName(NS["pet"], "identificador")).text = identificador
            etree.SubElement(op, etree.QName(NS["pet"], "codigoOrigen")).text = codigo_origen
            etree.SubElement(op, etree.QName(NS["pet"], "concepto")).text = concepto
            etree.SubElement(op, etree.QName(NS["pet"], "nifReceptor")).text = self.config.nif
            etree.SubElement(op, etree.QName(NS["pet"], "nombreReceptor")).text = self.config.nombre
            etree.SubElement(op, etree.QName(NS["pet"], "evento")).text = evento

        xml_bytes = self._build_signed_envelope(filler, "PeticionAcceso", NS["pet"])
        resp = self._soap_call_with_retry(self.config.base_url_lema, "PeticionAcceso", xml_bytes)

        xml_part, file_part = self._parse_multipart(resp)
        if not xml_part:
            xml_part = resp.content

        code, desc = self._check_response_code(xml_part)

        doc_name = None
        csv_resguardo = None
        if code == "200":
            try:
                root = etree.fromstring(xml_part)
                doc_name = self._xpath_text(root, ".//*[local-name()='nombre']")
                csv_resguardo = self._xpath_text(root, ".//*[local-name()='csvResguardo']")
            except Exception:
                pass

        return {
            "codigo": code,
            "descripcion": desc,
            "csv_resguardo": csv_resguardo,
            "documento_nombre": doc_name,
            "documento_bytes": file_part,
            "xml_raw": xml_part,
        }

    def localiza_realizadas(
        self,
        pagina: int = 1,
        fecha_desde: str = None,
        fecha_hasta: str = None,
    ) -> dict:
        """
        Localiza notificaciones ya gestionadas/realizadas.

        Returns:
            dict con: 'codigo', 'descripcion', 'items', 'total_paginas', 'xml_raw'
        """
        # DEHÚ error 4224: localizaRealizadas NO permite fecha_desde > 30 días.
        # No capamos aquí — el frontend valida y muestra aviso al usuario.

        if not fecha_hasta:
            ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
            fecha_hasta = ayer
        else:
            # DEHÚ rechaza fecha_hasta >= hoy → caparlo a ayer
            try:
                fh = datetime.datetime.fromisoformat(fecha_hasta.replace("Z", "+00:00"))
                hoy = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if fh.tzinfo:
                    hoy = hoy.replace(tzinfo=fh.tzinfo)
                if fh >= hoy:
                    ayer = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT23:59:59")
                    fecha_hasta = ayer
            except (ValueError, TypeError):
                pass

        # Si fecha_desde >= fecha_hasta, omitir fechas
        use_dates = bool(fecha_desde)
        if use_dates:
            try:
                fd = datetime.datetime.fromisoformat(fecha_desde.replace("Z", "+00:00"))
                fh2 = datetime.datetime.fromisoformat(fecha_hasta.replace("Z", "+00:00"))
                if fd.tzinfo and not fh2.tzinfo:
                    fh2 = fh2.replace(tzinfo=fd.tzinfo)
                elif fh2.tzinfo and not fd.tzinfo:
                    fd = fd.replace(tzinfo=fh2.tzinfo)
                if fd >= fh2:
                    use_dates = False
            except (ValueError, TypeError):
                pass

        def filler(op):
            etree.SubElement(op, etree.QName(NS["locR"], "nifTitular")).text = self.config.nif
            etree.SubElement(op, etree.QName(NS["locR"], "nifDestinatario")).text = self.config.nif
            if use_dates:
                etree.SubElement(op, etree.QName(NS["locR"], "fechaDesde")).text = fecha_desde
                etree.SubElement(op, etree.QName(NS["locR"], "fechaHasta")).text = fecha_hasta
            etree.SubElement(op, etree.QName(NS["locR"], "pagina")).text = str(pagina)

        xml_bytes = self._build_signed_envelope(filler, "LocalizaRealizadas", NS["locR"])
        resp = self._soap_call_with_retry(
            self.config.base_url_realizadas, "LocalizaRealizadas", xml_bytes
        )

        code, desc = self._check_response_code(resp.content)
        logger.debug("LocalizaRealizadas response: code=%s desc=%s fecha_desde=%s fecha_hasta=%s pagina=%d", code, desc, fecha_desde, fecha_hasta, pagina)
        items = self._parse_items(resp.content) if code == "200" else []
        logger.debug("LocalizaRealizadas parsed %d items", len(items))

        total_paginas = 1
        if code == "200":
            try:
                root = etree.fromstring(resp.content)
                tp_nodes = root.xpath(".//*[local-name()='totalPaginas']")
                if tp_nodes and tp_nodes[0].text:
                    total_paginas = int(tp_nodes[0].text)
            except Exception:
                pass

        return {
            "codigo": code,
            "descripcion": desc,
            "items": items,
            "total_paginas": total_paginas,
            "xml_raw": resp.content,
        }

    def localiza_todas_realizadas(self, **kwargs) -> dict:
        """
        Localiza TODAS las notificaciones realizadas (paginación automática).

        Returns:
            dict con: 'items', 'errores_paginacion'
        """
        logger.info("localiza_todas_realizadas called with kwargs: %s", kwargs)
        all_items = []
        errores_paginacion = []
        page = 1

        self._ui_log('[INFO] ═══ DEHÚ API: Localiza Realizadas (paginación automática) ═══')

        while True:
            logger.info("Consultando realizadas - página %d...", page)
            self._ui_log(f'[INFO] 📄 Solicitando página {page}...')
            try:
                result = self.localiza_realizadas(pagina=page, **kwargs)
            except (DEHuServerError, requests.exceptions.RequestException) as e:
                msg = f"Error en página {page}: {e}"
                logger.warning(msg)
                self._ui_log(f'[ERROR] ❌ {msg}')
                errores_paginacion.append({"pagina": page, "error": str(e)})
                break

            if result["codigo"] != "200":
                msg = f"Código {result['codigo']} en página {page}: {result['descripcion']}"
                logger.warning(msg)
                self._ui_log(f'[ERROR] ❌ DEHÚ respondió: {msg}')
                errores_paginacion.append({
                    "pagina": page,
                    "codigo": result["codigo"],
                    "error": result["descripcion"],
                })
                break

            if not result["items"]:
                self._ui_log(f'[INFO]    Página {page} vacía — fin de resultados')
                break

            all_items.extend(result["items"])
            total = result["total_paginas"]
            logger.info(
                "Página %d/%d - %d items (acumulado: %d)",
                page, total, len(result["items"]), len(all_items),
            )
            self._ui_log(f'[INFO]    Página {page}/{total}: +{len(result["items"])} items (acumulado: {len(all_items)})')

            if page >= total:
                break
            page += 1

        self._ui_log(f'[INFO] ═══ Total: {len(all_items)} notificaciones realizadas ═══')
        return {"items": all_items, "errores_paginacion": errores_paginacion}

    def consulta_realizadas(
        self,
        identificador: str,
        codigo_origen: str,
        concepto: str,
    ) -> dict:
        """
        Consulta el detalle de una notificación ya gestionada.

        Returns:
            dict con: 'codigo', 'descripcion', 'fecha_ultimo_estado',
                       'documento_nombre', 'documento_bytes', 'xml_raw'
        """
        def filler(op):
            etree.SubElement(op, etree.QName(NS["conR"], "identificador")).text = identificador
            etree.SubElement(op, etree.QName(NS["conR"], "codigoOrigen")).text = codigo_origen
            etree.SubElement(op, etree.QName(NS["conR"], "concepto")).text = concepto
            etree.SubElement(op, etree.QName(NS["conR"], "nifPeticion")).text = self.config.nif
            etree.SubElement(op, etree.QName(NS["conR"], "nombrePeticion")).text = self.config.nombre

        xml_bytes = self._build_signed_envelope(filler, "ConsultaRealizadas", NS["conR"])
        resp = self._soap_call_with_retry(
            self.config.base_url_realizadas, "ConsultaRealizadas", xml_bytes
        )

        xml_part, file_part = self._parse_multipart(resp)
        if not xml_part:
            xml_part = resp.content

        code, desc = self._check_response_code(xml_part)

        fecha_ultimo_estado = None
        doc_name = None
        if code == "200":
            try:
                root = etree.fromstring(xml_part)
                fecha_ultimo_estado = self._xpath_text(
                    root, ".//*[local-name()='fechaUltimoEstado']"
                )
                doc_name = self._xpath_text(root, ".//*[local-name()='nombre']")
            except Exception:
                pass

        return {
            "codigo": code,
            "descripcion": desc,
            "fecha_ultimo_estado": fecha_ultimo_estado,
            "documento_nombre": doc_name,
            "documento_bytes": file_part,
            "xml_raw": xml_part,
        }

    def test_conexion(self) -> bool:
        """
        Test rápido de conectividad a Red SARA y al endpoint DEHu.
        Intenta una petición Localiza con rango de fechas vacío para verificar
        que la autenticación funciona.
        """
        logger.info("Probando conexión a %s ...", self.config.base_url_lema)
        try:
            result = self.localiza_pendientes(
                fecha_desde="2025-01-01T00:00:00",
                fecha_hasta="2025-01-01T00:00:01",
            )
            if result["codigo"] == "200":
                logger.info("Conexión OK. Respuesta: %s", result["descripcion"])
                return True
            else:
                logger.warning(
                    "Conexión establecida pero respuesta inesperada: %s - %s",
                    result["codigo"],
                    result["descripcion"],
                )
                # Código != 200 pero la conexión funciona
                return True
        except requests.exceptions.SSLError as e:
            logger.error("Error SSL/mTLS: %s", e)
            logger.error(
                "Verifica que el certificado es válido y que tienes acceso a Red SARA."
            )
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error("Error de conexión: %s", e)
            logger.error(
                "No se puede conectar a Red SARA. "
                "Verifica que tienes acceso a la red (VPN, conexión directa, etc.)."
            )
            return False
        except Exception as e:
            logger.error("Error inesperado: %s", e)
            return False
