"""A memoising wrapper around any `LineageSource`.

Every layer above the resolver is a pure function; the resolver is where all the
latency lives. One `resolve_footprint` call already memoises entity fetches
within itself, but that cache dies with the call — and the two places Undertow
does the most work are exactly the ones that call it repeatedly:

  * `check --all` resolves each model in the inventory independently. Twenty
    models sharing a staging layer fetch that staging layer twenty times.
  * Every check resolves the current footprint, and the demo resolves a second
    one for the baseline state.

Wrapping the source instead of the traversal means the cache spans every call
made against that source, without `resolve_footprint` having to grow a cache
parameter that every caller then has to remember to thread through.

**Lifetime is one command run**, because that is the longest window in which the
catalog can be assumed not to have changed underneath us. The wrapper is created
alongside the connection and dies with it; nothing is persisted between
invocations. A gate that answered from yesterday's cache would be a much worse
bug than a slow one.

Negative results are cached too. "This URN resolves to nothing" is an answer,
and re-asking DataHub for it on every model in a sweep costs exactly as much as
asking for one that exists.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from undertow.resolver.base import (
    LineageEdge,
    LineageNode,
    LineageSource,
    SchemaFieldInfo,
)

# Methods that exist on some sources and not others, and whose *absence* is
# meaningful: the traversal probes for them with `getattr` and changes behaviour
# when they are missing. `get_column_lineage` is the sharp one — only the MCP
# source has it, and a wrapper that always exposed it would make every other
# source claim column-level lineage it cannot resolve, which is worse than the
# thing this module was written to fix.
_OPTIONAL_METHODS = frozenset({"get_latest_profile", "get_column_lineage"})


class CachingLineageSource:
    """Answer each distinct read once, then serve it from memory.

    Wraps rather than subclasses, so it works identically over the MCP source,
    the SDK source, and a recording, and so a source gaining a new method does
    not silently bypass the cache — it goes through `__getattr__`, which either
    memoises it or refuses to claim it exists.
    """

    def __init__(self, inner: LineageSource) -> None:
        self._inner = inner
        self._entities: dict[str, LineageNode | None] = {}
        self._lineage: dict[tuple[str, str, int], list[LineageEdge]] = {}
        self._fields: dict[str, list[SchemaFieldInfo]] = {}
        self._optional: dict[str, dict[tuple[Any, ...], Any]] = {}
        self.hits = 0
        self.misses = 0

    # -- passthrough state -------------------------------------------------
    #
    # `_fail_if_unreachable` reads this off the source by name. A wrapper that
    # swallowed it would turn "cannot reach DataHub" into a silent empty walk,
    # which is the exact failure the whole gate is built to refuse.
    @property
    def connection_error(self) -> str | None:
        error: str | None = getattr(self._inner, "connection_error", None)
        return error

    # -- cached reads ------------------------------------------------------

    def get_entity(self, urn: str) -> LineageNode | None:
        if urn in self._entities:
            self.hits += 1
            return self._entities[urn]
        self.misses += 1
        node = self._inner.get_entity(urn)
        self._entities[urn] = node
        return node

    def get_entities(self, urns: list[str]) -> dict[str, LineageNode]:
        """Batch fetch, but only for what is not already known.

        Answering a batch entirely from cache is worth the bookkeeping: it is
        the shape `check --all` produces once the first model has warmed it.
        """
        missing = [u for u in urns if u not in self._entities]
        self.hits += len(urns) - len(missing)

        if missing:
            self.misses += len(missing)
            fetched = self._inner.get_entities(missing)
            for urn in missing:
                # Absent from the response means absent from DataHub. Recording
                # that as None stops the next caller re-asking for it.
                self._entities[urn] = fetched.get(urn)

        found: dict[str, LineageNode] = {}
        for urn in urns:
            node = self._entities.get(urn)
            if node is not None:
                found[urn] = node
        return found

    def get_lineage(
        self, urn: str, direction: str = "UPSTREAM", hops: int = 1
    ) -> list[LineageEdge]:
        # Direction is normalised because callers spell it inconsistently and
        # `UPSTREAM`/`upstream` must not occupy two cache slots.
        key = (urn, direction.upper(), hops)
        if key in self._lineage:
            self.hits += 1
            return self._lineage[key]
        self.misses += 1
        edges = self._inner.get_lineage(urn, direction=direction, hops=hops)
        self._lineage[key] = edges
        return edges

    def list_schema_fields(self, urn: str) -> list[SchemaFieldInfo]:
        if urn in self._fields:
            self.hits += 1
            return self._fields[urn]
        self.misses += 1
        fields = self._inner.list_schema_fields(urn)
        self._fields[urn] = fields
        return fields

    # -- optional capabilities --------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Memoise the optional methods, and only when the inner source has them.

        Reached only for attributes normal lookup did not find, so it never
        shadows the cached methods above. Raising `AttributeError` for a
        capability the inner source lacks is load-bearing rather than
        incidental: `getattr(source, "get_column_lineage", None)` in the
        traversal must still come back None for an SDK source, or it will
        believe column-level lineage is available and resolve nothing.
        """
        # Guards recursion during __init__, before _inner is bound.
        if name.startswith("_") or name not in _OPTIONAL_METHODS:
            raise AttributeError(name)

        inner_fn: Callable[..., Any] | None = getattr(self._inner, name, None)
        if not callable(inner_fn):
            raise AttributeError(name)

        cache = self._optional.setdefault(name, {})

        def call(*args: Any) -> Any:
            if args in cache:
                self.hits += 1
                return cache[args]
            self.misses += 1
            result = inner_fn(*args)
            cache[args] = result
            return result

        return call

    # -- diagnostics -------------------------------------------------------

    def stats(self) -> str:
        total = self.hits + self.misses
        if not total:
            return "no lineage reads"
        return f"{total} lineage read(s), {self.hits} served from cache"


__all__ = ["CachingLineageSource"]
