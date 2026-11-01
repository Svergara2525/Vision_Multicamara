# Vision_Multicamara

Detección de personas en dos cámaras: webcam principal e iPhone superpuesto en la esquina inferior derecha.

Ejecuta desde la raíz del proyecto, con el entorno `.venv` y las dependencias del notebook instaladas:

```sh
.venv/bin/python Modelos_deteccion/deteccion_multicamara.py --webcam 1 --iphone 0
```

Los índices conservan la configuración del notebook; ajústalos según tus dispositivos.
El modelo se carga desde `Modelos_deteccion/yolo26n.pt`, independientemente del directorio de ejecución.

Pulsa **I** con la ventana seleccionada para ocultar o mostrar el recuadro del iPhone.
La cámara sigue capturando y detectando personas mientras el recuadro está oculto.
Las vistas no muestran cabeceras con el nombre de cámara ni el contador de personas.

Pulsa **q**, **Q** o **Esc** con la ventana seleccionada, cierra la ventana o pulsa **Ctrl+C** en la terminal.
La interfaz se ejecuta en el proceso principal. La captura y YOLO se ejecutan en otro proceso,
para que una operación de cámara bloqueada no impida atender el teclado.
Al salir, se espera hasta tres segundos para liberar las cámaras normalmente; si el detector no
responde, se termina su proceso, con terminación forzada como último recurso.

Antes de usar el script, detén el notebook y reinicia su kernel si retenía las cámaras.
No ejecutes ambos a la vez con las mismas cámaras.
El notebook se conserva para experimentar; se recomienda el script para la visualización en directo.

Si OpenCV solo encuentra una cámara con índice 0, ejecuta:

```sh
.venv/bin/python Modelos_deteccion/deteccion_multicamara.py --webcam 0 --sin-iphone
```

Para intentar ambas con webcam 0 e iPhone 1, usa `--webcam 0 --iphone 1`.
Si la cámara secundaria no se abre, se continúa solo con la principal sin recuadro.
Conecta el iPhone y reinicia para incorporarlo. No se intercambian automáticamente los índices.

Coordenadas en pantalla:

- Cada vista muestra abajo a la izquierda únicamente la ubicación de su cámara. Las personas conservan los recuadros de detección, sin coordenadas ni marcas de centro.
- El GPS mostrado es la ubicación de la cámara, recibida de una fuente externa; no es el GPS de la persona. La geolocalización de personas requiere calibración y datos adicionales.
- El vídeo de OpenCV no aporta ubicación GPS. El receptor UDP escucha por defecto en `127.0.0.1:5005`. Sin emisor muestra `GPS: sin datos`; si pasan cinco segundos sin recibir una posición válida muestra `GPS: sin actualizar`.

La fuente de GPS debe enviar un objeto JSON por datagrama UDP, en WGS84 y grados decimales:

```text
{"camera": "webcam", "lat": LATITUD_REAL, "lon": LONGITUD_REAL}
{"camera": "iphone", "lat": LATITUD_REAL, "lon": LONGITUD_REAL}
```

Sustituye los marcadores por números reales de cada receptor. `camera` identifica la vista; no se copia la posición de una cámara a la otra.
El emisor debe enviar fixes recientes de forma continua. El plazo de cinco segundos se cuenta desde la recepción, no desde la medición del sensor.
Si la fuente es una app, un receptor serie/NMEA o un dron con otro protocolo, necesita un adaptador que produzca estos mensajes.

Para recibir desde el iPhone u otro equipo en la red local:

```sh
.venv/bin/python Modelos_deteccion/deteccion_multicamara.py --gps-host 0.0.0.0 --gps-port 5005
```

Configura el emisor con la IP local del Mac y ese puerto. El receptor no autentica mensajes; úsalo en una red de confianza. No es necesario abrir puertos del router.

Ubicación del MacBook en tiempo real:

Se obtiene con Core Location en `Modelos_deteccion/gps_macbook.py`. El script envía
la ubicación del Mac al receptor local del visor, como posición de la webcam principal.
La vista principal muestra latitud, longitud y precisión estimada abajo a la izquierda.
No se envía esa posición a la vista del iPhone.

Las dependencias están en `Modelos_deteccion/requirements-gps-macos.txt`:

```sh
.venv/bin/python -m pip install -r Modelos_deteccion/requirements-gps-macos.txt
```

Desde la raíz del proyecto, basta con ejecutar:

```sh
.venv/bin/python Modelos_deteccion/deteccion_multicamara.py
```

El visor importa las funciones de `gps_macbook.py`, inicia el servicio de ubicación
con el mismo puerto GPS y lo detiene automáticamente al cerrar la ventana o salir con Q/Esc.
No hace falta una segunda terminal. Si el GPS no puede iniciarse o se deniega el permiso,
el vídeo sigue funcionando y muestra el estado de ubicación disponible.
En Linux no se inicia Core Location; el receptor externo sigue disponible.
Usa `--sin-gps-mac` para evitar el arranque automático si utilizas otro emisor.
`gps_macbook.py` también se puede ejecutar por separado para diagnosticar la localización.

En la primera ejecución se genera una app auxiliar de desarrollo en
`Modelos_deteccion/.gps_macbook/`, con identidad y textos de permisos de ubicación.
Acepta el permiso de `gps_macbook` / GPS MacBook. Si se deniega, revisa
Ajustes del Sistema > Privacidad y seguridad > Localización.
El script no usa geolocalización por IP; la precisión depende de los servicios de ubicación de macOS.
Mantén la Wi-Fi activada para ayudar a determinar la posición del Mac.
Al salir del visor también se detiene su servicio GPS. Si ejecutas el GPS por separado, pulsa Ctrl+C en su terminal para detenerlo.

Cada medición nueva de Core Location se envía en cuanto llega al bucle (aproximadamente cada 0.1 s).
Se reenvía cada segundo el último fix válido conservando la fecha de medición; se descartan mediciones de más de treinta segundos
y precisiones inválidas. El receptor comprueba también la fecha de medición. Si no hay una
posición reciente no se generan coordenadas ficticias.
Para cambiar el puerto, combina `gps_macbook.py --port 5006` con
`deteccion_multicamara.py --gps-port 5006`.
La app auxiliar depende del entorno Python que la construyó. Si lo cambias, instala las
dependencias allí y ejecuta `gps_macbook.py --rebuild-app`.

Seguimiento del movimiento del Mac: el servicio mantiene `startUpdatingLocation()` activo,
solicita la mejor precisión y no filtra por distancia. El texto indica la antigüedad de la
medición (`hace N s`) y su precisión estimada. Los reenvíos no reinician esa antigüedad.
La frecuencia real de medición depende de macOS; no se garantiza una posición nueva cada segundo.
El Mac determina su ubicación con las redes Wi-Fi locales, por lo que movimientos pequeños
pueden no reflejarse en las coordenadas. Si necesitas seguimiento más preciso, hace falta
una fuente de posicionamiento adecuada (por ejemplo, un receptor GNSS externo).
