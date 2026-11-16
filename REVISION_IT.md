# Descargador DEHÚ: pasada «todo lo de Red.es». Documento para revisión de IT

Rama `auto/todo-redes-a-diario`. Este documento explica qué cambia respecto a la versión que ya tenéis, qué sale del ordenador, qué se guarda en él y cómo se apaga. Todo lo que se afirma aquí se puede comprobar en el código; se indica el fichero en cada punto.

## Qué cambia

1. **Un botón nuevo en la pantalla**: «Bajar todo lo de Red.es y subirlo a AiKit». Baja de DEHÚ todo lo que Red.es ha enviado al titular del certificado y lo sube al gestor de AiKit, sin marcar nada a mano y sin el paso de subir el ZIP a la web.
2. **Una pasada diaria opcional, sin nadie delante** (tarea programada de Windows). No se instala sola: hay que crearla expresamente.
3. **La pantalla manual sigue igual**: listar, marcar, descargar y bajar el ZIP funcionan como antes.
4. **La pantalla ya solo se abre en este ordenador** (`127.0.0.1`). Antes escuchaba en toda la red local y con el modo de depuración de Flask activo (`app.py`, última línea).

Ficheros: `auto.py` (nuevo, la pasada), `descarga.py` (nuevo: código que ya existía en `app.py`, movido sin cambiar lo que hace para que lo usen la pantalla y la pasada), `app.py` (dos rutas nuevas y el arranque), `templates/index.html` (el botón), `tests/` (pruebas). `dehu_client.py` y `auth.py` no cambian. No hay dependencias nuevas (`requirements.txt` no cambia).

## Qué baja la pasada

- **Solo Red.es**: organismo emisor `EA0044367` o nombre que contiene «red.es» (`descarga.py`, `es_red_es`). Lo de cualquier otro organismo (AEAT, Seguridad Social, etc.) ni se acepta, ni se consulta, ni se sube, con cualquier configuración. No hay opción para cambiarlo.
- **Comunicaciones pendientes** de Red.es: se abren y se bajan.
- **Todo lo ya realizado** de Red.es en los últimos 30 días (DEHÚ no devuelve más atrás): se vuelve a bajar con `ConsultaRealizadas`, que no tiene efecto legal.
- **Notificaciones pendientes** de Red.es: **no se aceptan** salvo que lo activéis vosotros (siguiente apartado).

## Aceptar notificaciones pendientes: apagado por defecto

Aceptar una notificación pendiente es comparecer: tiene efecto legal y abre su plazo. La pasada solo lo hace si en `auto_config.json` figura `"aceptar_notificaciones_pendientes": true`, y eso solo se escribe ejecutando `auto configurar` y tecleando la palabra `ACEPTO`. Con cualquier otro valor está apagado (`auto.py`, `leer_config` y `bajar_todo`).

Con el permiso apagado, una notificación pendiente entra en AiKit cuando alguien la acepta (en la pantalla manual o en la web de DEHÚ): a partir de entonces aparece como realizada y la pasada la recoge.

## Qué sale del ordenador y a dónde

Solo hay dos destinos:

| Destino | Qué se envía | Dónde está en el código |
|---|---|---|
| DEHÚ (`gd-dehuws.redsara.es`) | Las mismas llamadas que hace hoy la pantalla manual, firmadas con el certificado | `dehu_client.py` (sin cambios) |
| Gestor de AiKit: `https://clasificador-dehu-v2-production.up.railway.app` | El usuario y la contraseña del gestor (los que ya usáis en esa web) y los ZIP de Red.es tal como los devuelve DEHÚ, con un `meta.json` dentro (identificador, concepto, fecha de puesta a disposición, tipo y emisor) | `auto.py`, `entregar_todo` |

- La dirección del gestor es una constante del código (`GESTOR_URL`). No se puede cambiar desde la configuración, y las peticiones no siguen redirecciones.
- Es la misma web y son los mismos tres pasos que hacéis hoy a mano: entrar, subir los ZIP y pulsar procesar.
- **El certificado y su contraseña no se envían a ningún sitio.** Solo se usan en este ordenador para la conexión con DEHÚ y para firmar las peticiones. Hay una prueba que lo comprueba (`tests/test_auto.py`, `test_el_certificado_y_su_clave_no_salen`).
- La pantalla (el navegador, no el programa) carga el logotipo de AiKit desde `cdn.prod.website-files.com`. Ya era así.

## Qué se guarda en el ordenador

Todo queda en la carpeta del programa:

