"""
Piezas comunes de descarga: las usan la pantalla manual (app.py) y la pasada
«todo lo de Red.es» (auto.py). No hacen ninguna llamada de red por sí mismas:
reciben el cliente DEHÚ ya creado.
"""

import json
import os
import re
import zipfile

# Organismo emisor Red.es en DEHÚ. Es la misma regla que marcaba las filas
# «Red.es» en la pantalla (templates/index.html, isRedEs).
CODIGO_RED_ES = 'EA0044367'

_NO_VALIDOS_EN_NOMBRE = '/\\:*?"<>|'


def es_red_es(item: dict) -> bool:
    """True si el envío lo emite Red.es."""
    codigo = item.get('organismoEmisor_codigoOrganismo') or ''
    nombre = (item.get('organismoEmisor_nombreOrganismo') or '').lower()
    return codigo == CODIGO_RED_ES or 'red.es' in nombre


def construir_meta(notif: dict) -> dict:
    """Metadata DEHÚ del envío, tal como viene en el listado."""
    meta = {
        'identificador': notif.get('identificador', ''),
        'codigoOrigen': notif.get('codigoOrigen', ''),
        'concepto': notif.get('concepto', ''),
        'fechaPuestaDisposicion': notif.get('fechaPuestaDisposicion', ''),
    }
    for opt_key in ('tipoEnvio', 'organismoEmisor_codigoOrganismo', 'organismoEmisor_nombreOrganismo'):
        val = notif.get(opt_key)
        if val:
            meta[opt_key] = val
    return meta


def anadir_meta_a_zip(zip_path: str, meta: dict, log) -> None:
    """Añade meta.json a la raíz de un ZIP existente sin recomprimir el resto.

    Idempotente: si el ZIP ya trae meta.json no se duplica. Si no se puede
    añadir (ZIP corrupto, permisos, disco) se avisa y se sigue: el documento ya
    está escrito y meta.json es una mejora, no un requisito.
    """
    if not os.path.isfile(zip_path):
        return
    try:
        with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as zf:
            if 'meta.json' in zf.namelist():
                return
            zf.writestr('meta.json', json.dumps(meta, ensure_ascii=False, indent=2).encode('utf-8'))
        log(f'[OK] meta.json añadido a {os.path.basename(zip_path)}')
    except Exception as e:
        log(f'[WARN] No se pudo añadir meta a {os.path.basename(zip_path)}: {e}')


def aceptar_con_reintento(client, notif: dict, log):
    """PeticionAcceso de un envío pendiente. Devuelve (resultado, concepto usado).

    Si DEHÚ responde 4204 (el concepto no coincide), vuelve a listar y prueba
    una vez con el concepto tal como viene en el XML.
    """
    ident = notif.get('identificador', '')
    origen = notif.get('codigoOrigen', '')
    concepto = notif.get('concepto', '')

    result = client.peticion_acceso(identificador=ident, codigo_origen=origen, concepto=concepto)

    xml_raw = result.get('xml_raw', b'')
    fault_text = xml_raw.decode('utf-8', errors='ignore') if isinstance(xml_raw, bytes) else str(xml_raw)
    if '4204' not in fault_text:
        return result, concepto

    log('[WARN] Error 4204 (concepto no coincide). Reintentando con concepto fresco...')
    try:
        raw_xml = client.localiza_pendientes().get('xml_raw', b'')
        raw_xml_str = raw_xml.decode('utf-8', errors='ignore') if isinstance(raw_xml, bytes) else str(raw_xml)
        item_match = re.search(
            rf'<[^>]*item[^>]*>.*?{re.escape(ident)}.*?</[^>]*item[^>]*>', raw_xml_str, re.DOTALL)
        if item_match:
            concepto_match = re.search(
                r'<[^>]*concepto[^>]*>(.*?)</[^>]*concepto[^>]*>', item_match.group(0), re.DOTALL)
            if concepto_match and concepto_match.group(1) != concepto:
                log('[INFO] Probando concepto del XML raw...')
                result = client.peticion_acceso(
                    identificador=ident, codigo_origen=origen, concepto=concepto_match.group(1))
                if result.get('codigo') == '200':
                    log('[OK] Retry exitoso con concepto fresco')
                    concepto = concepto_match.group(1)
    except Exception as e_retry:
        log(f'[WARN] Error en retry 4204: {e_retry}')
    return result, concepto


def guardar_documento(result: dict, notif: dict, concepto: str, destino: str, log) -> list:
    """Escribe en `destino` el documento que devolvió DEHÚ, con su meta.json.

    Si el documento es un ZIP contenedor de otros ZIP, se extraen y cada uno
    lleva su meta.json. Devuelve las rutas de los ficheros que quedan en disco.
    """
    ident = notif.get('identificador', '')
    doc_name = result.get('documento_nombre') or f'{ident}_documento.zip'
    for c in _NO_VALIDOS_EN_NOMBRE:
        doc_name = doc_name.replace(c, '_')
    doc_path = os.path.join(destino, doc_name)
    with open(doc_path, 'wb') as f:
        f.write(result['documento_bytes'])
    log(f'[OK] [{ident}] Descargado: {doc_name} ({len(result["documento_bytes"]):,} bytes)')

    meta = construir_meta(notif)
    meta['concepto'] = concepto

    if not doc_name.endswith('.zip'):
        return [doc_path]
    try:
        with zipfile.ZipFile(doc_path, 'r') as zf:
            inner_files = zf.namelist()
            if any(inner_f.endswith('.zip') for inner_f in inner_files):
                zf.extractall(destino)
            else:
                inner_files = None
    except zipfile.BadZipFile:
        log(f'[WARN] {doc_name} no es un ZIP válido, se mantiene como está')
        return [doc_path]

    if inner_files is None:
        anadir_meta_a_zip(doc_path, meta, log)
        return [doc_path]

    os.remove(doc_path)
    log(f'[INFO] Extraídos {len(inner_files)} ficheros del ZIP contenedor')
    rutas = []
    for inner_f in inner_files:
        ruta = os.path.join(destino, inner_f)
        if inner_f.endswith('.zip'):
            anadir_meta_a_zip(ruta, meta, log)
        if os.path.isfile(ruta):
            rutas.append(ruta)
    return rutas
