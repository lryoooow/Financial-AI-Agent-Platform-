"""Generate tracked Electron PNG and ICNS icons from the existing brand artwork."""

from pathlib import Path

from PIL import Image


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "fronted" / "electron" / "icons"
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(root / "assets" / "logo-1024.png") as source:
        art = source.convert("RGBA")
        art.resize((512, 512), Image.Resampling.LANCZOS).save(destination / "icon.png")
        art.resize((44, 44), Image.Resampling.LANCZOS).save(destination / "tray@2x.png")
        art.resize((22, 22), Image.Resampling.LANCZOS).save(destination / "tray.png")
        art.save(destination / "icon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])


if __name__ == "__main__":
    main()
