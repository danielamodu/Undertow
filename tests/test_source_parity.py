"""The two lineage sources must offer the same surface.

The README's claim is "both paths resolve the same graph and produce the same
verdict". That was false for a while and nothing noticed: `get_latest_profile`
was added to the SDK source only, so `--mcp` resolved every asset unprofiled and
returned CLEAR on a graph where the SDK path returned WARN with three findings.

Nothing failed, because the traversal reaches that method through `getattr` —
absent means "no statistics available", which is a legitimate state for a source
that genuinely cannot read them, and indistinguishable from a source that simply
forgot to implement it.

These tests close that gap structurally rather than by remembering.
"""

from __future__ import annotations

import inspect

import pytest

from undertow.resolver import McpLineageSource, SdkLineageSource
from undertow.resolver.base import LineageSource

SOURCES = [SdkLineageSource, McpLineageSource]

# Reached through `getattr` rather than the Protocol, so a missing one degrades
# silently instead of raising. Each entry is a capability the traversal will use
# if present — which is exactly why every source has to be explicit about it.
DUCK_TYPED_CAPABILITIES = ["get_latest_profile"]


def protocol_methods() -> list[str]:
    return [
        name
        for name, member in inspect.getmembers(LineageSource, inspect.isfunction)
        if not name.startswith("_")
    ]


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.__name__)
@pytest.mark.parametrize("method", protocol_methods())
def test_every_source_implements_the_protocol(source: type, method: str) -> None:
    assert callable(getattr(source, method, None)), f"{source.__name__} is missing {method}"


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.__name__)
@pytest.mark.parametrize("method", DUCK_TYPED_CAPABILITIES)
def test_every_source_implements_the_duck_typed_capabilities(
    source: type, method: str
) -> None:
    """Absence here is silent, not loud. That is the whole problem."""
    assert callable(getattr(source, method, None)), (
        f"{source.__name__} is missing {method}. The traversal reaches it via "
        "getattr, so omitting it does not raise — it just resolves every asset "
        "unprofiled and disagrees with the other source."
    )


@pytest.mark.parametrize("method", DUCK_TYPED_CAPABILITIES + protocol_methods())
def test_the_two_sources_agree_on_arity(method: str) -> None:
    """A method present on both but taking different arguments is the same bug
    wearing a different hat: the traversal calls one shape."""
    sdk = inspect.signature(getattr(SdkLineageSource, method))
    mcp = inspect.signature(getattr(McpLineageSource, method))

    def required(sig: inspect.Signature) -> list[str]:
        return [
            name
            for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty
        ]

    assert required(sdk) == required(mcp), (
        f"{method} takes {required(sdk)} on the SDK source and {required(mcp)} on the "
        "MCP source; the traversal calls one of those."
    )


def test_a_source_without_statistics_reports_none_rather_than_empty() -> None:
    """`None` means cannot assess. An empty profile would mean assessed, clean.

    Coverage reads these differently, and conflating them turns "we could not
    look" into "we looked and it was fine".
    """
    source = McpLineageSource(lambda name, args: None, profile_reader=None)

    assert source.get_latest_profile("urn:li:dataset:(urn:li:dataPlatform:x,y,PROD)") is None


def test_the_mcp_source_uses_a_reader_when_given_one() -> None:
    class Reader:
        def get_latest_profile(self, urn: str) -> dict[str, object]:
            return {"rowCount": 10, "fieldProfiles": []}

    source = McpLineageSource(lambda name, args: None, profile_reader=Reader())

    assert source.get_latest_profile("urn:li:dataset:(urn:li:dataPlatform:x,y,PROD)") == {
        "rowCount": 10,
        "fieldProfiles": [],
    }
