import asyncio
import json
from types import SimpleNamespace

from capture_network import (
    NetworkCaptureClient,
    filter_network_requests,
    serialize_to_jsonl,
)


def test_filter_network_requests_applies_all_filters():
    requests = [
        {
            "request": {"url": "https://api.example.com/users", "method": "GET"},
            "response": {"status": 200},
        },
        {
            "request": {"url": "https://api.example.com/users", "method": "POST"},
            "response": {"status": 201},
        },
        {"url": "https://example.com/health", "method": "GET", "status": 503},
    ]

    assert filter_network_requests(requests, r"api\.example\.com", "get", 200, 299) == [
        requests[0]
    ]


def test_serialize_to_jsonl_preserves_each_record():
    records = [{"status": 200}, {"status": 404}]

    lines = serialize_to_jsonl(records).splitlines()

    assert [json.loads(line) for line in lines] == records


def test_tool_is_available_handles_present_missing_and_error():
    client = object.__new__(NetworkCaptureClient)

    class AvailableSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[SimpleNamespace(name="write_file"), SimpleNamespace(name="read_file")]
            )

    class BrokenSession:
        async def list_tools(self):
            raise RuntimeError("unavailable")

    assert asyncio.run(client.tool_is_available(AvailableSession(), "write_file"))
    assert not asyncio.run(client.tool_is_available(AvailableSession(), "delete_file"))
    assert not asyncio.run(client.tool_is_available(BrokenSession(), "write_file"))
