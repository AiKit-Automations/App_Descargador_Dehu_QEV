# Descargador DEHÚ: pasada «todo lo de Red.es». Documento para revisión de IT

Rama `auto/acceso-aikit-en-pantalla` (sobre `auto/todo-redes-a-diario`). Este documento explica qué cambia respecto a la versión que ya tenéis, qué sale del ordenador, qué se guarda en él y cómo se apaga. Todo lo que se afirma aquí se puede comprobar en el código; se indica el fichero en cada punto.

## Qué cambia

1. **Un botón nuevo en la pantalla**: «Bajar todo lo de Red.es y subirlo a AiKit». Baja de DEHÚ todo lo que Red.es ha enviado al titular del certificado y lo sube al gestor de AiKit, sin marcar nada a mano y sin el paso de subir el ZIP a la web.
2. **Una pasada diaria opcional, sin nadie delante** (tarea programada de Windows). No se instala sola: hay que crearla expresamente.
3. **El acceso a AiKit se configura en la pantalla**: el botón «Configurar acceso a AiKit» pide el usuario y la contraseña de la web de AiKit, los comprueba entrando y saliendo de esa web, y solo los guarda si AiKit los acepta. No hace falta ninguna ventana de órdenes (apartado «El acceso a AiKit»).
4. **Un botón para subir lo ya bajado**: «Subir ahora los N ficheros guardados» entrega lo que haya quedado sin subir, sin pedir nada a DEHÚ.
5. **Bajar no depende del acceso a AiKit**: sin un acceso comprobado, el botón avisa y deja elegir entre configurarlo o bajar igualmente; lo bajado se guarda y la pantalla dice que no se ha subido y por qué (apartado «El acceso a AiKit»).
6. **Una subida larga aguanta**: la sesión con AiKit se renueva sola, un lote que AiKit no procesa no retiene a los demás, y la pantalla dice cuántos ficheros van y cuánto falta (apartado «La subida»).
7. **Una salida de emergencia**: «Preparar un ZIP para enviarlo a mano» copia a un ZIP lo que espera subida, para hacerlo llegar a AiKit por otro camino si la subida no funciona (apartado «Enviar a mano»).
8. **La pantalla manual sigue igual**: listar, marcar, descargar y bajar el ZIP funcionan como antes.
9. **La pantalla ya solo se abre en este ordenador** (`127.0.0.1`). Antes escuchaba en toda la red local y con el modo de depuración de Flask activo (`app.py`, última línea).

Ficheros: `auto.py` (nuevo, la pasada y el acceso a AiKit), `descarga.py` (nuevo: código que ya existía en `app.py`, movido sin cambiar lo que hace para que lo usen la pantalla y la pasada), `app.py` (cinco rutas nuevas, `/api/auto/…`, y el arranque), `templates/index.html` (los botones y el cuadro del acceso), `tests/` (pruebas). `dehu_client.py` y `auth.py` no cambian. No hay dependencias nuevas (`requirements.txt` no cambia).

## Qué baja la pasada

- **Solo Red.es**: organismo emisor `EA0044367` o nombre que contiene «red.es» (`descarga.py`, `es_red_es`). Lo de cualquier otro organismo (AEAT, Seguridad Social, etc.) ni se acepta, ni se consulta, ni se sube, con cualquier configuración. No hay opción para cambiarlo.
- **Comunicaciones pendientes** de Red.es: se abren y se bajan.
- **Todo lo ya realizado** de Red.es en los últimos 29 días (DEHÚ rechaza una fecha de más de 30 días): se vuelve a bajar con `ConsultaRealizadas`, que no tiene efecto legal.
- **Notificaciones pendientes** de Red.es: **no se aceptan** salvo que lo activéis vosotros (siguiente apartado). De las que quedan pendientes, AiKit recibe solo la lista (apartado «La lista de notificaciones pendientes»).

La primera pasada abre todas las comunicaciones de Red.es que estén pendientes, también las antiguas (hasta 998 por pasada). Abrir una comunicación no tiene efecto legal, pero deja de figurar como pendiente en DEHÚ.

## Aceptar notificaciones pendientes: apagado por defecto

