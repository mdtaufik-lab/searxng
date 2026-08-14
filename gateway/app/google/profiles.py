from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    categories: tuple[str, ...]
    engines: tuple[str, ...]


class Profiles:
    def __init__(self, profiles: dict[str, Profile], aliases: dict[str, str], default: str) -> None:
        self._p = profiles
        self._aliases = aliases
        self._default = default

    @classmethod
    def load(cls, path: str | Path) -> "Profiles":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        profiles = {}
        for name, spec in (raw.get("profiles") or {}).items():
            # An explicit `categories: []` means "no category" (so only the named
            # engines are searched). Only a MISSING key defaults to general.
            cats = spec.get("categories")
            categories = tuple(cats) if cats is not None else ("general",)
            profiles[name] = Profile(
                name=name,
                title=spec.get("title", name),
                categories=categories,
                engines=tuple(spec.get("engines") or ()),
            )
        return cls(
            profiles=profiles,
            aliases={str(k): v for k, v in (raw.get("aliases") or {}).items()},
            default=raw.get("default_cx", "web"),
        )

    def resolve(self, cx: str | None) -> Profile | None:
        """None means the cx is unknown -- the caller decides whether that is a
        400 or a silent fall back to the default."""
        if not cx:
            return self._p.get(self._default)
        return self._p.get(self._aliases.get(cx, cx))

    def default(self) -> Profile:
        return self._p[self._default]
