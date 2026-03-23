#!/usr/bin/env python3
"""Coral Vision API — universal image recognition service using Google Coral Edge TPU.

Endpoints:
  POST /detect        — object detection (COCO SSD MobileNet v2)
  POST /classify      — image classification (ImageNet MobileNet v2)
  POST /analyze       — both detection + classification combined
  GET  /health        — service status and TPU info
  GET  /labels        — available label sets

All POST endpoints accept raw image bytes (JPEG/PNG).
Optional query params: ?threshold=0.4&top_k=5&lang=en|ru
"""

import base64
import io
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import numpy as np
from PIL import Image
from pycoral.adapters import common, detect, classify
from pycoral.utils.edgetpu import make_interpreter, list_edge_tpus
from pycoral.utils.dataset import read_label_file

MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
PORT = int(os.environ.get("PORT", "8090"))
DEFAULT_THRESHOLD = float(os.environ.get("THRESHOLD", "0.4"))
DEFAULT_TOP_K = int(os.environ.get("TOP_K", "10"))

DETECT_MODEL = os.path.join(MODELS_DIR, "ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite")
DETECT_LABELS = os.path.join(MODELS_DIR, "coco_labels.txt")
CLASSIFY_MODEL = os.path.join(MODELS_DIR, "cls_model.tflite")
CLASSIFY_LABELS = os.path.join(MODELS_DIR, "imagenet_labels.txt")

# Russian translations for COCO labels
COCO_RU = {
    "person": "человек", "bicycle": "велосипед", "car": "машина",
    "motorcycle": "мотоцикл", "airplane": "самолёт", "bus": "автобус",
    "train": "поезд", "truck": "грузовик", "boat": "лодка",
    "traffic light": "светофор", "fire hydrant": "пожарный гидрант",
    "stop sign": "знак стоп", "bench": "скамейка", "bird": "птица",
    "cat": "кот", "dog": "собака", "horse": "лошадь", "sheep": "овца",
    "cow": "корова", "elephant": "слон", "bear": "медведь",
    "zebra": "зебра", "giraffe": "жираф", "backpack": "рюкзак",
    "umbrella": "зонт", "handbag": "сумка", "tie": "галстук",
    "suitcase": "чемодан", "frisbee": "фрисби", "skis": "лыжи",
    "snowboard": "сноуборд", "sports ball": "мяч", "kite": "воздушный змей",
    "baseball bat": "бейсбольная бита", "skateboard": "скейтборд",
    "surfboard": "доска для сёрфинга", "tennis racket": "теннисная ракетка",
    "bottle": "бутылка", "wine glass": "бокал", "cup": "чашка",
    "fork": "вилка", "knife": "нож", "spoon": "ложка", "bowl": "миска",
    "banana": "банан", "apple": "яблоко", "sandwich": "бутерброд",
    "orange": "апельсин", "broccoli": "брокколи", "carrot": "морковь",
    "hot dog": "хот-дог", "pizza": "пицца", "donut": "пончик",
    "cake": "торт", "chair": "стул", "couch": "диван",
    "potted plant": "растение", "bed": "кровать", "dining table": "стол",
    "toilet": "туалет", "tv": "телевизор", "laptop": "ноутбук",
    "mouse": "мышка", "remote": "пульт", "keyboard": "клавиатура",
    "cell phone": "телефон", "microwave": "микроволновка", "oven": "духовка",
    "toaster": "тостер", "sink": "раковина", "refrigerator": "холодильник",
    "book": "книга", "clock": "часы", "vase": "ваза",
    "scissors": "ножницы", "teddy bear": "мишка", "hair drier": "фен",
    "toothbrush": "зубная щётка",
}


