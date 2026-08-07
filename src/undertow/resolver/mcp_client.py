"""A real MCP client for the DataHub MCP server.

Undertow's traversal is synchronous and the MCP Python SDK is async, so this
module owns the bridge: a background thread running an event loop that keeps one
stdio subprocess and one initialised `ClientSession` alive for the whole run.
The alternative — spawning a server per call — costs a process launch and a
DataHub handshake on every hop of the graph walk.

Two rules govern everything here:

1. **Never return empty on failure.** A tool call that errors, times out, or
   comes back in an unrecognised shape raises. Undertow's resolver treats an
   empty result as "nothing upstream changed", so a silent `[]` from this layer
   would surface as a CLEAR verdict produced by a broken connection.
2. **Only call tools the server actually advertises.** `available_tools` is
   populated from `tools/list` at connect time and checked before dispatch, so a
   signature drift in the server fails with a clear message instead of a
   confusing argument error deep in a traversal.

Verified against mcp-server-datahub 0.6.0, whose OSS build exposes eight
read-only tools. Mutation, data-quality, and user tools are gated off in OSS —
which is why verdict write-back goes through the REST emitter, not through here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from contextlib import AsyncExitStack, suppress
from typing import Any

DEFAULT_TIMEOUT_SEC = 60.0
# Startup is bounded tighter than a tool call. A misconfigured server dies
# almost immediately, and the stdio transport does not surface that as an error —
# it simply stops talking. Without a separate, shorter budget, a crashed
# subprocess costs a full call timeout of silence before anyone is told why.
#
# Not *too* tight, though. The server imports the DataHub SDK and fastmcp before
# it answers, and on a cold filesystem cache that has been observed to take
# longer than 20s — which surfaced as "cannot reach DataHub" on a run where
# DataHub was fine. A spurious startup failure reads as a broken integration,
# so the budget is set well above the observed worst case rather than near it.
# Override with UNDERTOW_MCP_STARTUP_TIMEOUT for slower machines.
DEFAULT_STARTUP_TIMEOUT_SEC = float(os.environ.get("UNDERTOW_MCP_STARTUP_TIMEOUT", "60"))

# The OSS tool surface, as reported by tools/list on mcp-server-datahub 0.6.0
# against DataHub OSS v1.7.0. Kept here so a drift between what Undertow expects
# and what the server offers is reported as a named mismatch rather than a
# runtime TypeError.
#
# Six, not eight. `search_documents` and `grep_documents` are documented but do
# not appear on this build's tools/list, and calling one fails. Anything that
# offers tools to a model has to intersect its list with `available_tools` from
# the live handshake rather than trusting this constant — see the investigator.
OSS_TOOLS: frozenset[str] = frozenset(
    {
        "get_entities",
        "get_lineage",
        "get_lineage_paths_between",
        "list_schema_fields",
        "search",
        "get_dataset_queries",
    }
)

# Present in some builds and documented, but absent from OSS v1.7.0's handshake.
# Named rather than deleted so their absence stays a known fact.
OPTIONAL_TOOLS: frozenset[str] = frozenset({"search_documents", "grep_documents"})


class McpError(RuntimeError):
    """A tool call failed, timed out, or returned an unusable payload."""


class McpToolExecutor:
    """Callable `(tool_name, arguments) -> parsed result`, backed by a live server.

    Use as a context manager so the subprocess is torn down deterministically:

        with McpToolExecutor() as call:
            source = McpLineageSource(call)
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SEC,
    ) -> None:
        # `sys.executable -m mcp_server_datahub` rather than a bare `mcp-server-datahub`
        # console script: it works from a venv, a Store Python, and CI without
        # depending on the script directory being on PATH.
        self.command = command or sys.executable
        default_args = ["-m", "mcp_server_datahub", "--transport", "stdio"]
        self.args = args if args is not None else default_args
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.env = self._build_env(env)

        self.available_tools: frozenset[str] = frozenset()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._stderr_file: Any = None

    @staticmethod
    def _build_env(overrides: dict[str, str] | None) -> dict[str, str]:
        """Inherit the parent environment, then apply explicit overrides.

        The server calls `DataHubClient.from_env()`, so `DATAHUB_GMS_URL` and
        `DATAHUB_GMS_TOKEN` have to survive into the subprocess. Inheriting
        wholesale also preserves the `~/.datahubenv` fallback for local runs.
        """
        env = dict(os.environ)
        if overrides:
            env.update(overrides)
        return env

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> McpToolExecutor:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        if self._thread is not None:
            return

        self._preflight()

        # The server logs its GraphQL traffic at debug level, and `stdio_client`
        # sends the child's stderr straight to ours by default. On a healthy run
        # that buries the verdict under a query dump; on a failed start it is the
        # only explanation available. So it goes to a temp file: silent when
        # things work, quoted back in the error when they don't.
        #
        # A real file rather than a StringIO because the child needs a genuine
        # file descriptor to inherit.
        #
        # Deliberately not a context manager (SIM115): the handle outlives this
        # function by design — the subprocess writes to it for the whole session
        # and `close()` owns its teardown.
        self._stderr_file = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8", errors="replace"
        )

        ready = threading.Event()
        error: list[BaseException] = []

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._connect())
            except BaseException as exc:  # noqa: BLE001 - surfaced to the caller below
                error.append(exc)
                ready.set()
                return
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run_loop, name="undertow-mcp", daemon=True)
        self._thread.start()

        if not ready.wait(timeout=self.startup_timeout):
            raise McpError(
                f"Timed out after {self.startup_timeout}s starting the DataHub MCP server "
                f"({self.command} {' '.join(self.args)}). The server usually exits this way "
                "when it cannot reach DataHub — check DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN."
                + self._captured_stderr()
            )
        if error:
            raise McpError(
                f"Could not start the DataHub MCP server: {type(error[0]).__name__}: {error[0]}"
                + self._captured_stderr()
            ) from error[0]

    def _preflight(self) -> None:
        """Fail on a missing package before paying the startup timeout for it.

        Without this, a missing `mcp-server-datahub` presents as `python -m
        mcp_server_datahub` exiting instantly, the session never initialising,
        and a 20-second wait ending in "timed out starting the server" — which
        reads as "DataHub is unreachable" and sends the reader to check a URL
        that was never the problem. The install is the problem; say so.

        Only the default invocation is checked. A caller who passed an explicit
        `command`/`args` is pointing at a server we cannot introspect, and
        guessing at its packaging would be worse than letting it speak for itself.
        """
        if self.command != sys.executable or self.args[:2] != ["-m", "mcp_server_datahub"]:
            return

        import importlib.util

        for module, package in (("mcp", "mcp"), ("mcp_server_datahub", "mcp-server-datahub")):
            if importlib.util.find_spec(module) is None:
                raise McpError(
                    f"`{package}` is not installed, so the MCP read path is unavailable. "
                    'Install it with `pip install -e ".[mcp]"`, or drop `--mcp` to resolve '
                    "through the Python SDK instead — both paths produce the same verdict."
                )

    def _captured_stderr(self, max_lines: int = 15) -> str:
        """The child's last words, for use in a startup failure message.

        Truncated from the end: the traceback that explains the exit is the last
        thing written, and the import warnings above it explain nothing.
        """
        handle = self._stderr_file
        if handle is None:
            return ""
        try:
            handle.flush()
            handle.seek(0)
            lines = [ln.rstrip() for ln in handle.readlines() if ln.strip()]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
            return ""
        if not lines:
            return ""
        tail = lines[-max_lines:]
        elided = (
            ""
            if len(lines) <= max_lines
            else f"  ... {len(lines) - max_lines} earlier lines\n"
        )
        return "\n\nServer output:\n" + elided + "\n".join(f"  {ln}" for ln in tail)

    async def _connect(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - covered by the preflight in start()
            raise McpError(
                "The `mcp` client SDK is not installed. "
                'Install it with `pip install -e ".[mcp]"`.'
            ) from exc

        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=self._stderr_file or sys.stderr)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        listed = await self._session.list_tools()
        self.available_tools = frozenset(t.name for t in listed.tools)

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
            future.result(timeout=self.timeout)
        except Exception:
            # Teardown failures are not worth masking a real verdict; the
            # subprocess is a daemon and dies with the interpreter regardless.
            pass
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            self._loop = None
            self._thread = None
            self._session = None
            if self._stderr_file is not None:
                # Closing a diagnostics temp file must never fail a run.
                with suppress(Exception):
                    self._stderr_file.close()
                self._stderr_file = None

    async def _disconnect(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    # -- dispatch ----------------------------------------------------------

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self._loop is None or self._session is None:
            raise McpError(
                "MCP executor is not connected. Use it as a context manager, or call start()."
            )
        if self.available_tools and tool_name not in self.available_tools:
            raise McpError(
                f"The DataHub MCP server does not expose {tool_name!r}. "
                f"Available: {sorted(self.available_tools)}. "
                "Mutation, data-quality, and user tools are gated off in OSS builds."
            )

        # Drop unset optionals rather than sending explicit nulls — the server's
        # own defaults are better than None for every one of these parameters.
        payload = {k: v for k, v in arguments.items() if v is not None}

        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, payload), self._loop
        )
        try:
            result = future.result(timeout=self.timeout)
        except TimeoutError as exc:
            raise McpError(f"{tool_name} timed out after {self.timeout}s.") from exc
        except Exception as exc:
            raise McpError(f"{tool_name} failed: {type(exc).__name__}: {exc}") from exc

        if getattr(result, "isError", False):
            raise McpError(f"{tool_name} returned an error: {_result_text(result)!r}")

        return _parse_result(result)


def _result_text(result: Any) -> str:
    parts = [
        getattr(block, "text", "")
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "text", None)
    ]
    return "\n".join(parts)


def _parse_result(result: Any) -> Any:
    """Unwrap a CallToolResult into plain Python.

    Prefers `structuredContent` when the server provides it, falls back to
    JSON-decoding the text blocks, and returns raw text only when it is not
    JSON at all. Anything genuinely unusable raises rather than degrading to
    an empty collection.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # FastMCP wraps bare lists under a "result" key to keep the payload an object.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured

    text = _result_text(result)
    if not text:
        raise McpError("Tool returned no content.")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


__all__ = ["McpToolExecutor", "McpError", "OSS_TOOLS", "DEFAULT_TIMEOUT_SEC"]
