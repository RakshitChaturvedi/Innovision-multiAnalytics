"""Fake models for service startup smoke tests. TEST-ONLY.

Python imports `sitecustomize` automatically when this folder is on
PYTHONPATH; the smoke tests add it there so they can run the real command
`python -m services.<svc>.main` without weights, torch or insightface.
Does nothing unless INNOVISION_FAKE_MODELS=1, so it can never affect a real run.
"""
import os

if os.environ.get("INNOVISION_FAKE_MODELS") == "1":
    import importlib.abc
    import importlib.machinery
    import logging
    import sys
    import types

    import numpy as np

    class _FakeYOLO:
        def __init__(self, *args, **kwargs):
            pass

        def to(self, device):
            return self

        def __call__(self, frames, **kwargs):
            return []

    class _FakeBYTETracker:
        def __init__(self, args, frame_rate=30):
            pass

        def update(self, results, img=None):
            return np.zeros((0, 7), np.float32)

    class _FakeFaceAnalysis:
        def __init__(self, *args, **kwargs):
            pass

        def prepare(self, *args, **kwargs):
            pass

        def get(self, img):
            return []

    class _FakeFace(dict):
        pass

    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = _FakeYOLO
    trackers = types.ModuleType("ultralytics.trackers")
    trackers.BYTETracker = _FakeBYTETracker
    ultralytics.trackers = trackers
    insightface = types.ModuleType("insightface")
    app = types.ModuleType("insightface.app")
    app.FaceAnalysis = _FakeFaceAnalysis
    common = types.ModuleType("insightface.app.common")
    common.Face = _FakeFace
    app.common = common
    insightface.app = app
    sys.modules.update({
        "ultralytics": ultralytics, "ultralytics.trackers": trackers,
        "insightface": insightface, "insightface.app": app, "insightface.app.common": common,
    })

    def _patch_recognition(module):
        # No pack folder in tests: skip the on-disk check, keep the rest of preload.
        module.check_model_pack = lambda root, pack: module.model_pack_path(root, pack)

    _PATCHES = {"services.recognition.src.model_loader": _patch_recognition}

    class _PatchAfterImport(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            patch = _PATCHES.get(name)
            if patch is None:
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None:
                return spec
            exec_module = spec.loader.exec_module

            def patched(module):
                exec_module(module)
                patch(module)

            spec.loader.exec_module = patched
            return spec

    sys.meta_path.insert(0, _PatchAfterImport())
    logging.getLogger("innovision.fake_models").warning("INNOVISION_FAKE_MODELS=1: fake models active")
