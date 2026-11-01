"""Ubicación del Mac por Core Location -> visor multicámara (UDP local).

Ejecutar: python3 gps_macbook.py
En la primera ejecución crea una app auxiliar con los textos de permisos de macOS.
No sustituye la ubicación del iPhone ni utiliza geolocalización por IP.
"""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


MAX_FIX_AGE = 30.0


def location_payload(location, now=None):
    """Descarta fixes inválidos o antiguos; conserva la fecha de la medición."""
    coordinate = location.coordinate()
    lat, lon = float(coordinate.latitude), float(coordinate.longitude)
    accuracy = float(location.horizontalAccuracy())
    timestamp = float(location.timestamp().timeIntervalSince1970())
    age = (time.time() if now is None else now) - timestamp
    if (not all(math.isfinite(v) for v in (lat, lon, accuracy, timestamp))
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or accuracy < 0 or not -5 <= age <= MAX_FIX_AGE):
        return None
    return {"camera": "webcam", "lat": lat, "lon": lon,
            "accuracy_m": accuracy, "timestamp": timestamp, "source": "macbook"}



def send_status(sender, args, status):
    message = {"camera": "webcam", "type": "status", "status": status, "source": "macbook"}
    sender.sendto(json.dumps(message).encode("utf-8"), (args.host, args.port))

def prepare_app(rebuild=False):
    """Da a Core Location una identidad estable y un Info.plist propio."""
    base = Path(__file__).resolve().parent
    executable = base / ".gps_macbook/dist/gps_macbook.app/Contents/MacOS/gps_macbook"
    if rebuild or not executable.exists():
        try:
            import py2app  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Faltan las dependencias. Ejecuta con este mismo Python: "
                "python3 -m pip install -r requirements-gps-macos.txt"
            ) from None
        print("[INFO] Preparando app auxiliar para los permisos de ubicación...", flush=True)
        subprocess.run([sys.executable, str(base / "setup_gps_macbook.py"), "py2app"],
                       cwd=base, check=True)
    if not executable.exists():
        raise RuntimeError("No se ha generado la app auxiliar de ubicación.")
    return executable


def launch_app(rebuild=False):
    executable = prepare_app(rebuild)
    os.execv(str(executable), [str(executable), *sys.argv[1:]])


def start_location_service(host="127.0.0.1", port=5005):
    """Arranca el GPS desde el visor, conservando Core Location en su propio run loop."""
    if sys.platform != "darwin":
        return None
    executable = prepare_app()
    destination = "127.0.0.1" if host == "0.0.0.0" else host
    process = subprocess.Popen([str(executable), "--host", destination, "--port", str(port)])
    print("[INFO] Servicio de ubicación del Mac iniciado.", flush=True)
    return process


