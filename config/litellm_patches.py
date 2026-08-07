"""Startup patch: make LiteLLM's streaming model serialization self-healing.

WHY THIS EXISTS
---------------
litellm 1.95.0 on pydantic 2.13.4 leaves ModelResponseStream (and friends) with
__pydantic_complete__ = False and a MockValSer placeholder instead of a real
SchemaSerializer. MockValSer is supposed to rebuild itself on first use; inside
the proxy that lazy rebuild does not happen, and this line in
litellm/litellm_core_utils/streaming_handler.py:

    obj_dict = processed_chunk.model_dump()

raises:

    TypeError: 'MockValSer' object is not an instance of 'SchemaSerializer'

Symptom: any request that streams AND passes `tools` ends with an HTTP 500 in
the SSE body instead of a clean `[DONE]`. Non-streaming tool calls are fine and
streaming without tools is fine, so it only breaks agentic clients -- which are
the entire point of this gateway. Cline, Copilot agent mode and Claude Code all
send stream+tools together.

WHY A ONE-SHOT REBUILD AT STARTUP IS NOT ENOUGH
-----------------------------------------------
Rebuilding the classes at import time does work -- the serializers become real
SchemaSerializers and the patch logs success -- but by the time a request is
served they are MockValSer again. Something in the proxy's later startup path
re-mocks them. Rather than depend on import ordering we do not control, this
wraps ModelResponseBase.model_dump so the class is repaired at the moment of
use, which is correct regardless of when the mock reappears.

The wrapper costs one attribute lookup per chunk on the happy path.

REMOVAL CONDITION
-----------------
A workaround for a version-pairing bug, not something to keep forever. When
bumping the pinned image digest in docker-compose.yml:
  1. Comment out `callbacks` in litellm.config.yaml and restart.
  2. Run scripts/smoke_test.py -- it covers stream+tools as of this change.
  3. If it passes, delete this file and its volume mount. If not, keep it and
     update the version numbers above.
"""

import logging

log = logging.getLogger(__name__)

_SCHEMA_SER = "SchemaSerializer"


def _is_mocked(cls) -> bool:
    ser = getattr(cls, "__pydantic_serializer__", None)
    return ser is not None and type(ser).__name__ != _SCHEMA_SER


def _ensure_real_serializer(cls) -> bool:
    """Force-rebuild `cls` if its pydantic serializer is still a mock."""
    if not _is_mocked(cls):
        return True
    try:
        cls.model_rebuild(force=True)
    except Exception as exc:  # pragma: no cover - diagnostic path only
        log.error("[sap-gateway] model_rebuild(%s) failed: %s", cls.__name__, exc)
        return False
    return not _is_mocked(cls)


# The offender in practice is openai.types.chat.chat_completion_chunk.
# ChoiceLogprobs -- the SAP provider emits `logprobs: {}` on tool-call chunks,
# and that class lives in the `openai` package, not `litellm`. Sweeping only
# `litellm.*` misses it, which is what made this bug so hard to place.
_SWEEP_PREFIXES = ("litellm", "openai")

_swept_once = False


def _repair_all_litellm_models(force: bool = False) -> int:
    """Rebuild every mocked pydantic model reachable under `litellm.*`.

    Repairing only `type(self)` is not enough: pydantic's `to_python` recurses
    into nested models, so a mocked *nested* class raises from inside the outer
    serializer call while the traceback still points at the outer model_dump.
    Sweep the namespace instead of guessing which nested class it was.

    The startup sweep cannot be the only one: litellm imports provider modules
    lazily, so the SAP models are not in sys.modules yet when the proxy boots.
    `force=True` re-sweeps from the failure path to catch them.
    """
    global _swept_once
    if _swept_once and not force:
        return 0
    _swept_once = True

    import sys

    try:
        from pydantic import BaseModel
    except Exception:  # pragma: no cover
        return 0

    fixed, failed, seen = [], [], set()
    for name, mod in list(sys.modules.items()):
        if mod is None or not any(name == p or name.startswith(p + ".")
                                  for p in _SWEEP_PREFIXES):
            continue
        try:
            members = list(vars(mod).values())
        except Exception:  # pragma: no cover
            continue
        for attr in members:
            if not isinstance(attr, type) or not issubclass(attr, BaseModel):
                continue
            # pydantic's own BaseModel is abstract and legitimately carries a
            # mock serializer; model_rebuild() on it raises. Skip it.
            if attr is BaseModel or attr in seen or not _is_mocked(attr):
                continue
            seen.add(attr)
            (fixed if _ensure_real_serializer(attr) else failed).append(attr.__name__)

    log.warning("[sap-gateway] swept litellm models: repaired=%d failed=%d%s",
                len(fixed), len(failed),
                (" " + ", ".join(sorted(set(failed))[:8])) if failed else "")
    return len(fixed)


