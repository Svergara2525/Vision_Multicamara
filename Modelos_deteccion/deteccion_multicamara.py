import argparse
import json
import math
import socket
import multiprocessing as mp
import sys
from queue import Empty, Full
import time
from pathlib import Path

import cv2
import numpy as np

from gps_macbook import start_location_service, stop_location_service


MODEL_PATH = Path(__file__).resolve().with_name("yolo26n.pt")

CAMERAS = [
    {"name": "Webcam", "index": 1},
    {"name": "iPhone", "index": 0},
]
CONFIDENCE = 0.5
IMG_SIZE = 640
CLASSES = [0]  # Solo personas en ambas cámaras.
# Orden de CAMERAS: webcam principal primero, iPhone superpuesto después.
VIEW_WIDTH, VIEW_HEIGHT = 1280, 720
PIP_WIDTH, PIP_HEIGHT = 384, 216
PIP_MARGIN = 24
PIP_BORDER = 2
WINDOW_NAME = "Deteccion multicamara - YOLO"
GPS_MAX_AGE = 5.0  # Segundos desde la última recepción; no certifica la edad del fix GPS.


def fit_panel(frame, panel_width, panel_height):
    """Ajusta cada vista sin deformar la imagen."""
    height, width = frame.shape[:2]
    scale = min(panel_width / width, panel_height / height)
    resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
    panel = np.zeros((panel_height, panel_width, 3), dtype=np.uint8)
    y = (panel_height - resized.shape[0]) // 2
    x = (panel_width - resized.shape[1]) // 2
    panel[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return panel


def compose_views(panels, show_iphone=True):
    """Webcam de fondo e iPhone en la esquina inferior derecha."""
    combined = panels[0].copy()
    if not show_iphone or panels[1] is None:
        return combined
    x = VIEW_WIDTH - PIP_WIDTH - PIP_MARGIN
    y = VIEW_HEIGHT - PIP_HEIGHT - PIP_MARGIN
    cv2.rectangle(combined, (x - PIP_BORDER, y - PIP_BORDER),
                  (x + PIP_WIDTH + PIP_BORDER - 1, y + PIP_HEIGHT + PIP_BORDER - 1),
                  (255, 255, 255), -1)
    combined[y:y + PIP_HEIGHT, x:x + PIP_WIDTH] = panels[1]
    return combined



class GPSReceiver:
    """Recibe posiciones externas por UDP; el vídeo no contiene estas ubicaciones."""

    def __init__(self, host, port):
        self.positions = {}
        self.details = {}
        self.statuses = {}
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((host, port))
            self.socket.setblocking(False)
        except Exception:
            self.socket.close()
            raise
        print(f"[INFO] GPS UDP escuchando en {host}:{port}", flush=True)

    def poll(self):
        # Limitar el trabajo por frame aunque el emisor envíe muchos mensajes.
        for _ in range(64):
            try:
                data, _ = self.socket.recvfrom(4096)
            except BlockingIOError:
                break
            try:
                message = json.loads(data)
                camera = message["camera"]
                if message.get("type") == "status":
                    status = message.get("status")
                    if camera == "webcam" and status in ("waiting", "denied", "disabled", "unavailable"):
                        self.statuses[camera] = status
                    continue
                lat, lon = float(message["lat"]), float(message["lon"])
                if (camera not in ("webcam", "iphone")
                        or not math.isfinite(lat) or not math.isfinite(lon)
                        or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                    continue
            except (ValueError, TypeError, KeyError, UnicodeError):
                continue
            # Los emisores con fecha de medición no pueden refrescar fixes antiguos.
            try:
                timestamp = float(message.get("timestamp", time.time()))
                accuracy = float(message.get("accuracy_m", 0))
                if (not math.isfinite(timestamp) or not -5 <= time.time() - timestamp <= 30
                        or not math.isfinite(accuracy) or accuracy < 0):
                    continue
            except (TypeError, ValueError):
                continue
            previous = self.details.get(camera)
            if previous is not None and timestamp < previous[2]:
                continue
            self.statuses.pop(camera, None)
            self.positions[camera] = (lat, lon, time.monotonic())
            self.details[camera] = (message.get("source"), accuracy, timestamp)

    def text(self, camera):
        status = self.statuses.get(camera)
        if status is not None:
            labels = {
                "waiting": "MacBook: esperando ubicacion (revisa permiso)",
                "denied": "MacBook: permiso de Localizacion denegado",
                "disabled": "MacBook: Localizacion desactivada",
                "unavailable": "MacBook: sin medicion reciente",
            }
            return labels[status]
        position = self.positions.get(camera)
        if position is None:
            return "GPS: sin datos"
        lat, lon, received = position
        source, accuracy, timestamp = self.details.get(camera, (None, 0, time.time()))
        age = max(0, time.time() - timestamp)
        if time.monotonic() - received > GPS_MAX_AGE or age > 30:
            label = "MacBook" if source == "macbook" else "GPS"
            return f"{label}: sin actualizar (medicion hace {age:.0f} s)"
        if source == "macbook":
            return f"MacBook: {lat:.6f}, {lon:.6f} (+/- {accuracy:.0f} m) | hace {age:.0f} s"
        return f"GPS: {lat:.6f}, {lon:.6f}"

    def close(self):
        self.socket.close()


def annotated_panel(result, size, gps_text):
    """Dibuja las detecciones y solo la ubicación de la cámara en la esquina inferior."""
    panel = fit_panel(result.plot(), *size)
    small = size[0] <= PIP_WIDTH
    scale, line_height = (0.42, 17) if small else (0.6, 24)
    lines = [gps_text]
    # Fondo translúcido solo en la esquina inferior izquierda, sin cabecera.
    width = min(size[0], max(cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0]
                              for line in lines) + 20)
    top = size[1] - len(lines) * line_height - 12
    region = panel[top:, :width]
    region[:] = (region.astype(np.float32) * 0.3).astype(np.uint8)
    for row, line in enumerate(lines):
        cv2.putText(panel, line, (8, top + (row + 1) * line_height),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    return panel


def detection_worker(stop, messages, cameras, gps_config):
    import torch
    from ultralytics import YOLO

    global CAMERAS
    CAMERAS = cameras
    if len(CAMERAS) != 2:
        raise ValueError("Configura dos camaras: primero la webcam y despues el iPhone.")
    if (min(PIP_WIDTH, PIP_HEIGHT, PIP_BORDER) <= 0
            or PIP_MARGIN < PIP_BORDER
            or PIP_WIDTH + 2 * PIP_MARGIN > VIEW_WIDTH
            or PIP_HEIGHT + 2 * PIP_MARGIN > VIEW_HEIGHT):
        raise ValueError("El recuadro del iPhone y su borde deben caber en la vista principal.")
    indices = [camera["index"] for camera in CAMERAS if camera["index"] is not None]
    if len(set(indices)) != len(indices):
        raise ValueError("Cada camara debe tener un indice distinto.")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"[INFO] Utilizando {device}")
    captures = []
    failed = set()
    gps = None

    try:
        gps = GPSReceiver(*gps_config)
        model = YOLO(str(MODEL_PATH))
        for i, camera in enumerate(CAMERAS):
            if camera["index"] is None:
                captures.append(None)
                failed.add(i)
                continue
            cap = cv2.VideoCapture(camera["index"], cv2.CAP_ANY)
            captures.append(cap)
            if not cap.isOpened():
                if i == 1:
                    failed.add(i)
                    print(f"[AVISO] No se pudo abrir iPhone (indice {camera['index']}). "
                          "Se continua solo con la webcam. Conectalo y reinicia para mostrarlo.", flush=True)
                    continue
                raise RuntimeError(
                    f"No se pudo abrir {camera['name']} (indice {camera['index']}). "
                    "Comprueba la conexion y los permisos. Si solo hay una camara (indice 0), "
                    "ejecuta: python3 deteccion_multicamara.py --webcam 0 --sin-iphone"
                )
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        while not stop.is_set():
            start_time = time.perf_counter()
            frames, active_indices = [], []
            for i, cap in enumerate(captures):
                if i in failed:
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    failed.add(i)
                    print(f"[ERROR] Sin imagen de {CAMERAS[i]['name']}. Reinicia el programa tras reconectarla.")
                    continue
                frames.append(frame)
                active_indices.append(i)

            if not frames:
                raise RuntimeError("Ninguna camara esta proporcionando imagen.")

            # Un mismo modelo procesa una imagen de cada cámara en cada iteración.
            results = model.predict(
                source=frames,
                conf=CONFIDENCE,
                imgsz=IMG_SIZE,
                device=device,
                classes=CLASSES,
                verbose=False,
            )
            sizes = [(VIEW_WIDTH, VIEW_HEIGHT), (PIP_WIDTH, PIP_HEIGHT)]
            panels = [np.zeros((height, width, 3), dtype=np.uint8) for width, height in sizes]
            if 1 in failed:
                panels[1] = None
            for i in failed - {1}:
                cv2.putText(panels[i], "Sin imagen", (15, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

            gps.poll()
            for i, result in zip(active_indices, results):
                camera_id = "webcam" if i == 0 else "iphone"
                panels[i] = annotated_panel(result, sizes[i], gps.text(camera_id))

            fps = 1 / max(time.perf_counter() - start_time, 1e-9)
            cv2.putText(panels[0], f"FPS del conjunto: {fps:.1f}", (15, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            publish_latest(messages, ("frame", panels))
    finally:
        if gps is not None:
            gps.close()
        for camera, cap in zip(CAMERAS, captures):
            if cap is None:
                continue
            print(f"[INFO] Liberando {camera['name']}...", flush=True)
            cap.release()
        print("[INFO] Camaras liberadas.", flush=True)


def publish_latest(messages, message):
    """Evita acumular vídeo si la interfaz va más lenta que el detector."""
    try:
        messages.put_nowait(message)
    except Full:
        try:
            messages.get_nowait()
        except Empty:
            pass
        try:
            messages.put_nowait(message)
        except Full:
            pass


def worker_entry(stop, messages, cameras, gps_config):
    # El hijo no debe quedarse esperando a que la interfaz lea el último frame.
    try:
        detection_worker(stop, messages, cameras, gps_config)
    except Exception as exc:
        publish_latest(messages, ("error", str(exc)))
        # Entregar el error antes de terminar, sin imprimir otro traceback.
        messages.close()
        messages.join_thread()
        raise SystemExit(1)
    finally:
        messages.cancel_join_thread()


def stop_worker(worker, stop, timeout=3):
    stop.set()
    worker.join(timeout)
    if worker.is_alive():
        print("[INFO] El detector no responde; terminando su proceso.", flush=True)
        worker.terminate()
        worker.join(1)
    if worker.is_alive():
        worker.kill()
        worker.join(1)


def run_detection(cameras, gps_config=("127.0.0.1", 5005), start_mac_gps=True):
    # HighGUI se ejecuta en el hilo principal. Captura e inferencia van en un
    # proceso independiente para que read(), YOLO o release() no bloqueen Q.
    context = mp.get_context("spawn")
    stop = context.Event()
    messages = context.Queue(maxsize=2)
    worker = context.Process(target=worker_entry, args=(stop, messages, cameras, gps_config))
    window_created = False
    started = False
    show_iphone = True
    last_panels = None
    gps_process = None
    gps_exit_reported = False
    try:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        window_created = True
        cv2.resizeWindow(WINDOW_NAME, VIEW_WIDTH, VIEW_HEIGHT)
        waiting = np.zeros((VIEW_HEIGHT, VIEW_WIDTH, 3), dtype=np.uint8)
        cv2.putText(waiting, "Iniciando camaras y modelo... Q / Esc para salir",
                    (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        cv2.imshow(WINDOW_NAME, waiting)
        worker.start()
        started = True
        if start_mac_gps:
            try:
                gps_process = start_location_service(*gps_config)
            except Exception as exc:
                print(f"[AVISO] No se pudo iniciar la ubicación del Mac: {exc}", flush=True)
        print("[INFO] I: mostrar/ocultar iPhone. q, Q o Esc: salir. Ctrl+C en la terminal.", flush=True)
        while True:
            if gps_process is not None and not gps_exit_reported and gps_process.poll() is not None:
                print("[AVISO] El servicio GPS terminó. Revisa los permisos de Localización.", flush=True)
                gps_exit_reported = True
            key = cv2.waitKey(10) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (ord("i"), ord("I")):
                show_iphone = not show_iphone
                if last_panels is not None:
                    cv2.imshow(WINDOW_NAME, compose_views(last_panels, show_iphone))
            try:
                kind, payload = messages.get_nowait()
            except Empty:
                if not worker.is_alive():
                    if worker.exitcode:
                        raise RuntimeError("El detector termino con un error. Revisa los mensajes de la terminal.")
                    break
                continue
            if kind == "error":
                raise RuntimeError(payload)
            last_panels = payload
            cv2.imshow(WINDOW_NAME, compose_views(last_panels, show_iphone))
    except KeyboardInterrupt:
        print("[INFO] Ejecucion interrumpida.", flush=True)
    finally:
        stop.set()
        try:
            if window_created:
                cv2.destroyAllWindows()
                for _ in range(5):
                    cv2.waitKey(10)
        finally:
            try:
                if started:
                    stop_worker(worker, stop)
            finally:
                try:
                    stop_location_service(gps_process)
                finally:
                    messages.close()
            print("[INFO] Programa cerrado.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Webcam principal e iPhone superpuesto; deteccion de personas.")
    parser.add_argument("--webcam", type=int, default=CAMERAS[0]["index"], help="Indice de la webcam principal")
    parser.add_argument("--iphone", type=int, default=CAMERAS[1]["index"], help="Indice de la camara superpuesta")
    parser.add_argument("--sin-iphone", action="store_true", help="Usar solo la webcam principal")
    parser.add_argument("--gps-host", default="127.0.0.1", help="IP local donde recibir GPS UDP; 0.0.0.0 para recibir desde otro equipo")
    parser.add_argument("--gps-port", type=int, default=5005, help="Puerto de recepción GPS UDP (por defecto: 5005)")
    parser.add_argument("--sin-gps-mac", action="store_true", help="No iniciar el GPS del Mac (por ejemplo, si usas otro emisor)")
    args = parser.parse_args()
    if not 1 <= args.gps_port <= 65535:
        parser.error("El puerto GPS debe estar entre 1 y 65535.")
    if not args.sin_iphone and args.webcam == args.iphone:
        parser.error("Las dos camaras deben tener indices distintos.")
    try:
        run_detection([{"name": "Webcam", "index": args.webcam},
                       {"name": "iPhone", "index": None if args.sin_iphone else args.iphone}],
                      gps_config=(args.gps_host, args.gps_port),
                      start_mac_gps=not args.sin_gps_mac)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