def stop_location_service(process, timeout=3):
    """Detiene únicamente el servicio GPS iniciado por este visor."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    else:
        process.wait()



def run_location(args):
    try:
        import objc
        import CoreLocation as CL
        from Foundation import NSDate, NSRunLoop, NSObject
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        raise RuntimeError(
            "Instala las dependencias con el mismo Python que ejecuta este script: "
            "python3 -m pip install -r requirements-gps-macos.txt"
        ) from None

    class MacBookLocationDelegate(NSObject):
        def locationManager_didUpdateLocations_(self, manager, locations):
            if locations:
                payload = location_payload(locations[-1])
                if payload is not None and (self.latest is None
                                            or payload["timestamp"] >= self.latest["timestamp"]):
                    self.latest = payload
                    self.location_warning = False

        def locationManager_didFailWithError_(self, manager, error):
            self.latest = None
            if int(error.code()) == int(CL.kCLErrorDenied):
                self.failure = "Permiso de ubicación denegado o localización desactivada."
            elif not self.location_warning:
                print(f"[AVISO] Core Location: {error.localizedDescription()}", flush=True)
                self.location_warning = True

        def locationManagerDidChangeAuthorization_(self, manager):
            self.checkAuthorization_(manager)

        def locationManager_didChangeAuthorizationStatus_(self, manager, status):
            self.checkAuthorization_(manager)

        @objc.python_method
        def checkAuthorization_(self, manager):
            status = manager.authorizationStatus()
            if status in (CL.kCLAuthorizationStatusDenied, CL.kCLAuthorizationStatusRestricted):
                self.latest = None
                self.failure = "Acceso a ubicación denegado o restringido."

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    delegate = MacBookLocationDelegate.alloc().init()
    delegate.latest = None
    delegate.failure = None
    delegate.location_warning = False
    manager = CL.CLLocationManager.alloc().init()
    manager.setDelegate_(delegate)
    manager.setDesiredAccuracy_(CL.kCLLocationAccuracyBest)
    # Recibir todas las actualizaciones que macOS pueda medir, sin umbral de distancia.
    manager.setDistanceFilter_(CL.kCLDistanceFilterNone)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if not CL.CLLocationManager.locationServicesEnabled():
            send_status(sender, args, "disabled")
            raise RuntimeError("Activa Localización en Ajustes del Sistema > Privacidad y seguridad.")
        send_status(sender, args, "waiting")
        manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()
        print(f"[INFO] Esperando ubicación del Mac. Destino: {args.host}:{args.port} (webcam).", flush=True)
        print("[INFO] Acepta el permiso de gps_macbook. Ctrl+C para terminar.", flush=True)
        last_send = 0.0
        last_sent_timestamp = None
        last_status = 0.0
        last_warning = time.monotonic()
        while not stopped:
            # Core Location necesita el run loop del hilo principal para sus callbacks.
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            if delegate.failure:
                send_status(sender, args, "denied")
                raise RuntimeError(delegate.failure + " Revisa gps_macbook en Privacidad y seguridad > Localización.")
            now = time.monotonic()
            payload = delegate.latest
            if payload is not None and time.time() - payload["timestamp"] <= MAX_FIX_AGE:
                new_fix = payload["timestamp"] != last_sent_timestamp
                # Publicar cada medición nueva en cuanto llega. Los reenvíos solo
                # mantienen la comunicación y conservan la fecha original del fix.
                if new_fix or now - last_send >= args.interval:
                    sender.sendto(json.dumps(payload, allow_nan=False).encode("utf-8"), (args.host, args.port))
                    if new_fix:
                        print(f"[MAC] Nueva medicion: {payload['lat']:.6f}, {payload['lon']:.6f} "
                              f"Precision: {payload['accuracy_m']:.0f} m", flush=True)
                    last_sent_timestamp = payload["timestamp"]
                    last_send = now
            else:
                if now - last_status >= args.interval:
                    send_status(sender, args, "waiting" if last_sent_timestamp is None else "unavailable")
                    last_status = now
                if now - last_warning >= 15:
                    print("[AVISO] Sin ubicación reciente. Comprueba Localización, el permiso y la conexión Wi-Fi.", flush=True)
                    last_warning = now
    finally:
        manager.stopUpdatingLocation()
        manager.setDelegate_(None)
        sender.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        print("[INFO] Ubicación del Mac detenida.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Envía ubicación real del Mac al visor multicámara.")
    parser.add_argument("--host", default="127.0.0.1", help="Destino UDP (por defecto: este Mac)")
    parser.add_argument("--port", type=int, default=5005, help="Puerto GPS del visor")
    parser.add_argument("--interval", type=float, default=1.0, help="Segundos entre reenvíos de la última medición (0.1 a 4); las nuevas se envían al llegar")
    parser.add_argument("--rebuild-app", action="store_true", help="Regenerar la app auxiliar después de cambiar de Python")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("El puerto debe estar entre 1 y 65535.")
    if not math.isfinite(args.interval) or not 0.1 <= args.interval <= 4:
        parser.error("El intervalo debe estar entre 0.1 y 4 segundos.")
    if sys.platform != "darwin":
        parser.error("Este script utiliza Core Location y solo funciona en macOS.")
    try:
        if not getattr(sys, "frozen", False):
            launch_app(args.rebuild_app)
        run_location(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