| Qué | Dónde | Cómo |
|---|---|---|
| ZIP bajados pendientes de subir | `outputs/auto/por_subir/` | Tal cual |
| Copia de los ZIP ya subidos | `outputs/auto/entregado/` | Tal cual. No se borra sola |
| Estado de cada envío | `outputs/auto/registro.json` | Texto. Sirve para no repetir y para reintentar |
| Registro de lo enviado | `outputs/auto/enviado.log` | Una línea por fichero: fecha, destino, nombre, tamaño y sha256 |
| Mensajes de cada pasada programada | `outputs/auto/pasadas.log` | Texto |
| Configuración | `auto_config.json` | Texto. Las contraseñas van cifradas (abajo) |

### El punto sensible: la pasada sin nadie delante

Con el **botón**, el certificado se carga en la pantalla como hasta ahora: vive en memoria mientras el programa está abierto y no se guarda en disco. Lo único que se guarda es la contraseña del gestor de AiKit.

La **tarea programada** necesita el certificado sin que nadie lo cargue. Para eso `auto_config.json` guarda:

- la **ruta** del fichero `.pfx` (el fichero se queda donde lo tengáis; no se copia);
- la **contraseña del certificado, cifrada con DPAPI de Windows** (`CryptProtectData`; `auto.py`, `proteger`).

DPAPI cifra con una clave ligada a la cuenta de Windows: el valor guardado solo se descifra con esa misma cuenta en ese mismo ordenador. Copiar `auto_config.json` a otro equipo o leerlo con otra cuenta no da la contraseña. No protege frente a un programa que se ejecute con esa misma cuenta de Windows: ese programa podría descifrarla igual que este. Si ese riesgo no os resulta aceptable, usad solo el botón y no configuréis el certificado.

La contraseña del gestor de AiKit se guarda del mismo modo.

## Cómo se usa

```
iniciar.bat                          abre la pantalla (como siempre); ahí está el botón
python app.py auto configurar        usuario y contraseña del gestor; opcional: certificado y permiso de aceptar
python app.py auto ejecutar          hace una pasada ahora, sin pantalla
python app.py auto programar 08:00   crea la tarea diaria de Windows (schtasks) a esa hora
python app.py auto desprogramar      borra la tarea
python app.py auto estado            enseña la configuración y la última pasada
```

La tarea se llama `DescargadorDehuQEV-TodoRedes` y se ejecuta con la cuenta que la crea, con la sesión iniciada. Si un día el ordenador está apagado, la siguiente pasada recoge lo que faltó, siempre que no pasen más de 30 días.

## Cómo se apaga

- **La pasada diaria**: `python app.py auto desprogramar` (o borrar la tarea en el Programador de tareas).
- **Aceptar notificaciones**: `python app.py auto configurar` y responder `NO`, o poner `false` en `auto_config.json`.
- **Todo lo nuevo**: borrar `auto_config.json`. Sin él no hay contraseñas guardadas, la pasada baja pero no sube nada y la tarea programada no puede arrancar. La pantalla manual sigue funcionando.

## Comportamiento ante fallos

- Si la subida falla, lo bajado se queda en `por_subir` y se reintenta en la siguiente pasada, sin volver a pedirlo a DEHÚ.
- Un envío ya bajado o ya subido no se vuelve a pedir ni a subir.
- Si DEHÚ contesta tres veces que no tiene el documento de un envío, se deja de pedir. Un corte de red no cuenta.
- La primera pasada sube también lo realizado en los últimos 30 días, aunque ya lo hubierais subido a mano: el gestor sustituye el registro de ese envío, no lo duplica.
- Como mucho 998 documentos por pasada, el mismo tope que la pantalla manual; el resto entra en la siguiente.

## Pruebas

`python -m pytest tests -q` (18 pruebas, sin red: DEHÚ y el gestor se sustituyen por respuestas de mentira y el certificado es uno autofirmado creado en la prueba). Cubren: solo Red.es; notificaciones pendientes sin aceptar por defecto; el permiso solo con `true`; no repetir; reintento de la subida; registro de lo enviado; que el certificado y su contraseña no salen; y que la pantalla manual sigue funcionando.

## Lo que no está probado

- No se ha ejecutado contra el DEHÚ real ni contra el gestor real.
- El cifrado DPAPI y la creación de la tarea (`schtasks`) no se han ejecutado en Windows.
- El `.exe` de `dist/` es el de la versión anterior: no incluye nada de esto. Para generarlo con este código: `pyinstaller --onefile --console --add-data "templates;templates" --name DescargadorDehuQEV app.py` (con consola, porque `auto configurar` pregunta por teclado). Esta orden no se ha ejecutado.
