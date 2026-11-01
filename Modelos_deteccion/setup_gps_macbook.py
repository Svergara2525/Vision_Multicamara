"""App de desarrollo para que macOS pueda solicitar permisos de ubicación."""
from pathlib import Path
from setuptools import setup

BASE = Path(__file__).resolve().parent

if __name__ == "__main__":
    setup(
        name="gps_macbook",
        version="1.0.0",
        app=[str(BASE / "gps_macbook.py")],
        options={"py2app": {
            "alias": True,
            "dist_dir": str(BASE / ".gps_macbook/dist"),
            "bdist_base": str(BASE / ".gps_macbook/build"),
            "argv_emulation": False,
            "plist": {
                "CFBundleIdentifier": "local.visionmulticamara.gpsmacbook",
                "CFBundleName": "gps_macbook",
                "CFBundleDisplayName": "GPS MacBook",
                "LSUIElement": True,
                "NSLocationUsageDescription": "Mostrar la ubicación del Mac junto al vídeo de su webcam.",
                "NSLocationWhenInUseUsageDescription": "Mostrar la ubicación del Mac junto al vídeo de su webcam.",
            },
        }},
    )
