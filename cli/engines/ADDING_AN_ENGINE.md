1. Create `cli/engines/<name>.py` implementing `DetectionEngine`
2. `load()` must be idempotent — safe to call multiple times
3. `detect()` must return `list[DetectedElement]` with valid `bbox` in absolute pixels
4. `element_id` must be stable for the same image + same model weights (use a hash of bbox + label if the model doesn't provide one)
5. `raw` may contain anything but must be JSON-serializable
6. Register in `cli/engines/registry.py`: one line
7. Run `pytest tests/test_engine_contract.py --engine <name>` to validate the contract