def _diagnose_mocked_graph(root, _depth: int = 0, _path: str = "root",
                           _seen=None) -> None:
    """Walk the live object graph and name every value whose class is mocked.

    The sweep only reaches classes exposed as module attributes. If the mocked
    class is defined locally, aliased, or lives outside `litellm.*`, this is
    what identifies it.
    """
    from pydantic import BaseModel

    if _seen is None:
        _seen = set()
    if _depth > 6 or id(root) in _seen:
        return
    _seen.add(id(root))

    if isinstance(root, BaseModel):
        cls = type(root)
        if _is_mocked(cls):
            log.error("[sap-gateway] MOCKED at %s -> %s.%s (repairable=%s)",
                      _path, cls.__module__, cls.__qualname__,
                      _ensure_real_serializer(cls))
        for fname in getattr(cls, "model_fields", {}):
            try:
                _diagnose_mocked_graph(getattr(root, fname), _depth + 1,
                                       f"{_path}.{fname}", _seen)
            except Exception:
                pass
        for k, v in (getattr(root, "__pydantic_extra__", None) or {}).items():
            _diagnose_mocked_graph(v, _depth + 1, f"{_path}[{k}]", _seen)
    elif isinstance(root, dict):
        for k, v in list(root.items())[:20]:
            _diagnose_mocked_graph(v, _depth + 1, f"{_path}[{k!r}]", _seen)
    elif isinstance(root, (list, tuple)):
        for i, v in enumerate(list(root)[:20]):
            _diagnose_mocked_graph(v, _depth + 1, f"{_path}[{i}]", _seen)


def _install_model_dump_guard() -> None:
    from litellm.types.utils import ModelResponseBase

    if getattr(ModelResponseBase, "_sap_gateway_patched", False):
        return

    original = ModelResponseBase.model_dump

    def model_dump(self, **kwargs):
        cls = type(self)
        if _is_mocked(cls):
            _ensure_real_serializer(cls)
        try:
            return original(self, **kwargs)
        except TypeError as exc:
            # The mock is on some nested model, not on cls. Sweep everything
            # once, then retry.
            if "MockValSer" not in str(exc):
                raise
            log.warning("[sap-gateway] MockValSer hit while dumping %s; sweeping",
                        cls.__name__)
            # force=True: provider modules are imported lazily, so the startup
            # sweep will not have seen them.
            _repair_all_litellm_models(force=True)
            _diagnose_mocked_graph(self)
            _ensure_real_serializer(cls)
            return original(self, **kwargs)

    ModelResponseBase.model_dump = model_dump
    ModelResponseBase._sap_gateway_patched = True
    log.warning("[sap-gateway] installed self-healing model_dump on ModelResponseBase")


_install_model_dump_guard()

# Do the sweep once up front too. If it holds, no request ever pays for the
# lazy path above; if something re-mocks a class later, the guard still saves it.
_repair_all_litellm_models()


# LiteLLM's `callbacks:` setting expects a CustomLogger instance to import. The
# patch above already ran at import time; this is a deliberate no-op that exists
# only to give the config something valid to point at.
try:
    from litellm.integrations.custom_logger import CustomLogger

    class _NoopLogger(CustomLogger):
        pass

    patch_handler = _NoopLogger()
except Exception:  # pragma: no cover
    patch_handler = None
