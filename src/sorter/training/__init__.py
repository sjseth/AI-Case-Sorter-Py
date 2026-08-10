"""ConvNeXt training subsystem.

`manager.py` spawns `train_convnext.py` as a subprocess and parses its
JSON-line progress markers. Training is intentionally out-of-process so a
long run can be canceled cleanly via SIGTERM rather than fighting Python's
GIL to cancel a PyTorch loop running in a thread.
"""
