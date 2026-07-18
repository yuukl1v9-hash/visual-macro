"""Train a YOLOv8 button/UI detector and export it to ONNX.

This is a convenience wrapper around Ultralytics YOLO. You only need this to
*create* a model; running macros needs just onnxruntime.

Prereqs:
    pip install ultralytics
    # plus a YOLO-format dataset with a data.yaml (see models/README.md)

Usage:
    python train_detector.py --data path\\to\\data.yaml --epochs 50
    # -> runs/detect/train*/weights/best.onnx
    # copy that into models/ (e.g. as buttons.onnx) with a matching .names file

Notes:
    * --base picks the starting checkpoint; yolov8n = smallest/fastest.
    * Training uses your GPU automatically if PyTorch sees one, else CPU (slow).
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Train + export a YOLOv8 ONNX detector")
    ap.add_argument("--data", required=True, help="path to YOLO data.yaml")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--base", default="yolov8n.pt",
                    help="base checkpoint (yolov8n/s/m/l/x .pt)")
    args = ap.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError:
        print("Ultralytics is not installed. Run:\n    pip install ultralytics")
        return 1

    model = YOLO(args.base)
    model.train(data=args.data, epochs=args.epochs, imgsz=args.imgsz)
    onnx_path = model.export(format="onnx", imgsz=args.imgsz)
    print("\nExported ONNX model:")
    print(f"  {onnx_path}")
    print("Copy it into models/ (e.g. models/buttons.onnx) and add a matching")
    print("models/buttons.names listing one class per line.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
