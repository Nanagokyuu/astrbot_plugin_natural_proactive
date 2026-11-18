"""Test bootstrap.

The plugin normally runs inside AstrBot. When the host package is not
installed (CI, plain checkouts), install a tiny stub so the plugin modules can
be imported and exercised without pulling the full AstrBot runtime.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_natural_proactive"

if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))


def _install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    sys.modules["astrbot"] = astrbot

    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    sys.modules["astrbot.api"] = api
    astrbot.api = api

    class Plain:
        type = "plain"

        def __init__(self, text: str = "") -> None:
            self.text = text

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = Plain
    sys.modules["astrbot.api.message_components"] = components
    api.message_components = components

    class MessageChain:
        def __init__(self, chain=None) -> None:
            self.chain = list(chain or [])
            self.image_path = None

        def file_image(self, path: str):
            self.image_path = path
            return self

    class AstrMessageEvent:
        pass

    class EventMessageType:
        GROUP_MESSAGE = 2
        PRIVATE_MESSAGE = 4
        OTHER_MESSAGE = 8
        ALL = 14

    class PermissionType:
        ADMIN = 1
        MEMBER = 2

    def _register(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    _event_message_type = EventMessageType
    _permission_type = PermissionType

    class _Filter:
        EventMessageType = _event_message_type
        PermissionType = _permission_type
        event_message_type = staticmethod(_register)
        on_decorating_result = staticmethod(_register)
        on_astrbot_loaded = staticmethod(_register)
        on_plugin_loaded = staticmethod(_register)
        command = staticmethod(_register)
        permission_type = staticmethod(_register)

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.MessageChain = MessageChain
    event_module.filter = _Filter
    sys.modules["astrbot.api.event"] = event_module
    api.event = event_module

    class Star:
        def __init__(self, context, config=None) -> None:
            self.context = context
            self.config = config

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str | None = None) -> Path:
            path = Path(tempfile.gettempdir()) / "astrbot_np_test_data"
            path.mkdir(parents=True, exist_ok=True)
            return path

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = Star
    star_module.StarTools = StarTools
    sys.modules["astrbot.api.star"] = star_module
    api.star = star_module


try:
    # Import the submodule we depend on: a stray "astrbot" directory on
    # sys.path would otherwise be mistaken for a real host installation.
    import astrbot.api  # noqa: F401
except Exception:
    for _name in [n for n in sys.modules if n == "astrbot" or n.startswith("astrbot.")]:
        sys.modules.pop(_name, None)
    _install_astrbot_stub()

if PACKAGE_NAME not in sys.modules:
    if REPO_ROOT.name == PACKAGE_NAME:
        importlib.import_module(PACKAGE_NAME)
    else:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            REPO_ROOT / "__init__.py",
            submodule_search_locations=[str(REPO_ROOT)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)
