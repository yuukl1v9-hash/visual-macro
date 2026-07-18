# models/

Drop a **YOLOv8-style ONNX object-detection model** here to use the
`find_object_click` / `wait_for_object` steps. Unlike image anchors (which need a
near-pixel match), a trained model finds a *class* of element — e.g. any button —
even when its text, size, or theme changes.

## What goes here

```
models/
├─ buttons.onnx        # your exported model
└─ buttons.names       # one class name per line (optional but recommended)
```

`buttons.names` example:
```
button
checkbox
text_field
icon
```

If no `.names` (or `.txt` / `classes.txt`) file is found, target classes are
matched by **numeric index** instead (e.g. target `"0"`).

The engine auto-picks the first `*.onnx` in this folder. To use a specific file,
set the step's **Model** field to its filename (e.g. `buttons.onnx`).

## Requirement

```powershell
pip install onnxruntime
```

(Already present if you installed the OCR extra, `rapidocr-onnxruntime`.)

## Getting a model

**Option A — train your own (most accurate for your app).**
1. Capture 100–300 screenshots of your target app.
2. Label the elements with a tool like [Roboflow](https://roboflow.com),
   [LabelImg](https://github.com/HumanSignal/labelImg), or Label Studio —
   export in **YOLO** format. You'll get a `data.yaml`.
3. Train + export to ONNX:
   ```powershell
   pip install ultralytics
   python train_detector.py --data path\to\data.yaml --epochs 50
   ```
   That writes `runs/detect/train/weights/best.onnx` — copy it here as
   `buttons.onnx` and create a matching `buttons.names`.

**Option B — a pre-trained UI-element model.** Several public YOLOv8 models are
trained on UI datasets (e.g. RICO-based "screen element" detectors). Export any
of them to ONNX (`yolo export model=... format=onnx`) and drop it here. Quality
varies by how close their training data is to your app.

## Why ONNX (and not a `.pt`)?

ONNX runs on `onnxruntime` — a small CPU dependency with no PyTorch install
needed at run time. You only need the heavy `ultralytics`/PyTorch stack to
*train*, not to *run* macros.