Aceptar una notificación pendiente es comparecer: tiene efecto legal y abre su plazo. La pasada solo lo hace si en `auto_config.json` figura `"aceptar_notificaciones_pendientes": true`, y eso solo se escribe ejecutando `auto configurar` y tecleando la palabra `ACEPTO`. Con cualquier otro valor está apagado (`auto.py`, `leer_config` y `bajar_todo`).

Con el permiso apagado, una notificación pendiente entra en AiKit cuando alguien la acepta (en la pantalla manual o en la web de DEHÚ): a partir de entonces aparece como realizada y la pasada la recoge.

## El acceso a AiKit

Para subir, el programa entra en la web de AiKit con el mismo usuario y la misma contraseña que usáis en ella. Se configuran en la pantalla, con «Configurar acceso a AiKit» (`templates/index.html`; `app.py`, `auto_acceso`; `auto.py`, `comprobar_y_guardar_acceso`):

- **Se comprueban antes de guardarlos.** El programa entra en la web de AiKit con lo escrito (`POST /login-password`) y sale (`GET /logout`). Si AiKit los acepta, los guarda; si no, dice por qué (usuario o contraseña que AiKit no reconoce, o no se ha podido llegar a AiKit) y **no guarda nada**: lo que hubiera guardado antes queda como estaba.
- **Lo que se escribe se limpia poco y a la vista**: se quitan los espacios y saltos de línea de los extremos (restos de copiar y pegar) y se rechaza, sin llamar a AiKit, un usuario o una contraseña con caracteres que no se ven (un salto de línea en medio, el carácter que deja Ctrl+V en algunas ventanas).
- **La contraseña no se devuelve a la pantalla, no se escribe en ningún registro y no aparece en los mensajes**: solo queda cifrada en `auto_config.json` (apartado «Qué se guarda en el ordenador»). Hay una prueba que lo comprueba (`tests/test_auto.py`, `test_la_contrasena_del_gestor_no_se_devuelve_ni_se_anota`).
- **Bajar no depende del acceso.** Sin un acceso comprobado (no hay ninguno, AiKit rechazó el guardado, o es el que dejó la versión anterior, sin comprobar), el botón lo dice en una frase y deja elegir: configurarlo ahora o bajar de todas formas. Si se baja, los ficheros se guardan en `outputs/auto/por_subir`, no se intenta subir nada ni se envía la lista de pendientes, y el final de la pasada dice cuántos ficheros esperan y las dos salidas: configurar el acceso y «Subir ahora», o el ZIP para enviar a mano. La línea bajo los botones enseña en rojo el estado del acceso mientras falte (`auto.py`, `_estado_acceso` y `_entregar_y_cerrar`; `app.py`, `_arrancar_auto`). La pasada sin pantalla (`auto ejecutar`) sigue la misma regla y termina con código 1.
- **Si AiKit deja de aceptar lo guardado** (un 401 al entregar) o la contraseña guardada no se puede leer (otra cuenta de Windows, otro ordenador), la pantalla lo dice y ofrece el cuadro. Lo bajado sigue en `outputs/auto/por_subir`. Hasta que se configure de nuevo no se vuelve a intentar entrar con lo rechazado.
- **«Subir ahora los N ficheros guardados»** entrega lo que hay en `por_subir` por los mismos tres pasos de siempre. No llama a DEHÚ y no envía lista de pendientes (`auto.py`, `subir_guardado`). Este sí pide un acceso comprobado.

## La subida

Los ZIP se suben en lotes de 20 por los tres pasos de la web: subir, procesar y esperar a que termine (`auto.py`, `entregar_todo` y `_entregar_lote`). Con el gestor de AiKit, un lote de 20 tarda unos 2 minutos y medio: unos 12 minutos por cada 100 ficheros, unas 3 horas para 1.400. Antes de empezar, la pantalla dice lo que tardará la subida de los ficheros que hay guardados (`SEGUNDOS_POR_FICHERO` en `auto.py`); durante la subida, el «faltan…» sale del ritmo que lleva de verdad.

