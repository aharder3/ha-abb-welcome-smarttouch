"""Behavior tests for the intercom ``EventEntity``.

``ABBWelcomeEventEntity`` must write state after every new portal event (Home
Assistant's ``_trigger_event`` does not), fire them oldest first, unregister
its coordinator listener on removal, and not replay the cached history when
the entity is (re)added after a restart.

Home Assistant is stubbed so the test runs without it installed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

_FILE = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "abb_welcome" / "event.py"
)


@dataclass
class _Event:
    event_id: str
    event_type: str = "ring"
    station_id: str = ""
    local_id: str = ""
    local_name: str = ""
    sender: str = ""
    belongs_to: str = ""
    timestamp: str = ""


@dataclass
class _Data:
    events: list[_Event] = field(default_factory=list)


class _FakeEventEntity:
    # The integration entity does not call super().__init__(), like HA's
    # Entity, so the recorders live on the instance lazily.
    @property
    def triggered(self) -> list[str]:
        return self.__dict__.setdefault("_triggered", [])

    @property
    def writes(self) -> list[str]:
        return self.__dict__.setdefault("_writes", [])

    @property
    def _on_remove(self) -> list:
        return self.__dict__.setdefault("_removers", [])

    def _trigger_event(self, event_type, event_attributes=None) -> None:
        self.triggered.append(event_attributes["event_id"])

    def async_write_ha_state(self) -> None:
        self.writes.append(self.triggered[-1] if self.triggered else "")

    def async_on_remove(self, func) -> None:
        self._on_remove.append(func)

    def remove(self) -> None:
        for func in self._on_remove:
            func()


def _load_event_module():
    pkg = "abb_event_test_pkg"
    stubs = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.event": types.SimpleNamespace(
            EventEntity=_FakeEventEntity
        ),
        "homeassistant.config_entries": types.SimpleNamespace(ConfigEntry=object),
        "homeassistant.core": types.SimpleNamespace(
            HomeAssistant=object, callback=lambda f: f
        ),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": types.SimpleNamespace(
            DeviceInfo=dict
        ),
        "homeassistant.helpers.entity_platform": types.SimpleNamespace(
            AddEntitiesCallback=object
        ),
        pkg: types.ModuleType(pkg),
        f"{pkg}.const": types.SimpleNamespace(DOMAIN="abb_welcome"),
        f"{pkg}.coordinator": types.SimpleNamespace(
            ABBWelcomeCoordinator=object, ABBWelcomeData=_Data
        ),
    }
    stubs[pkg].__path__ = []
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(f"{pkg}.event", _FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return module


event = _load_event_module()


class _FakeCoordinator:
    def __init__(self, data: _Data | None) -> None:
        self.data = data
        self.listeners: list = []

    def async_add_listener(self, func):
        self.listeners.append(func)
        return lambda: self.listeners.remove(func)

    def push(self, *events: _Event) -> None:
        """Prepend events (given newest first) like a poll, then notify."""
        self.data.events = list(events) + self.data.events
        for func in list(self.listeners):
            func()


def _entity(coordinator: _FakeCoordinator):
    ent = event.ABBWelcomeEventEntity(coordinator, "gw", [])
    asyncio.run(ent.async_added_to_hass())
    return ent


def test_cached_history_is_not_replayed_on_add() -> None:
    coord = _FakeCoordinator(_Data([_Event("h2"), _Event("h1")]))
    ent = _entity(coord)

    coord.push()  # a poll with nothing new
    assert ent.triggered == []

    coord.push(_Event("n1"))
    assert ent.triggered == ["n1"]
    assert ent.writes == ["n1"]


def test_first_data_after_empty_start_is_baseline() -> None:
    coord = _FakeCoordinator(_Data())
    ent = _entity(coord)

    coord.push(_Event("h2"), _Event("h1"))  # initial history poll
    assert ent.triggered == []

    coord.push(_Event("n1"))
    assert ent.triggered == ["n1"]


def test_each_new_event_is_written_oldest_first() -> None:
    coord = _FakeCoordinator(_Data([_Event("h1")]))
    ent = _entity(coord)

    coord.push(
        _Event("n3", "call-missed"),
        _Event("s1", "screenshot"),
        _Event("n1", "ring"),
    )
    assert ent.triggered == ["n1", "n3"]
    assert ent.writes == ["n1", "n3"]


def test_listener_is_removed_with_entity() -> None:
    coord = _FakeCoordinator(_Data([_Event("h1")]))
    ent = _entity(coord)
    assert len(coord.listeners) == 1

    ent.remove()
    assert coord.listeners == []
