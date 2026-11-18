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
- **Todo lo ya realizado** de Red.es en los últimos 29 días (DEHÚ rechaza una fecha de más de 30 días): se vuelve a bajar con `ConsultaRealizadas`, que no tiene efecto legal.
- **Notificaciones pendientes** de Red.es: **no se aceptan** salvo que lo activéis vosotros (siguiente apartado). De las que quedan pendientes, AiKit recibe solo la lista (apartado «La lista de notificaciones pendientes»).

La primera pasada abre todas las comunicaciones de Red.es que estén pendientes, también las antiguas (hasta 998 por pasada). Abrir una comunicación no tiene efecto legal, pero deja de figurar como pendiente en DEHÚ.

## Aceptar notificaciones pendientes: apagado por defecto

Aceptar una notificación pendiente es comparecer: tiene efecto legal y abre su plazo. La pasada solo lo hace si en `auto_config.json` figura `"aceptar_notificaciones_pendientes": true`, y eso solo se escribe ejecutando `auto configurar` y tecleando la palabra `ACEPTO`. Con cualquier otro valor está apagado (`auto.py`, `leer_config` y `bajar_todo`).

Con el permiso apagado, una notificación pendiente entra en AiKit cuando alguien la acepta (en la pantalla manual o en la web de DEHÚ): a partir de entonces aparece como realizada y la pasada la recoge.

## La lista de notificaciones pendientes

Una notificación que nadie abre en 10 días naturales cuenta como notificada y su plazo corre igual. Para que AiKit pueda avisar a tiempo, cada pasada le envía la lista de las notificaciones de Red.es que deja pendientes (todas, si el permiso de aceptar está apagado; con el permiso activo, solo las que DEHÚ no haya dejado aceptar).

- **No se abre ni se acepta nada para hacer la lista.** Sale del listado de pendientes de DEHÚ (`Localiza`), la misma llamada que hace la pantalla al listar. No hay `PeticionAcceso` ni descarga del documento (`auto.py`, `bajar_todo` e `informar_pendientes`).
- **Qué lleva cada notificación**, tal como lo devuelve ese listado: identificador, código de origen, concepto (ahí va el número de acuerdo), fecha de puesta a disposición, tipo de envío, organismo emisor (código y nombre), NIF y nombre del titular y vínculo. Nada más: ni el documento ni su contenido.
- **Además**: la hora de la pasada y si el listado estaba completo.
- **Solo Red.es.** Lo pendiente de otros organismos no entra en la lista.
- **Dónde se ve lo enviado**: `outputs/auto/pendientes_informadas.json` guarda la última lista tal como salió, y `outputs/auto/enviado.log` tiene una línea por cada envío con su sha256.
- Se envía a `POST /api/dehu-pendientes` del gestor, en la misma sesión que los ZIP. Si el gestor no la admite o falla, se anota y la pasada sigue: los ZIP se entregan igual.

## Qué sale del ordenador y a dónde

Solo hay dos destinos:

| Destino | Qué se envía | Dónde está en el código |
|---|---|---|
| DEHÚ (`gd-dehuws.redsara.es`) | Las mismas llamadas que hace hoy la pantalla manual, firmadas con el certificado | `dehu_client.py` (sin cambios) |
| Gestor de AiKit: `https://clasificador-dehu-v2-production.up.railway.app` | El usuario y la contraseña del gestor (los que ya usáis en esa web); los ZIP de Red.es tal como los devuelve DEHÚ, con un `meta.json` dentro (identificador, concepto, fecha de puesta a disposición, tipo y emisor); y la lista de notificaciones de Red.es pendientes (apartado anterior) | `auto.py`, `entregar_todo` e `informar_pendientes` |

- La dirección del gestor es una constante del código (`GESTOR_URL`). No se puede cambiar desde la configuración, y las peticiones no siguen redirecciones.
- Es la misma web y son los mismos tres pasos que hacéis hoy a mano: entrar, subir los ZIP y pulsar procesar. Lo único nuevo que sale es la lista de pendientes.
- Una comunicación que DEHÚ entrega como PDF suelto se sube dentro de un ZIP (`<nombre>.pdf.zip`) con su `meta.json`, porque el gestor solo admite ZIP.
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
| Última lista de pendientes enviada | `outputs/auto/pendientes_informadas.json` | Texto, tal como salió |
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

La tarea se llama `DescargadorDehuQEV-TodoRedes` y se ejecuta con la cuenta que la crea, solo con la sesión de esa cuenta iniciada (modo «Interactive only» del Programador de tareas): con el ordenador encendido pero sin sesión, no arranca. Si un día no se ejecuta, la siguiente pasada recoge lo que faltó, siempre que no pasen más de 29 días. La orden termina con código 1 si algo falla (un listado incompleto, un envío que no baja, la entrega), y el Programador de tareas lo muestra como último resultado.

