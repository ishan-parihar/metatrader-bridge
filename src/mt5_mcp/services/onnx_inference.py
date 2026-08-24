"""ONNX Model Inference Service — loads .onnx files and runs trading predictions.

Auto-discovers models in ~/.MetaTraderBridge/models/ on startup.
Supports both classification (multi-output) and regression (single-output) models.
GPU-accelerated when CUDA is available, falls back to CPU.
"""

from __future__ import annotations

import os
import time
import threading
from pathlib import Path
from typing import Any

import numpy as np

from mt5_mcp.observability.logging import logger


class ONNXInferenceService:
    """Load and run ONNX model inference for trading predictions."""

    def __init__(self, models_dir: str | None = None):
        """Auto-discover .onnx files in models_dir on startup.

        Args:
            models_dir: Directory containing .onnx model files.
                Defaults to ~/.MetaTraderBridge/models/
        """
        if models_dir is None:
            models_dir = str(Path.home() / ".MetaTraderBridge" / "models")
        self._models_dir = models_dir
        self._models: dict[str, str] = {}
        self._sessions: dict[str, Any] = {}
        self._lock = threading.Lock()

        os.makedirs(self._models_dir, exist_ok=True)

        self.reload()

    def _discover_models(self) -> dict[str, str]:
        """Scan models_dir for .onnx files.

        Returns:
            Dict mapping model name (filename without .onnx) to file path.
        """
        discovered: dict[str, str] = {}
        models_path = Path(self._models_dir)
        if not models_path.exists():
            return discovered

        for onnx_file in sorted(models_path.glob("*.onnx")):
            discovered[onnx_file.stem] = str(onnx_file)

        return discovered

    def _load_model(self, model_name: str) -> Any:
        """Load an ONNX model into memory.

        Args:
            model_name: Name of the model (must be in discovered models).

        Returns:
            InferenceSession for the model.

        Raises:
            ValueError: If model file is invalid or cannot be loaded.
        """
        import onnxruntime as ort

        file_path = self._models[model_name]

        if not os.path.isfile(file_path):
            raise ValueError(f"Model file not found: {file_path}")

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        try:
            session = ort.InferenceSession(file_path, providers=providers)
        except Exception as e:
            logger.warning(
                f"CUDA provider failed for {model_name}, falling back to CPU: {e}"
            )
            session = ort.InferenceSession(
                file_path, providers=["CPUExecutionProvider"]
            )

        return session

    def reload(self) -> dict:
        """Reload all models from disk.

        Returns:
            Dict with 'loaded' count and 'failed' list of model names.
        """
        with self._lock:
            discovered = self._discover_models()
            self._models = discovered
            self._sessions.clear()

            loaded = 0
            failed: list[str] = []

            for name, path in discovered.items():
                try:
                    self._sessions[name] = self._load_model(name)
                    loaded += 1
                    logger.info(f"Loaded ONNX model: {name} ({path})")
                except Exception as e:
                    failed.append(name)
                    logger.error(f"Failed to load ONNX model {name}: {e}")

            return {"loaded": loaded, "failed": failed}

    def predict(
        self,
        model_name: str,
        features: list[float],
        feature_names: list[str] | None = None,
    ) -> dict:
        """Run inference on a feature vector.

        Args:
            model_name: Name of the model (filename without .onnx).
            features: Input feature vector.
            feature_names: Optional names for input validation.

        Returns:
            Dict with prediction, confidence, raw output, inference time, and model info.

        Raises:
            ValueError: If model not found, input shape mismatch, or invalid ONNX file.
        """
        with self._lock:
            if model_name not in self._sessions:
                if model_name in self._models:
                    try:
                        self._sessions[model_name] = self._load_model(model_name)
                    except Exception as e:
                        raise ValueError(f"Failed to load model '{model_name}': {e}")
                else:
                    available = list(self._models.keys())
                    raise ValueError(
                        f"Model '{model_name}' not found. Available models: {available}"
                    )

            session = self._sessions[model_name]

        inputs = session.get_inputs()
        outputs = session.get_outputs()

        input_shape = inputs[0].shape
        output_shape = outputs[0].shape
        input_names = [inp.name for inp in inputs]
        output_names = [out.name for out in outputs]

        expected_features = None
        for dim in input_shape:
            if isinstance(dim, int) and dim > 0:
                if expected_features is None and dim == 1:
                    continue
                expected_features = dim
                break

        if expected_features is None:
            for dim in input_shape:
                if isinstance(dim, int):
                    expected_features = dim
                    break

        if expected_features is not None and len(features) != expected_features:
            raise ValueError(
                f"Input shape mismatch for model '{model_name}': "
                f"expected {expected_features} features, got {len(features)}"
            )

        if feature_names and len(feature_names) != len(features):
            raise ValueError(
                f"Feature names length ({len(feature_names)}) does not match "
                f"features length ({len(features)})"
            )

        input_name = inputs[0].name
        features_array = np.array([features], dtype=np.float32)

        start_time = time.perf_counter()
        raw_outputs = session.run(output_names, {input_name: features_array})
        end_time = time.perf_counter()

        inference_time_ms = round((end_time - start_time) * 1000, 2)

        output_data = raw_outputs[0]
        output_last_dim = output_data.shape[-1] if output_data.ndim > 0 else 1

        if output_last_dim > 1:
            raw_output = output_data[0].tolist()

            probabilities = np.array(raw_output, dtype=np.float64)
            prob_sum = probabilities.sum()
            if abs(prob_sum - 1.0) > 0.01:
                exp_vals = np.exp(probabilities - np.max(probabilities))
                probabilities = exp_vals / exp_vals.sum()
                raw_output = probabilities.tolist()

            prediction_idx = int(np.argmax(probabilities))
            confidence = float(probabilities[prediction_idx])

            if output_last_dim == 2:
                prediction = "up" if prediction_idx == 0 else "down"
            elif output_last_dim == 3:
                prediction = ["up", "neutral", "down"][prediction_idx]
            else:
                prediction = f"class_{prediction_idx}"

            return {
                "model_name": model_name,
                "prediction": prediction,
                "confidence": round(confidence, 4),
                "raw_output": [round(v, 4) for v in raw_output],
                "inference_time_ms": inference_time_ms,
                "model_info": {
                    "input_shape": list(input_shape),
                    "output_shape": list(output_shape),
                    "input_names": input_names,
                    "output_names": output_names,
                },
            }
        else:
            raw_value = float(output_data[0][0])

            return {
                "model_name": model_name,
                "prediction": raw_value,
                "confidence": None,
                "raw_output": [round(raw_value, 4)],
                "inference_time_ms": inference_time_ms,
                "model_info": {
                    "input_shape": list(input_shape),
                    "output_shape": list(output_shape),
                    "input_names": input_names,
                    "output_names": output_names,
                },
            }

    def list_models(self) -> dict:
        """List all loaded models with their metadata.

        Returns:
            Dict with 'models' key containing per-model metadata.
        """
        with self._lock:
            models_info = {}

            for name, file_path in self._models.items():
                info: dict[str, Any] = {
                    "file": file_path,
                    "loaded": name in self._sessions,
                }

                if name in self._sessions:
                    session = self._sessions[name]
                    inputs = session.get_inputs()
                    outputs = session.get_outputs()
                    info["input_shape"] = list(inputs[0].shape)
                    info["output_shape"] = list(outputs[0].shape)
                else:
                    info["input_shape"] = None
                    info["output_shape"] = None

                models_info[name] = info

            return {"models": models_info}
