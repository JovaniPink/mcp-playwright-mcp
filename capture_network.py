#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Navigate with Playwright MCP → fetch browser_network_requests → persist JSONL via Filesystem MCP
using the official Python MCP SDK.

Prerequisite:
    # https://github.com/microsoft/playwright-mcp
    - Playwright MCP (SSE):
        npx @playwright/mcp@latest --port 8931 \
            --block-service-workers --image-responses=omit \
            --allowed-origins "https://yourapp.com;https://api.yourapp.com"
    # https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
    - Filesystem MCP (stdio):
        npx @agent-infra/mcp-server-filesystem@latest --allowed-directories "$HOME/mcp_captures"

Install:
    # https://github.com/modelcontextprotocol/python-sdk
    uv pip install -r requirements.txt  # or: pip install -r requirements.txt

Usage:
    uv run capture_network.py \
        --url https://yourapp.com \
        --out "$HOME/mcp_captures/captures/yourapp_{ts}.jsonl" \
        --sse http://127.0.0.1:8931/sse \
        --fs-cmd npx \
        --fs-args "@agent-infra/mcp-server-filesystem@latest" \
        --wait-mode networkidle \
        --wait 15 \
        --filter-url "api\\.yourapp\\.com" \
        --filter-method GET \
        --status-min 200 --status-max 399