## Cómo se apaga

- **La pasada diaria**: `python app.py auto desprogramar` (o borrar la tarea en el Programador de tareas).
- **Aceptar notificaciones**: `python app.py auto configurar` y responder `NO`, o poner `false` en `auto_config.json`.
- **Todo lo nuevo**: borrar `auto_config.json`. Sin él no hay contraseñas guardadas, la pasada baja pero no sube nada y la tarea programada no puede arrancar. La pantalla manual sigue funcionando.

## Comportamiento ante fallos

- Si la subida falla, lo bajado se queda en `por_subir` y se reintenta en la siguiente pasada, sin volver a pedirlo a DEHÚ.
- Un envío ya bajado o ya subido no se vuelve a pedir ni a subir.
- Si DEHÚ contesta tres veces que no tiene el documento de un envío, se deja de pedir. Un corte de red no cuenta.
- La primera pasada sube también lo realizado en los últimos 29 días, aunque ya lo hubierais subido a mano: el gestor sustituye el registro de ese envío, no lo duplica.
- Si DEHÚ rechaza un listado, la pasada lo dice como error y termina con código 1; no se da por buena una pasada que no ha visto todo.
- Como mucho 998 documentos por pasada, el mismo tope que la pantalla manual; el resto entra en la siguiente.

## Pruebas

`python -m pytest tests -q` (24 pruebas, sin red: DEHÚ y el gestor se sustituyen por respuestas de mentira y el certificado es uno autofirmado creado en la prueba). Cubren: solo Red.es; notificaciones pendientes sin aceptar por defecto; el permiso solo con `true`; la lista de pendientes (qué lleva, que no abre nada, lista vacía, gestor que no la admite); el rango de fechas que DEHÚ admite; un listado rechazado termina en error; no repetir; reintento de la subida; registro de lo enviado; que el certificado y su contraseña no salen; y que la pantalla manual sigue funcionando.

## Probado con el DEHÚ real (18-11-2026)

En un PC con Windows 11 y Python 3.11, con un certificado de AiKit (no el vuestro), en solo lectura: `PeticionAcceso` bloqueada y la entrega apuntando a un gestor de mentira en el propio PC.

- Listados reales: 27 pendientes (23 comunicaciones de Red.es, ninguna notificación de Red.es) y 74 realizadas en 29 días (67 de Red.es). Lo de otros organismos no se tocó.
- Bajada real con `ConsultaRealizadas` de una muestra de 5 envíos de Red.es (3 comunicaciones y 2 notificaciones ya aceptadas): los 5 bajan, llevan su `meta.json` y llegan al gestor de mentira por los tres pasos. Los 3 PDF de comunicación son idénticos (sha256) a los que AiKit ya tenía de esos envíos.
- Una segunda pasada no vuelve a pedir nada a DEHÚ.
- La lista de pendientes llegó vacía, como correspondía.
- Windows: las 24 pruebas pasan; DPAPI cifra y descifra, también entre la sesión que configura y la tarea programada, y la contraseña no aparece en `auto_config.json`; `auto configurar` pregunta y guarda; `auto programar` crea la tarea, la tarea ejecuta una pasada completa (resultado 0) y `auto desprogramar` la borra.
- El `.exe` se genera con la orden de abajo y hace una pasada real.

Esa prueba encontró y corrigió dos fallos: el listado de realizadas pedía 30 días y DEHÚ lo rechazaba (error 4224), y una pasada con el listado rechazado terminaba sin error.

## Lo que no está probado

- **Aceptar notificaciones pendientes** (`PeticionAcceso` de una notificación) y **abrir comunicaciones pendientes**: no se han ejecutado contra el DEHÚ real, a propósito.
- **El gestor real**: la entrega solo se ha hecho contra un gestor de mentira. En particular, no se ha visto cómo clasifica el gestor real un `<nombre>.pdf.zip`.
- **La lista de pendientes con datos reales**: el buzón de la prueba no tenía notificaciones de Red.es pendientes. La ruta `POST /api/dehu-pendientes` del gestor está preparada aparte y aún no desplegada; hasta entonces el gestor contesta 404 y la pasada lo anota y sigue.
- **El botón de la pantalla** en Windows (la pasada se ha probado por la línea de órdenes y la tarea programada).
- Vuestro certificado y vuestro ordenador.
- El `.exe` de `dist/` es el de la versión anterior: no incluye nada de esto. Para generarlo con este código: `pyinstaller --onefile --console --add-data "templates;templates" --add-data "static;static" --name DescargadorDehuQEV app.py` (con consola, porque `auto configurar` pregunta por teclado). Con esa orden se generó el de la prueba.