- **Entregado es lo que AiKit confirma.** Un lote se da por entregado solo si el gestor responde que ha pasado por todos sus ficheros. Entonces sus ZIP pasan de `por_subir` a `entregado` y se anotan en `enviado.log`. Si no lo confirma (por ejemplo, porque el gestor se ha reiniciado a mitad), el lote se queda en `por_subir`.
- **Un lote que no entra no retiene a los demás.** La subida sigue con los siguientes y, al final, reintenta ese lote fichero a fichero: así un fichero que el gestor no admite no retiene a los otros 19. Los que tampoco entran solos se quedan en `por_subir`, la pantalla dice cuántos son y sus nombres quedan en `outputs/auto/pasadas.log`.
- **Si algo general falla, se deja de subir.** Tres intentos seguidos sin entrar (con pausas de 30 y 120 segundos entre ellos), o el aviso del gestor de que no puede procesar, paran la subida. Lo que falta sigue en `por_subir` y «Subir ahora» continúa.
- **La sesión se renueva sola.** El gestor cierra cada sesión a las 12 horas de entrar. El programa entra de nuevo entre lotes a las 6 horas, y también si el gestor rechaza una subida por sesión caducada; en ese caso repite esa subida.
- **Un lote que el gestor no termina en 2 horas** se deja para la próxima vez y no se le vuelve a dar en esa pasada.
- **Cerrar el programa a mitad no pierde nada**: lo que no estaba confirmado sigue en `por_subir`. El gestor admite recibir dos veces el mismo envío (último punto de «Comportamiento ante fallos»).
- **Mientras dura una pasada, Windows no suspende el ordenador por inactividad** (`SetThreadExecutionState`; `auto.py`, `_sin_suspension`). No cambia ninguna configuración: al terminar la pasada, o al cerrar el programa, queda como estaba. No impide apagar ni suspender a mano.
- **La pantalla dice por dónde va**: cuántos ficheros de cuántos, cuánto falta y que se puede cerrar el programa. Si se cierra la pestaña del navegador con la pasada en marcha, al volver a abrirla se sigue viendo.

## Enviar a mano

«Preparar un ZIP para enviarlo a mano» es una salida de emergencia para cuando la subida no funciona y hace falta que los ficheros lleguen a AiKit (`auto.py`, `preparar_envio_a_mano`; `app.py`, `auto_paquete`).

- **No sale nada del ordenador.** No llama a DEHÚ ni al gestor, y funciona sin acceso configurado. Solo escribe ficheros en `para_enviar_a_aikit/<fecha y hora>/`, dentro de la carpeta del programa, y abre esa carpeta en el Explorador. Enviarlos es cosa vuestra.
- **Qué prepara**: uno o varios ZIP (`QEV_para_AiKit_…`, de 100 MB como mucho cada uno) con copias de todos los ZIP de `por_subir`, tal cual, y un `QEV_diagnostico_….zip` con `CONTENIDO.txt` (la lista de ficheros con su tamaño y sha256, y la versión del programa) y, si existen, `registro.json`, `enviado.log`, `pasadas.log` y `pendientes_informadas.json`.
- **Qué no lleva nunca**: `auto_config.json` (el usuario de AiKit, las contraseñas cifradas y la ruta del certificado), ni el certificado. Hay una prueba que lo comprueba (`tests/test_auto.py`, `test_el_paquete_a_mano_no_lleva_la_configuracion_ni_secretos`).
- **No cambia nada de lo guardado.** Los ficheros siguen en `por_subir` sin marcar como subidos, y la siguiente subida los envía igual. Queda anotado en `registro.json` y en `pasadas.log` cuándo se preparó cada paquete.

## La lista de notificaciones pendientes

Una notificación que nadie abre en 10 días naturales cuenta como notificada y su plazo corre igual. Para que AiKit pueda avisar a tiempo, cada pasada le envía la lista de las notificaciones de Red.es que deja pendientes (todas, si el permiso de aceptar está apagado; con el permiso activo, solo las que DEHÚ no haya dejado aceptar).