class CoralVision:
    """Manages Coral TPU models and inference."""

    def __init__(self):
        tpus = list_edge_tpus()
        print(f"Edge TPUs found: {len(tpus)}")
        for t in tpus:
            print(f"  {t['type']} @ {t['path']}")

        self.det_interp = self._load(DETECT_MODEL, "detection")
        self.cls_interp = self._load(CLASSIFY_MODEL, "classification")
        self.det_labels = read_label_file(DETECT_LABELS)
        self.cls_labels = read_label_file(CLASSIFY_LABELS)
        print(f"Labels: {len(self.det_labels)} detection, {len(self.cls_labels)} classification")
        print("Ready.")

    @staticmethod
    def _load(path, name):
        if not os.path.exists(path):
            print(f"WARNING: {name} model not found: {path}")
            return None
        print(f"Loading {name}: {os.path.basename(path)}")
        interp = make_interpreter(path)
        interp.allocate_tensors()
        return interp

    def detect(self, image_bytes, threshold=DEFAULT_THRESHOLD, lang="en"):
        if not self.det_interp:
            return {"error": "detection model not loaded"}

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        _, scale = common.set_resized_input(
            self.det_interp, img.size,
            lambda s: img.resize(s, Image.LANCZOS))

        start = time.monotonic()
        self.det_interp.invoke()
        elapsed = time.monotonic() - start

        objs = detect.get_objects(self.det_interp, threshold, scale)
        results = []
        for obj in objs:
            label_en = self.det_labels.get(obj.id, f"id:{obj.id}")
            item = {
                "id": obj.id,
                "label": COCO_RU.get(label_en, label_en) if lang == "ru" else label_en,
                "score": round(float(obj.score), 3),
                "bbox": {
                    "x1": obj.bbox.xmin, "y1": obj.bbox.ymin,
                    "x2": obj.bbox.xmax, "y2": obj.bbox.ymax,
                },
            }
            if lang == "ru":
                item["label_en"] = label_en
            results.append(item)

        return {
            "objects": results,
            "image_size": {"width": w, "height": h},
            "inference_ms": round(elapsed * 1000, 1),
        }

    def classify(self, image_bytes, top_k=DEFAULT_TOP_K, threshold=0.05, lang="en"):
        if not self.cls_interp:
            return {"error": "classification model not loaded"}

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        size = common.input_size(self.cls_interp)
        img_resized = img.resize(size, Image.LANCZOS)
        common.set_input(self.cls_interp, img_resized)

        start = time.monotonic()
        self.cls_interp.invoke()
        elapsed = time.monotonic() - start

        classes = classify.get_classes(self.cls_interp, top_k=top_k, score_threshold=threshold)
        results = []
        for c in classes:
            label = self.cls_labels.get(c.id, f"id:{c.id}")
            results.append({
                "id": int(c.id),
                "label": label,
                "score": round(float(c.score), 3),
            })

        return {
            "classes": results,
            "inference_ms": round(elapsed * 1000, 1),
        }

    def analyze(self, image_bytes, threshold=DEFAULT_THRESHOLD, top_k=DEFAULT_TOP_K, lang="en"):
        det = self.detect(image_bytes, threshold=threshold, lang=lang)
        cls = self.classify(image_bytes, top_k=top_k, lang=lang)
        return {
            "detection": det,
            "classification": cls,
        }


vision = CoralVision()


def parse_params(path):
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    return {
        "path": parsed.path,
        "threshold": float(qs.get("threshold", [str(DEFAULT_THRESHOLD)])[0]),
        "top_k": int(qs.get("top_k", [str(DEFAULT_TOP_K)])[0]),
        "lang": qs.get("lang", ["en"])[0],
    }


def json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return json_response(self, 400, {"error": "empty body"})

        body = self.rfile.read(length)
        params = parse_params(self.path)

        try:
            if params["path"] == "/detect":
                result = vision.detect(body, threshold=params["threshold"], lang=params["lang"])
            elif params["path"] == "/classify":
                result = vision.classify(body, top_k=params["top_k"], lang=params["lang"])
            elif params["path"] == "/analyze":
                result = vision.analyze(body, threshold=params["threshold"],
                                        top_k=params["top_k"], lang=params["lang"])
            elif params["path"] == "/detect_b64":
                data = json.loads(body)
                img_bytes = base64.b64decode(data.get("image", ""))
                result = vision.detect(img_bytes, threshold=params["threshold"], lang=params["lang"])
            elif params["path"] == "/analyze_b64":
                data = json.loads(body)
                img_bytes = base64.b64decode(data.get("image", ""))
                result = vision.analyze(img_bytes, threshold=params["threshold"],
                                        top_k=params["top_k"], lang=params["lang"])
            else:
                return json_response(self, 404, {"error": f"unknown endpoint: {params['path']}"})

            json_response(self, 200, result)

        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def do_GET(self):
        params = parse_params(self.path)

        if params["path"] == "/health":
            tpus = list_edge_tpus()
            json_response(self, 200, {
                "status": "ok",
                "tpu_count": len(tpus),
                "tpus": tpus,
                "models": {
                    "detection": os.path.basename(DETECT_MODEL) if vision.det_interp else None,
                    "classification": os.path.basename(CLASSIFY_MODEL) if vision.cls_interp else None,
                },
            })

        elif params["path"] == "/labels":
            lang = params["lang"]
            det = {}
            for k, v in vision.det_labels.items():
                det[str(k)] = COCO_RU.get(v, v) if lang == "ru" else v
            json_response(self, 200, {
                "detection": det,
                "classification": {str(k): v for k, v in vision.cls_labels.items()},
            })

        else:
            json_response(self, 404, {"error": f"unknown endpoint: {params['path']}"})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    print(f"Coral Vision API starting on port {PORT}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
