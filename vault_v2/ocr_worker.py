"""Private one-page OCR child. Fixed local models, stdin image, bounded JSON."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import sys

MODEL_HASHES = {
    "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "rus": "e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225",
}
MAX_INPUT = 32 * 1024 * 1024
MAX_TEXT = 100000
ENGINE_VERSIONS = frozenset({"tesseract 5.5.2", "tesseract 5.5.3"})


def _deny_socket(*args, **kwargs):
    raise RuntimeError("Python network is not used by this OCR worker")


def _models():
    directory = Path(__file__).absolute().parent / "ocr_models"
    for part in (directory, *directory.parents):
        if part.is_symlink() or part.is_junction():
            return None, "models_changed"
    for language, expected in MODEL_HASHES.items():
        path = directory / (language + ".traineddata")
        if not path.is_file():
            return None, "models_missing"
        if path.is_symlink() or path.is_junction() or path.stat().st_size > MAX_INPUT:
            return None, "models_changed"
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return None, "models_changed"
    return directory, None


def main():
    # Python paths only: this is not a native-DLL or OS network sandbox.
    socket.socket = _deny_socket
    try:
        directory, error = _models()
    except OSError:
        directory, error = None, "models_missing"
    if error:
        return {"error": error}
    try:
        from PIL import Image, ImageOps
        import tesserocr
    except ImportError:
        return {"error": "engine_unavailable"}
    engine_version = tesserocr.tesseract_version().splitlines()[0]
    if engine_version not in ENGINE_VERSIONS:
        return {"error": "engine_changed"}
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if not data or len(data) > MAX_INPUT:
        return {"error": "image_unreadable"}
    try:
        with Image.open(BytesIO(data)) as source:
            if getattr(source, "n_frames", 1) != 1:
                return {"error": "multi_frame_image"}
            width, height = source.size
            if min(width, height) < 1 or max(width, height) > 10000 or width * height > 16000000:
                return {"error": "image_too_large"}
            picture = ImageOps.exif_transpose(source).convert("RGB")
            try:
                with tesserocr.PyTessBaseAPI(path=str(directory), lang="eng+rus",
                                            oem=tesserocr.OEM.LSTM_ONLY,
                                            psm=tesserocr.PSM.AUTO) as engine:
                    engine.SetImage(picture)
                    text = engine.GetUTF8Text()
            finally:
                picture.close()
    except Exception:
        return {"error": "image_unreadable"}
    if len(text) > MAX_TEXT:
        return {"error": "text_too_large"}
    return {"text": text, "version": engine_version}


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = {"error": "worker_failed"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