- **No se abre ni se acepta nada para hacer la lista.** Sale del listado de pendientes de DEHÚ (`Localiza`), la misma llamada que hace la pantalla al listar. No hay `PeticionAcceso` ni descarga del documento (`auto.py`, `bajar_todo` e `informar_pendientes`).
- **Qué lleva cada notificación**, tal como lo devuelve ese listado: identificador, código de origen, concepto (ahí va el número de acuerdo), fecha de puesta a disposición, tipo de envío, organismo emisor (código y nombre), NIF y nombre del titular y vínculo. Nada más: ni el documento ni su contenido.
- **Además**: la hora de la pasada y si el listado estaba completo.
- **Solo Red.es.** Lo pendiente de otros organismos no entra en la lista.
- **Dónde se ve lo enviado**: `outputs/auto/pendientes_informadas.json` guarda la última lista tal como salió, y `outputs/auto/enviado.log` tiene una línea por cada envío con su sha256.
- Se envía a `POST /api/dehu-pendientes` del gestor, en la misma sesión que los ZIP. Si el gestor no la admite o falla, se anota y la pasada sigue: los ZIP se entregan igual.
- Sin un acceso a AiKit comprobado no se envía: queda escrita en `pendientes_informadas.json` y viaja en el ZIP de diagnóstico si se prepara un envío a mano.

## Qué sale del ordenador y a dónde

Solo hay dos destinos:

| Destino | Qué se envía | Dónde está en el código |
|---|---|---|
| DEHÚ (`gd-dehuws.redsara.es`) | Las mismas llamadas que hace hoy la pantalla manual, firmadas con el certificado | `dehu_client.py` (sin cambios) |
| Gestor de AiKit: `https://clasificador-dehu-v2-production.up.railway.app` | El usuario y la contraseña del gestor (los que ya usáis en esa web), al entregar y al comprobarlos en «Configurar acceso a AiKit»; los ZIP de Red.es tal como los devuelve DEHÚ, con un `meta.json` dentro (identificador, concepto, fecha de puesta a disposición, tipo y emisor); y la lista de notificaciones de Red.es pendientes (apartado anterior) | `auto.py`, `_entrar`, `entregar_todo` e `informar_pendientes` |

- La dirección del gestor es una constante del código (`GESTOR_URL`). No se puede cambiar desde la configuración, y las peticiones no siguen redirecciones.
- Es la misma web y son los mismos tres pasos que hacéis hoy a mano: entrar, subir los ZIP y pulsar procesar. Lo único nuevo que sale es la lista de pendientes.
- «Preparar un ZIP para enviarlo a mano» no envía nada: escribe ficheros en la carpeta del programa (apartado «Enviar a mano»).
- El programa no admite dos copias abiertas a la vez: la segunda abre la pantalla de la primera y termina (`app.py`, arranque).
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
| Mensajes de cada pasada, con el botón o programada | `outputs/auto/pasadas.log` | Texto. Nunca lleva contraseñas |
| Paquetes preparados para enviar a mano | `para_enviar_a_aikit/<fecha y hora>/` | Copias en ZIP. No se borran solas |
| Configuración | `auto_config.json` | Texto: el usuario de AiKit, cuándo se comprobó por última vez y las contraseñas, cifradas (abajo) |

### El punto sensible: la pasada sin nadie delante

Con el **botón**, el certificado se carga en la pantalla como hasta ahora: vive en memoria mientras el programa está abierto y no se guarda en disco. Lo único que se guarda es el acceso a AiKit: el usuario y, cifrada, la contraseña.

La **tarea programada** necesita el certificado sin que nadie lo cargue. Para eso `auto_config.json` guarda:

- la **ruta** del fichero `.pfx` (el fichero se queda donde lo tengáis; no se copia);
- la **contraseña del certificado, cifrada con DPAPI de Windows** (`CryptProtectData`; `auto.py`, `proteger`).

DPAPI cifra con una clave ligada a la cuenta de Windows: el valor guardado solo se descifra con esa misma cuenta en ese mismo ordenador. Copiar `auto_config.json` a otro equipo o leerlo con otra cuenta no da la contraseña. No protege frente a un programa que se ejecute con esa misma cuenta de Windows: ese programa podría descifrarla igual que este. Si ese riesgo no os resulta aceptable, usad solo el botón y no configuréis el certificado.

La contraseña del gestor de AiKit se guarda del mismo modo.

## Cómo se usa

```
iniciar.bat                          abre la pantalla (como siempre); ahí están los botones
python app.py auto configurar        certificado y permiso de aceptar; también el acceso a AiKit, comprobado igual que en la pantalla
python app.py auto ejecutar          hace una pasada ahora, sin pantalla
python app.py auto programar 08:00   crea la tarea diaria de Windows (schtasks) a esa hora
python app.py auto desprogramar      borra la tarea
python app.py auto estado            enseña la configuración y la última pasada
```

