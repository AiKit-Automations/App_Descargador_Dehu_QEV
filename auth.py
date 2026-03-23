"""
auth.py — Módulo de autenticación simplificado.

Extrae NIF, nombre y CIF empresa de un certificado PFX/P12.
"""

import base64
import datetime

from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
from cryptography.x509.oid import NameOID


def extraer_datos_certificado(pfx_data: bytes, password: str) -> dict:
    """
    Abre un PFX/P12 y extrae NIF, nombre, CIF empresa y fecha de caducidad.

    Args:
        pfx_data: contenido binario del archivo PFX
        password: contraseña del certificado

    Returns:
        dict con claves: nif, nombre, cif_empresa, pfx_b64, expiry_date, days_remaining

    Raises:
        ValueError: si la contraseña es incorrecta o el PFX es inválido
    """
    pwd_bytes = password.encode() if password else None

    try:
        private_key, certificate, _ = load_key_and_certificates(pfx_data, pwd_bytes)
    except Exception:
        raise ValueError('Contraseña incorrecta o certificado inválido')

    if certificate is None:
        raise ValueError('El archivo no contiene un certificado válido')

    subject = certificate.subject

    # ── Extraer NIF ──
    nif = ''
    try:
        serial_attrs = subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
        if serial_attrs:
            serial_val = serial_attrs[0].value
            if 'IDCES-' in serial_val.upper():
                nif = serial_val.upper().split('IDCES-')[-1].strip()
            elif 'IDCES' in serial_val.upper():
                nif = serial_val.upper().replace('IDCES', '').strip()
            else:
                nif = serial_val.strip()
    except Exception:
        pass

    # ── Extraer Nombre ──
    nombre = ''
    try:
        cn_attrs = subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn_attrs:
            nombre = cn_attrs[0].value.strip()
        else:
            gn = subject.get_attributes_for_oid(NameOID.GIVEN_NAME)
            sn = subject.get_attributes_for_oid(NameOID.SURNAME)
            parts = []
            if gn:
                parts.append(gn[0].value)
            if sn:
                parts.append(sn[0].value)
            nombre = ' '.join(parts).strip()
    except Exception:
        pass

    # ── Extraer CIF empresa (certificados de representante) ──
    import re
    cif_empresa = ''
    try:
        if nombre:
            match = re.search(r'\(R:\s*([A-Z0-9]+)\)', nombre)
            if match:
                cif_empresa = match.group(1).strip().upper()
        if not cif_empresa:
            from cryptography.x509 import ObjectIdentifier
            ORG_ID_OID = ObjectIdentifier("2.5.4.97")
            org_id_attrs = subject.get_attributes_for_oid(ORG_ID_OID)
            if org_id_attrs:
                val = org_id_attrs[0].value.upper()
                if 'VATES-' in val:
                    cif_empresa = val.split('VATES-')[-1].strip()
    except Exception:
        pass

    # ── Caducidad ──
    try:
        try:
            expiry = certificate.not_valid_after_utc
        except AttributeError:
            expiry = certificate.not_valid_after.replace(
                tzinfo=datetime.timezone.utc
            )
        now = datetime.datetime.now(datetime.timezone.utc)
        days_remaining = (expiry - now).days
        expiry_date = expiry.strftime("%Y-%m-%d")
    except Exception:
        days_remaining = None
        expiry_date = None

    return {
        'nif': nif.upper(),
        'nombre': nombre,
        'cif_empresa': cif_empresa,
        'pfx_b64': base64.b64encode(pfx_data).decode(),
        'expiry_date': expiry_date,
        'days_remaining': days_remaining,
    }