"""

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict, Iterable, List, Optional

from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client


# Logging
def setup_logging() -> None:
    """Configure root logger from environment variables."""
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_format = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log_datefmt = os.getenv("LOG_DATEFMT", "%Y-%m-%d %H:%M:%S")
    logging.basicConfig(
        level=log_level, format=log_format, datefmt=log_datefmt, stream=sys.stdout
    )
    # Quiet noisy libs unless explicitly raised
    for noisy_logger in ("httpx", "urllib3", "asyncio"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


LOGGER = logging.getLogger("capture_network")


# Helpers
def timestamp_yyyymmdd_hhmmss() -> str:
    """Return current local timestamp formatted as YYYYMMDD_HHMMSS."""
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def expand_output_path_template(path_template: str) -> str:
    """Expand '~' and the '{ts}' token in an output path template."""
    return os.path.expanduser(
        path_template.replace("{ts}", timestamp_yyyymmdd_hhmmss())
    )


def extract_tool_result_json(result: types.CallToolResult) -> Any:
    """Robustly unpack an MCP tool result's content (JSON or JSON-as-text).

    Avoids relying on optional content classes by inspecting the generic `type` field
    available on both dataclass-like objects and dict payloads.
    """
    if not result or result.content is None:
        return None

    for content_entry in result.content:
        # Determine entry type from attribute or mapping
        content_type = getattr(content_entry, "type", None)
        if content_type is None and isinstance(content_entry, dict):
            content_type = content_entry.get("type")

        if content_type == "json":
            if hasattr(content_entry, "json"):
                return getattr(content_entry, "json")
            if isinstance(content_entry, dict):
                return content_entry.get("json")
            return None

        if content_type == "text":
            if hasattr(content_entry, "text"):
                text_payload = getattr(content_entry, "text") or ""
            elif isinstance(content_entry, dict):
                text_payload = content_entry.get("text", "") or ""
            else:
                text_payload = ""
            try:
                return json.loads(text_payload)
            except Exception:
                return {"text": text_payload}

    return None


def serialize_to_jsonl(payload: Any) -> str:
    """Serialize a list/dict payload into newline-delimited JSON (JSONL)."""
    if payload is None:
        return ""
    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False) + "\n"
    if isinstance(payload, list):
        return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payload)
    # Fallback shape
    return json.dumps({"value": payload}, ensure_ascii=False) + "\n"


def filter_network_requests(
    request_records: Iterable[Dict[str, Any]],
    url_regex: Optional[str],
    method_filter: Optional[str],
    status_min: Optional[int],
    status_max: Optional[int],
) -> List[Dict[str, Any]]:
    """Filter request dictionaries by URL regex, HTTP method, and status range."""
    if request_records is None:
        return []

    compiled_url_pattern = re.compile(url_regex) if url_regex else None
    method_filter_upper = method_filter.upper() if method_filter else None

    filtered_records: List[Dict[str, Any]] = []
    for request_record in request_records:
        url_value = (
            (request_record.get("request") or {}).get("url")
            or request_record.get("url")
            or ""
        )
        method_value = (request_record.get("request") or {}).get(
            "method"
        ) or request_record.get("method")
        status_value = (request_record.get("response") or {}).get(
            "status"
        ) or request_record.get("status")

        if compiled_url_pattern and not compiled_url_pattern.search(url_value):
            continue
        if method_filter_upper and (
            not method_value or str(method_value).upper() != method_filter_upper
        ):
            continue
        if status_min is not None and status_value is not None:
            try:
                if int(status_value) < status_min:
                    continue
            except (TypeError, ValueError):
                pass
        if status_max is not None and status_value is not None:
            try:
                if int(status_value) > status_max:
                    continue
            except (TypeError, ValueError):
                pass

        filtered_records.append(request_record)

    return filtered_records


async def call_tool_with_retry(
    client_session: ClientSession,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]] = None,
    *,
    retries: int = 2,
    backoff_seconds: float = 0.75,
) -> types.CallToolResult:
    """Call a tool with retry and exponential backoff."""
    attempt_index = 0
    last_exception: Optional[Exception] = None

    while attempt_index <= retries:
        try:
            return await client_session.call_tool(tool_name, tool_args or {})
        except Exception as exc:  # broad: MCP servers surface various error types
            last_exception = exc
            attempt_index += 1
            if attempt_index > retries:
                break
            sleep_seconds = backoff_seconds * (2 ** (attempt_index - 1))
            LOGGER.warning(
                "Tool %s failed (attempt %d/%d): [%s] %s — retrying in %.2fs",
                tool_name,
                attempt_index,
                retries,
                type(exc).__name__,
                exc,
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

    raise RuntimeError(
        f"Tool {tool_name} failed after {retries} retries"
    ) from last_exception


# Core Client
class NetworkCaptureClient:
    """Owns connections to Playwright MCP (SSE) and Filesystem MCP (stdio)."""

    def __init__(
        self, playwright_sse_url: str, filesystem_stdio_params: StdioServerParameters
    ):
        self.playwright_sse_url = playwright_sse_url
        self.filesystem_stdio_params = filesystem_stdio_params
        self._exit_stack: Optional[AsyncExitStack] = None
        self.playwright_session: Optional[ClientSession] = None
        self.filesystem_session: Optional[ClientSession] = None

    async def __aenter__(self):
        self._exit_stack = AsyncExitStack()

        # Playwright (SSE)
        playwright_read_stream, playwright_write_stream = (
            await self._exit_stack.enter_async_context(
                sse_client(self.playwright_sse_url)
            )
        )
        self.playwright_session = await self._exit_stack.enter_async_context(
            ClientSession(playwright_read_stream, playwright_write_stream)
        )
        await self.playwright_session.initialize()

        # Filesystem (stdio)
        filesystem_read_stream, filesystem_write_stream = (
            await self._exit_stack.enter_async_context(
                stdio_client(self.filesystem_stdio_params)
            )
        )
        self.filesystem_session = await self._exit_stack.enter_async_context(
            ClientSession(filesystem_read_stream, filesystem_write_stream)
        )
        await self.filesystem_session.initialize()

        LOGGER.info("Connected to Playwright MCP (SSE) and Filesystem MCP (stdio)")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._exit_stack:
            await self._exit_stack.__aexit__(exc_type, exc_val, exc_tb)
        self._exit_stack = None
        self.playwright_session = None
        self.filesystem_session = None
        LOGGER.info("Disconnected MCP sessions")

    async def tool_is_available(
        self, client_session: ClientSession, tool_name: str
    ) -> bool:
        """Return True iff `tool_name` exists in the given MCP client session."""
        try:
            tools = await client_session.list_tools()
            return any(
                tool_descriptor.name == tool_name for tool_descriptor in tools.tools
            )
        except Exception as exc:
            LOGGER.warning("Error checking tool availability for '%s': %s", tool_name, exc)
            return False

    async def capture_network_requests(
        self,
        navigate_url: str,
        wait_mode: str,
        wait_timeout_seconds: float,
    ) -> List[Dict[str, Any]]:
        """Navigate, wait, fetch network requests, and return as a list of dicts."""
        assert self.playwright_session, "Playwright session not initialized"

        LOGGER.info("Navigating to %s", navigate_url)
        await call_tool_with_retry(
            self.playwright_session, "browser_navigate", {"url": navigate_url}
        )

        # Prefer semantic waiting if available; otherwise sleep fallback.
        if await self.tool_is_available(self.playwright_session, "browser_wait_for"):
            if wait_mode == "sleep":
                LOGGER.info("Waiting (sleep) for %.2fs", wait_timeout_seconds)
                await asyncio.sleep(wait_timeout_seconds)
            elif wait_mode == "networkidle":
                LOGGER.info(
                    "Waiting for state=networkidle (timeout=%ss)",
                    int(wait_timeout_seconds),
                )
                await call_tool_with_retry(
                    self.playwright_session,
                    "browser_wait_for",
                    {
                        "state": "networkidle",
                        "timeout": int(wait_timeout_seconds * 1000),
                    },
                )
            else:
                LOGGER.info(
                    "Unknown wait_mode=%s — defaulting to sleep %.2fs",
                    wait_mode,
                    wait_timeout_seconds,
                )
                await asyncio.sleep(wait_timeout_seconds)
        else:
            LOGGER.info(
                "browser_wait_for not available — sleeping %.2fs", wait_timeout_seconds
            )
            await asyncio.sleep(wait_timeout_seconds)

        LOGGER.info("Fetching network requests")
        call_result = await call_tool_with_retry(
            self.playwright_session, "browser_network_requests", {}
        )
        network_payload = extract_tool_result_json(call_result)

        # Normalize to list for downstream processing
        if network_payload is None:
            return []
        if isinstance(network_payload, list):
            return network_payload
        if isinstance(network_payload, dict):
            if "requests" in network_payload and isinstance(
                network_payload["requests"], list
            ):
                return network_payload["requests"]
            return [network_payload]
        return [{"value": network_payload}]

    async def save_jsonl(
        self, request_records: List[Dict[str, Any]], output_path: str
    ) -> str:
        """Persist request records as JSONL to `output_path` via Filesystem MCP."""
        assert self.filesystem_session, "Filesystem session not initialized"
        expanded_output_path = expand_output_path_template(output_path)
        output_directory = os.path.dirname(expanded_output_path)

        # Best-effort create directory if tool exists
        if await self.tool_is_available(self.filesystem_session, "create_directory"):
            try:
                await call_tool_with_retry(
                    self.filesystem_session,
                    "create_directory",
                    {"path": output_directory},
                )
                LOGGER.info("Ensured directory: %s", output_directory)
            except Exception as exc:
                LOGGER.warning("create_directory failed (non-fatal): %s", exc)

        jsonl_blob = serialize_to_jsonl(request_records)
        await call_tool_with_retry(
            self.filesystem_session,
            "write_file",
            {"path": expanded_output_path, "content": jsonl_blob},
        )
        LOGGER.info("Saved %d lines to %s", len(request_records), expanded_output_path)
        return expanded_output_path


# CLI Code Glue
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture_network",
        description="Capture network requests via Playwright MCP and save via Filesystem MCP.",
    )
    parser.add_argument("--url", required=True, help="URL to navigate.")
    parser.add_argument(
        "--sse",
        default=os.getenv("PLAYWRIGHT_MCP_SSE_URL", "http://127.0.0.1:8931/sse"),
        help="Playwright MCP SSE URL.",
    )
    parser.add_argument(
        "--fs-cmd",
        default=os.getenv("FILESYSTEM_MCP_CMD", "npx"),
        help="Filesystem MCP command (e.g., npx).",
    )
    parser.add_argument(
        "--fs-args",
        nargs="+",
        default=os.getenv(
            "FILESYSTEM_MCP_ARGS", "@agent-infra/mcp-server-filesystem@latest"
        ).split(),
        help="Filesystem MCP args.",
    )
    parser.add_argument(
        "--out",
        default=os.getenv(
            "CAPTURE_OUT",
            os.path.expanduser("~/mcp_captures/captures/capture_{ts}.jsonl"),
        ),
        help="Output JSONL path (supports {ts}).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=float(os.getenv("CAPTURE_WAIT_SECS", "5")),
        help="Seconds to wait (timeout for semantic waits; sleep duration for --wait-mode=sleep).",
    )
    parser.add_argument(
        "--wait-mode",
        choices=("sleep", "networkidle"),
        default=os.getenv("CAPTURE_WAIT_MODE", "networkidle"),
        help="Wait strategy after navigation.",
    )
    # Client-side filters
    parser.add_argument(
        "--filter-url",
        type=str,
        default=os.getenv("CAPTURE_FILTER_URL", None),
        help="Python regex to filter request URLs.",
    )
    parser.add_argument(
        "--filter-method",
        type=str,
        default=os.getenv("CAPTURE_FILTER_METHOD", None),
        help="HTTP method filter (e.g., GET, POST).",
    )
    parser.add_argument(
        "--status-min",
        type=int,
        default=int(os.getenv("CAPTURE_STATUS_MIN", "0")),
        help="Minimum response status to keep.",
    )
    parser.add_argument(
        "--status-max",
        type=int,
        default=int(os.getenv("CAPTURE_STATUS_MAX", "999")),
        help="Maximum response status to keep.",
    )
    return parser


async def run_async(argv: List[str]) -> int:
    """Async entrypoint."""
    setup_logging()
    parser = build_parser()
    cli_args = parser.parse_args(argv)

    filesystem_stdio_params = StdioServerParameters(
        command=cli_args.fs_cmd, args=cli_args.fs_args
    )

    try:
        async with NetworkCaptureClient(
            cli_args.sse, filesystem_stdio_params
        ) as capture_client:
            LOGGER.info("Starting capture for %s", cli_args.url)
            captured_requests = await capture_client.capture_network_requests(
                cli_args.url, cli_args.wait_mode, cli_args.wait
            )

            # Client-side filtering
            filtered_requests = filter_network_requests(
                captured_requests,
                url_regex=cli_args.filter_url,
                method_filter=cli_args.filter_method,
                status_min=cli_args.status_min,
                status_max=cli_args.status_max,
            )
            # Use parser defaults for status_min and status_max to avoid magic numbers
            status_min_default = parser.get_default("status_min")
            status_max_default = parser.get_default("status_max")
            if (
                cli_args.filter_url
                or cli_args.filter_method
                or cli_args.status_min != status_min_default
                or cli_args.status_max != status_max_default
            ):
                LOGGER.info(
                    "Filtered %d → %d requests",
                    len(captured_requests),
                    len(filtered_requests),
                )
            else:
                filtered_requests = captured_requests

            saved_output_path = await capture_client.save_jsonl(
                filtered_requests, cli_args.out
            )
            # Print the final path for shell pipelines
            print(saved_output_path)
            return 0

    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return 130
    except Exception as exc:
        LOGGER.error("Fatal error: %s", exc, exc_info=True)
        return 1


def main() -> None:
    """Sync entrypoint."""
    raise SystemExit(asyncio.run(run_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