Para el uso con el botón no hace falta ninguna de estas órdenes: el acceso a AiKit se configura en la pantalla. Las órdenes `auto …` son para la pasada diaria sin nadie delante y para el permiso de aceptar.

La tarea se llama `DescargadorDehuQEV-TodoRedes` y se ejecuta con la cuenta que la crea, solo con la sesión de esa cuenta iniciada (modo «Interactive only» del Programador de tareas): con el ordenador encendido pero sin sesión, no arranca. Si un día no se ejecuta, la siguiente pasada recoge lo que faltó, siempre que no pasen más de 29 días. La orden termina con código 1 si algo falla (un listado incompleto, un envío que no baja, la entrega), y el Programador de tareas lo muestra como último resultado.

## Cómo se apaga

- **La pasada diaria**: `python app.py auto desprogramar` (o borrar la tarea en el Programador de tareas).
- **Aceptar notificaciones**: `python app.py auto configurar` y responder `NO`, o poner `false` en `auto_config.json`.
- **Todo lo nuevo**: borrar `auto_config.json`. Sin él no hay contraseñas guardadas, el botón baja y guarda pero no sube nada (avisa de que falta el acceso a AiKit) y la tarea programada no puede arrancar. La pantalla manual sigue funcionando.

## Cambiar de versión

`outputs/auto/` y `auto_config.json` están en la carpeta del programa, junto al `.exe`. Al sustituir el `.exe` por uno nuevo **en la misma carpeta** se conserva todo: lo bajado sin subir, el registro de lo ya entregado y la configuración. Un `auto_config.json` de la versión anterior se lee igual; como su acceso a AiKit no está comprobado, la pantalla lo dice en rojo y no se sube nada hasta pasar por «Configurar acceso a AiKit»; lo que se guarde ahí sustituye a lo anterior. En una carpeta distinta, el programa empieza de cero y lo bajado sin subir se queda en la carpeta antigua.

## Comportamiento ante fallos

- Si la subida falla, lo bajado se queda en `por_subir` y se reintenta en la siguiente pasada, o con «Subir ahora los N ficheros guardados», sin volver a pedirlo a DEHÚ. El final de la pasada dice cuántos ficheros han subido, cuántos siguen guardados y por qué; nunca «completado» si ha quedado algo sin subir (apartado «La subida»).
- Si falla porque AiKit no acepta el usuario o la contraseña guardados, la pantalla lo dice con esas palabras y abre el cuadro del acceso.
- Un envío ya bajado o ya subido no se vuelve a pedir ni a subir.
- Si DEHÚ contesta tres veces que no tiene el documento de un envío, se deja de pedir. Un corte de red no cuenta.
- DEHÚ admite un número limitado de consultas por envío (error 4225). Ese envío cuenta un «no» y la pasada sigue con los demás; con 5 seguidos la pasada se para hasta el día siguiente. La orden termina con código 1 para que se vea.
- DEHÚ limita las aperturas de pendientes por certificado y día (error 4305). Al llegar al tope no se abre nada más ese día, no cuenta como «no» de ningún envío, y la pasada sigue con lo ya realizado.
- La primera pasada sube también lo realizado en los últimos 29 días, aunque ya lo hubierais subido a mano. El gestor admite recibir dos veces el mismo envío: en la base de datos de AiKit queda una sola vez (se guarda por el identificador de DEHÚ) y en el histórico del gestor queda una fila más. Lo mismo vale para un lote que se repite tras un corte y para lo enviado a mano que después sube solo.
- Si DEHÚ rechaza un listado, la pasada lo dice como error y termina con código 1; no se da por buena una pasada que no ha visto todo.
- Si no se puede llegar a DEHÚ (sin conexión, el certificado no entra), la pasada lo dice, no envía lista de pendientes y sube igualmente lo que ya estaba bajado.
- Como mucho 998 documentos por pasada, el mismo tope que la pantalla manual; el resto entra en la siguiente.

## Pruebas

`python -m pytest tests -q` (54 pruebas, sin red: DEHÚ y el gestor se sustituyen por respuestas de mentira y el certificado es uno autofirmado creado en la prueba). Cubren: solo Red.es; notificaciones pendientes sin aceptar por defecto; el permiso solo con `true`; la lista de pendientes (qué lleva, que no abre nada, lista vacía, gestor que no la admite); el rango de fechas que DEHÚ admite; un listado rechazado termina en error; los topes de DEHÚ (4225 por envío y 4305 por día); no repetir; registro de lo enviado; que el certificado y su contraseña no salen; el acceso a AiKit (se guarda solo si AiKit lo acepta, lo anterior queda intacto si no, un carácter que no se ve se rechaza sin llamar, la contraseña no se devuelve ni se anota); bajar sin acceso (se baja, se guarda, no se llama al gestor, se dice por qué, también sin pantalla) y subir solo con acceso comprobado; la subida (la sesión se renueva, una sesión cerrada por el gestor no pierde el lote, un lote que no entra no retiene a los demás, si ninguno entra se deja de subir, un corte de red se reintenta, un lote que no termina no se da dos veces, el avance y el final en palabras llanas, un cambio de contraseña a mitad, DEHÚ sin responder); el paquete para enviar a mano (qué lleva, que se parte por tamaño, que no lleva la configuración ni secretos, que no cambia nada, que funciona sin acceso); y que la pantalla manual sigue funcionando.

## Probado con el DEHÚ real (18-11-2026)

En un PC con Windows 11 y Python 3.11, con un certificado de AiKit (no el vuestro), en solo lectura: `PeticionAcceso` bloqueada y la entrega apuntando a un gestor de mentira en el propio PC.

- Listados reales: 27 pendientes (23 comunicaciones de Red.es, ninguna notificación de Red.es) y 74 realizadas en 29 días (67 de Red.es). Lo de otros organismos no se tocó.
- Bajada real con `ConsultaRealizadas` de una muestra de 5 envíos de Red.es (3 comunicaciones y 2 notificaciones ya aceptadas): los 5 bajan, llevan su `meta.json` y llegan al gestor de mentira por los tres pasos. Los 3 PDF de comunicación son idénticos (sha256) a los que AiKit ya tenía de esos envíos.
- Una segunda pasada no vuelve a pedir nada a DEHÚ.
- La lista de pendientes llegó vacía, como correspondía.
- Windows: las pruebas pasan; DPAPI cifra y descifra, también entre la sesión que configura y la tarea programada, y la contraseña no aparece en `auto_config.json`; `auto configurar` pregunta y guarda; `auto programar` crea la tarea, la tarea ejecuta una pasada completa (resultado 0) y `auto desprogramar` la borra.
- El `.exe` se genera con la orden de abajo y hace una pasada real.

Esa prueba encontró y corrigió dos fallos: el listado de realizadas pedía 30 días y DEHÚ lo rechazaba (error 4224), y una pasada con el listado rechazado terminaba sin error.

## Probado el acceso a AiKit (25-11-2026)

En el mismo PC (Windows 11, Python 3.11). No se subió ningún fichero a la web real de AiKit ni se llamó a DEHÚ.

- Con el `.exe` generado con este código y la **web real de AiKit, solo entrar y salir**: la contraseña pegada dos veces y un usuario equivocado se rechazan y no se guarda nada (no llega a existir `auto_config.json`); una contraseña con el carácter de Ctrl+V se rechaza sin llamar a AiKit; el acceso bueno se acepta y se guarda, con la contraseña cifrada por DPAPI y sin texto en claro en `auto_config.json`. Sin acceso comprobado, «Subir ahora» contesta que antes hay que configurarlo.
- Un `auto_config.json` como el que deja la versión anterior (acceso guardado sin fecha de comprobación) se lee como «guardado pero sin comprobar» y no se sube nada con él.
- Desde el código, con DPAPI de verdad y un **gestor de mentira en el propio PC**: una contraseña mala se rechaza; con la buena, el cuadro ofrece subir lo guardado y «Subir ahora» entrega 45 ficheros en 3 lotes sin llamar a DEHÚ ni enviar lista de pendientes. Después no hay contraseña en claro en ningún fichero de la carpeta ni en la salida del programa.
- En el uso real en QEV (24 y 25-11-2026), con vuestro certificado, las pasadas bajaron de DEHÚ lo de Red.es (998 y 357 envíos las dos primeras), una alcanzó el tope diario de aperturas (4305) y siguió con lo ya realizado, y ninguna pudo subir porque el acceso a AiKit guardado por la ventana de órdenes no era correcto. De ahí sale la configuración en pantalla.

## Probada la subida con el código del gestor (25-11-2026)

AiKit montó una copia de prueba del gestor: el mismo código que la web real (su versión de ese día), con una base de datos propia y vacía y sin acceso a ningún sistema real. Contra ella, sin tocar la web real y sin llamar a DEHÚ, con ZIP de muestra con la misma forma que escribe el programa (unos 150 KB cada uno, con su `meta.json`):

- **Los tres pasos y la lista de pendientes funcionan con el código real del gestor.** Entrar, subir, procesar y esperar; la lista de pendientes queda guardada en el gestor. Los primeros 23 ficheros los clasificó el gestor de verdad; un lote de 20 tardó 62 segundos. Después de esa prueba AiKit cambió el modelo con el que el gestor clasifica: ahora un lote de 20 tarda unos 2 minutos y medio, y es la cifra que usa la pantalla.
- **El final «AiKit no puede procesar» se vio de verdad**: en mitad de la prueba la copia del gestor dejó de poder clasificar. La subida paró, lo ya subido quedó anotado, el resto siguió en `por_subir` y la pantalla lo dijo. Desde ese momento, en la copia de prueba la clasificación se sustituyó por una respuesta fija con la misma duración; todo lo demás siguió siendo el código real.
- **La sesión**: con sesiones de 100 segundos en la copia de prueba, una subida rechazada por sesión caducada se repitió tras entrar de nuevo y no se perdió ningún lote; con la renovación adelantada no llegó a caducar.
- **Con el `.exe` de esta versión en Windows 11**, llevado a la copia de prueba por un puente local (el programa no se cambió: seguía apuntando a la dirección real): la configuración que deja la versión anterior sale como «sin comprobar»; la contraseña pegada dos veces se rechaza; con la buena, 60 ficheros suben en 3 lotes de unos 63 segundos. Provocando fallos en el puente: una subida rechazada por sesión caducada se repite sola; un lote cortado y otro que el gestor no confirma se reintentan fichero a fichero; cerrando el programa a mitad, al abrirlo sabe cuántos faltan y «Subir ahora» termina. Al final, 60 de 60 entregados, ninguno dos veces en `enviado.log`.
- **El paquete para enviar a mano**: una parte con 45 ZIP, cargada tal cual en el gestor por su pantalla de subir, se abre sola y procesa los 45.
- **Recibir dos veces el mismo envío** deja una fila más en el histórico del gestor y una sola en su registro de envíos a la base de datos de AiKit.
- Un lote de 20 ZIP de 2,5 MB (50 MB en una sola subida) entra.

## Lo que no está probado

- **Aceptar notificaciones pendientes** (`PeticionAcceso` de una notificación): no se ha ejecutado contra el DEHÚ real, a propósito. **Abrir comunicaciones pendientes** solo se ha ejecutado en vuestro uso real.
- **La web real de AiKit**: con ella solo se ha probado entrar y salir. La subida se ha probado contra la copia de prueba (el mismo código, otra máquina y otra base de datos), no contra la web real ni desde vuestra red.
- **La lista de pendientes con datos reales**: la copia de prueba la guarda; a la web real no ha llegado ninguna.
- **Una pasada completa lanzada con el botón** (bajar de DEHÚ y subir en la misma pasada) con este `.exe`: bajar se ha probado en vuestro uso real con la versión anterior y subir con este, por separado. La pantalla se ha manejado con Chromium; no con Edge.
- **La renovación de la sesión a las 6 horas** con una subida real de esa duración: se ha probado acortando los tiempos.
- **El tope de DEHÚ por envío** (4225): solo está probado con respuestas de mentira.
- **Cómo clasifica el gestor un `<nombre>.pdf.zip`** (una comunicación que DEHÚ entrega como PDF suelto).
- **Que Windows no suspenda el ordenador durante una pasada**: la llamada es la estándar de Windows; no se ha dejado un equipo desatendido para comprobarlo.
- El `.exe` de `dist/` es el de la versión anterior: no incluye nada de esto. Para generarlo con este código: `pyinstaller --onefile --console --add-data "templates;templates" --add-data "static;static" --name DescargadorDehuQEV app.py` (con consola, porque `auto configurar` pregunta por teclado). Con esa orden se generó el de la prueba.
