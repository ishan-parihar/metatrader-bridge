#!/usr/bin/env python3
"""E2E latency benchmark: TCP vs HTTP transport for MetaTraderBridge bridge.

Measures round-trip latency for common commands over both transports.
Supports live mode (real MT5 bridge) and mock mode (local echo servers).

Usage:
    # Mock mode (no MT5 needed)
    python tests/benchmarks/benchmark_latency.py --mode mock

    # Live mode (requires TCP bridge on 8025, HTTP gateway on 8020)
    python tests/benchmarks/benchmark_latency.py --mode live

    # Custom iterations
    python tests/benchmarks/benchmark_latency.py --mode mock --iterations 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
_HEADER_FORMAT = "!I"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------
def percentile(data: list[float], p: float) -> float:
    """Compute p-th percentile (0-100) of sorted data."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


@dataclass
class BenchmarkResult:
    command: str
    transport: str  # "TCP" or "HTTP"
    latencies_ms: list[float] = field(default_factory=list)
    successes: int = 0
    failures: int = 0
    connect_time_ms: float = 0.0  # TCP first-connect overhead

    @property
    def p50(self) -> float:
        return percentile(self.latencies_ms, 50)

    @property
    def p95(self) -> float:
        return percentile(self.latencies_ms, 95)

    @property
    def p99(self) -> float:
        return percentile(self.latencies_ms, 99)

    @property
    def mean(self) -> float:
        return (
            sum(self.latencies_ms) / len(self.latencies_ms)
            if self.latencies_ms
            else 0.0
        )

    @property
    def min_lat(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_lat(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return (self.successes / total * 100.0) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Command definitions — what to send over each transport
# ---------------------------------------------------------------------------
def tcp_commands(symbol: str) -> list[tuple[str, dict[str, Any]]]:
    """Return (type, payload) pairs for TCP bridge commands."""
    return [
        ("get_bars", {"symbol": symbol, "timeframe": "M1", "count": 10}),
        (
            "get_indicator",
            {"symbol": symbol, "timeframe": "H1", "indicator": "rsi", "period": 14},
        ),
        ("get_account", {}),
        ("get_positions", {}),
    ]


def http_command_params(symbol: str) -> list[tuple[str, dict[str, str]]]:
    """Return (type, query_params) for HTTP enqueue endpoint.

    The HTTP gateway uses query-string params on POST /bridge/commands/enqueue.
    """
    return [
        ("get_bars", {"symbol": symbol, "timeframe": "M1", "count": "10"}),
        (
            "get_indicator",
            {"symbol": symbol, "timeframe": "H1", "indicator": "rsi", "period": "14"},
        ),
        ("get_account", {}),
        ("get_positions", {}),
    ]


# ---------------------------------------------------------------------------
# TCP client (raw, for benchmarking)
# ---------------------------------------------------------------------------
async def tcp_send_and_recv(
    host: str, port: int, cmd_type: str, payload: dict[str, Any], timeout: float = 20.0
) -> tuple[dict[str, Any], float]:
    """Connect, send one command, receive response. Returns (frame, elapsed_ms)."""
    start = time.monotonic()
    reader, writer = await asyncio.open_connection(host, port)
    connect_ms = (time.monotonic() - start) * 1000

    import uuid

    request_id = str(uuid.uuid4())
    frame = {"type": cmd_type, "request_id": request_id, **payload}
    json_bytes = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    header = struct.pack(_HEADER_FORMAT, len(json_bytes))

    send_start = time.monotonic()
    writer.write(header + json_bytes)
    await writer.drain()

    # Read response: first 4 bytes = length, then payload
    len_data = await asyncio.wait_for(reader.readexactly(_HEADER_SIZE), timeout=timeout)
    payload_len = struct.unpack(_HEADER_FORMAT, len_data)[0]
    resp_data = await asyncio.wait_for(reader.readexactly(payload_len), timeout=timeout)
    recv_ms = (time.monotonic() - send_start) * 1000

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass

    result = json.loads(resp_data.decode("utf-8"))
    return result, recv_ms, connect_ms


# ---------------------------------------------------------------------------
# HTTP client (raw, for benchmarking — stdlib only)
# ---------------------------------------------------------------------------
def http_send_and_recv(
    base_url: str, cmd_type: str, params: dict[str, str], timeout: float = 20.0
) -> tuple[dict[str, Any], float]:
    """Enqueue command via HTTP, poll for result. Returns (result, total_ms)."""
    import urllib.request
    import urllib.parse

    # Build enqueue URL with query params
    qs = urllib.parse.urlencode({"type": cmd_type, **params})
    enqueue_url = f"{base_url}/bridge/commands/enqueue?{qs}"

    start = time.monotonic()
    req = urllib.request.Request(enqueue_url, method="POST", data=b"")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        enqueue_result = json.loads(resp.read().decode())

    cmd_id = enqueue_result["id"]

    # Poll for result
    result_url = f"{base_url}/bridge/results/{cmd_id}"
    deadline = start + timeout
    last_result = None
    while time.monotonic() < deadline:
        time.sleep(0.05)  # 50ms polling interval
        with urllib.request.urlopen(result_url, timeout=timeout) as resp:
            last_result = json.loads(resp.read().decode())
        if last_result.get("status") in ("completed", "error"):
            break

    total_ms = (time.monotonic() - start) * 1000
    return last_result, total_ms


# ---------------------------------------------------------------------------
# Mock servers
# ---------------------------------------------------------------------------
class MockTCPHandler(asyncio.Protocol):
    """Echo-style TCP handler that parses frames and responds instantly."""

    def __init__(self):
        self._buffer = bytearray()
        self._expected: int | None = None
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport

    def data_received(self, data):
        self._buffer.extend(data)
        while True:
            if self._expected is None:
                if len(self._buffer) < _HEADER_SIZE:
                    break
                self._expected = struct.unpack(
                    _HEADER_FORMAT, self._buffer[:_HEADER_SIZE]
                )[0]
            if len(self._buffer) < _HEADER_SIZE + self._expected:
                break
            json_bytes = bytes(
                self._buffer[_HEADER_SIZE : _HEADER_SIZE + self._expected]
            )
            del self._buffer[: _HEADER_SIZE + self._expected]
            self._expected = None

            request = json.loads(json_bytes.decode("utf-8"))
            request_id = request.get("request_id", "unknown")
            cmd_type = request.get("type", "unknown")

            # Build mock response based on command type
            response = self._build_response(cmd_type, request_id)
            resp_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
            header = struct.pack(_HEADER_FORMAT, len(resp_bytes))
            self._transport.write(header + resp_bytes)

    def _build_response(self, cmd_type: str, request_id: str) -> dict:
        if cmd_type == "get_bars":
            return {
                "request_id": request_id,
                "status": "ok",
                "payload": {
                    "bars": [
                        {
                            "time": 1700000000,
                            "open": 2000.0,
                            "high": 2001.0,
                            "low": 1999.0,
                            "close": 2000.5,
                            "volume": 100,
                        }
                        for _ in range(10)
                    ]
                },
            }
        elif cmd_type == "get_indicator":
            return {
                "request_id": request_id,
                "status": "ok",
                "payload": {"values": [55.5, 56.2, 54.8]},
            }
        elif cmd_type == "get_account":
            return {
                "request_id": request_id,
                "status": "ok",
                "payload": {
                    "balance": 10000.0,
                    "equity": 10050.0,
                    "margin": 500.0,
                    "free_margin": 9550.0,
                },
            }
        elif cmd_type == "get_positions":
            return {
                "request_id": request_id,
                "status": "ok",
                "payload": {
                    "positions": [
                        {"id": 1, "symbol": "XAUUSD", "volume": 0.1, "profit": 50.0}
                    ]
                },
            }
        else:
            return {
                "request_id": request_id,
                "status": "ok",
                "payload": {},
            }


async def start_mock_tcp_server(host: str, port: int) -> asyncio.Server:
    loop = asyncio.get_event_loop()
    server = await loop.create_server(lambda: MockTCPHandler(), host, port)
    return server


class MockHTTPHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler that mimics the bridge gateway's enqueue + results flow."""

    # Shared state across requests (class-level)
    _commands: dict[str, dict] = {}

    def log_message(self, format, *args):
        pass  # Suppress log output

    def do_POST(self):
        if "/bridge/commands/enqueue" in self.path:
            # Generate a command ID, store it as "completed" instantly
            import uuid

            cmd_id = str(uuid.uuid4())
            # Extract type from query string
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            cmd_type = qs.get("type", ["unknown"])[0]

            MockHTTPHandler._commands[cmd_id] = {
                "status": "completed",
                "result": {"payload": self._mock_result(cmd_type)},
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": cmd_id}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if "/bridge/results/" in self.path:
            cmd_id = self.path.split("/bridge/results/")[-1]
            cmd = MockHTTPHandler._commands.get(cmd_id)
            if cmd:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(cmd).encode())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "pending", "result": {}}).encode()
                )
        elif "/bridge/health" in self.path or "/bridge/terminal/status" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"connected": True}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _mock_result(self, cmd_type: str) -> dict:
        if cmd_type == "get_bars":
            return {
                "bars": [
                    {
                        "time": 1700000000,
                        "open": 2000.0,
                        "high": 2001.0,
                        "low": 1999.0,
                        "close": 2000.5,
                        "volume": 100,
                    }
                    for _ in range(10)
                ]
            }
        elif cmd_type == "get_indicator":
            return {"values": [55.5, 56.2, 54.8]}
        elif cmd_type == "get_account":
            return {
                "balance": 10000.0,
                "equity": 10050.0,
                "margin": 500.0,
                "free_margin": 9550.0,
            }
        elif cmd_type == "get_positions":
            return {
                "positions": [
                    {"id": 1, "symbol": "XAUUSD", "volume": 0.1, "profit": 50.0}
                ]
            }
        return {}


def start_mock_http_server(host: str, port: int) -> tuple[HTTPServer, Thread]:
    """Start mock HTTP server in a background thread. Returns (server, thread)."""
    MockHTTPHandler._commands.clear()  # Reset state
    server = HTTPServer((host, port), MockHTTPHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------
async def benchmark_tcp_live(
    host: str,
    port: int,
    commands: list[tuple[str, dict[str, Any]]],
    iterations: int,
    warmup: int,
) -> list[BenchmarkResult]:
    """Benchmark TCP against live bridge."""
    results: list[BenchmarkResult] = []

    for cmd_type, payload in commands:
        result = BenchmarkResult(command=cmd_type, transport="TCP")
        all_latencies: list[float] = []
        first_connect_ms: float = 0.0

        # Measure connection overhead once
        try:
            t0 = time.monotonic()
            r, w = await asyncio.open_connection(host, port)
            first_connect_ms = (time.monotonic() - t0) * 1000
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
        except Exception:
            result.failures += iterations
            results.append(result)
            continue

        result.connect_time_ms = first_connect_ms

        for i in range(warmup + iterations):
            try:
                _, lat_ms, _ = await tcp_send_and_recv(host, port, cmd_type, payload)
                if i >= warmup:
                    all_latencies.append(lat_ms)
                    result.successes += 1
                else:
                    result.successes += 0  # warmup, don't count
            except Exception:
                if i >= warmup:
                    result.failures += 1

        result.latencies_ms = all_latencies
        results.append(result)

    return results


async def benchmark_tcp_mock(
    host: str,
    port: int,
    commands: list[tuple[str, dict[str, Any]]],
    iterations: int,
    warmup: int,
) -> list[BenchmarkResult]:
    """Benchmark TCP against mock server."""
    server = await start_mock_tcp_server(host, port)

    # Let server start
    await asyncio.sleep(0.05)

    results = await benchmark_tcp_live(host, port, commands, iterations, warmup)

    server.close()
    await server.wait_closed()
    return results


def benchmark_http_live(
    base_url: str,
    commands: list[tuple[str, dict[str, str]]],
    iterations: int,
    warmup: int,
) -> list[BenchmarkResult]:
    """Benchmark HTTP against live gateway."""
    results: list[BenchmarkResult] = []

    for cmd_type, params in commands:
        result = BenchmarkResult(command=cmd_type, transport="HTTP")
        all_latencies: list[float] = []

        for i in range(warmup + iterations):
            try:
                _, lat_ms = http_send_and_recv(base_url, cmd_type, params)
                if i >= warmup:
                    all_latencies.append(lat_ms)
                    result.successes += 1
            except Exception:
                if i >= warmup:
                    result.failures += 1

        result.latencies_ms = all_latencies
        results.append(result)

    return results


def benchmark_http_mock(
    host: str,
    port: int,
    commands: list[tuple[str, dict[str, str]]],
    iterations: int,
    warmup: int,
) -> list[BenchmarkResult]:
    """Benchmark HTTP against mock server."""
    base_url = f"http://{host}:{port}"
    http_server, http_thread = start_mock_http_server(host, port)

    # Let server start
    time.sleep(0.1)

    results = benchmark_http_live(base_url, commands, iterations, warmup)

    http_server.shutdown()
    http_thread.join(timeout=2)
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def print_table(results: list[BenchmarkResult]) -> None:
    """Print a clean ASCII table comparing TCP vs HTTP latencies."""
    # Column widths
    col_cmd = 15
    col_tr = 9
    col_stat = 10
    col_pct = 10

    header = (
        f"┌{'─' * col_cmd}┬{'─' * col_tr}┬{'─' * col_stat}┬{'─' * col_stat}"
        f"┬{'─' * col_stat}┬{'─' * col_stat}┬{'─' * col_pct}┐\n"
    )
    header += (
        f"│ {'Command':<{col_cmd - 2}}│ {'Transport':<{col_tr - 2}}│ {'P50':<{col_stat - 2}}"
        f"│ {'P95':<{col_stat - 2}}│ {'P99':<{col_stat - 2}}│ {'Mean':<{col_stat - 2}}"
        f"│ {'Success':<{col_pct - 2}}│\n"
    )
    header += (
        f"├{'─' * col_cmd}┼{'─' * col_tr}┼{'─' * col_stat}┼{'─' * col_stat}"
        f"┼{'─' * col_stat}┼{'─' * col_stat}┼{'─' * col_pct}┤"
    )

    rows = []
    for r in results:
        row = (
            f"│ {r.command:<{col_cmd - 2}}│ {r.transport:<{col_tr - 2}}│ "
            f"{r.p50:>7.1f}ms │ {r.p95:>7.1f}ms │ {r.p99:>7.1f}ms │ "
            f"{r.mean:>7.1f}ms │ {r.success_rate:>6.1f}% │"
        )
        rows.append(row)

    footer = (
        f"└{'─' * col_cmd}┴{'─' * col_tr}┴{'─' * col_stat}┴{'─' * col_stat}"
        f"┴{'─' * col_stat}┴{'─' * col_stat}┴{'─' * col_pct}┘"
    )

    print(header)
    for row in rows:
        print(row)
    print(footer)


def print_summary(results: list[BenchmarkResult]) -> None:
    """Print speedup comparison between TCP and HTTP."""
    tcp_results = {r.command: r for r in results if r.transport == "TCP"}
    http_results = {r.command: r for r in results if r.transport == "HTTP"}

    print()
    for cmd in tcp_results:
        tcp = tcp_results[cmd]
        http = http_results.get(cmd)
        if http and http.p50 > 0 and tcp.p50 > 0:
            speedup_p50 = http.p50 / tcp.p50
            speedup_p95 = http.p95 / tcp.p95 if tcp.p95 > 0 else float("inf")
            print(
                f"  {cmd}: {speedup_p50:.1f}x faster (P50), {speedup_p95:.1f}x faster (P95)"
            )

    # Overall TCP connection overhead
    tcp_conns = [
        r.connect_time_ms
        for r in results
        if r.transport == "TCP" and r.connect_time_ms > 0
    ]
    if tcp_conns:
        print(
            f"\n  TCP connect overhead: {sum(tcp_conns) / len(tcp_conns):.1f}ms (avg)"
        )


def print_diagnostics(results: list[BenchmarkResult]) -> None:
    """Print per-command min/max for debugging."""
    print("\n  Detailed latency breakdown:")
    print(f"  {'Command':<15} {'Transport':<9} {'Min':>8} {'Max':>8} {'Count':>6}")
    print(f"  {'─' * 15} {'─' * 9} {'─' * 8} {'─' * 8} {'─' * 6}")
    for r in results:
        print(
            f"  {r.command:<15} {r.transport:<9} "
            f"{r.min_lat:>6.1f}ms {r.max_lat:>6.1f}ms {len(r.latencies_ms):>6}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="E2E latency benchmark: TCP vs HTTP for MetaTraderBridge bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["live", "mock"],
        default="mock",
        help="Test mode: 'live' (real bridge) or 'mock' (local echo servers, default: mock)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="Number of requests per command type (default: 50)",
    )
    parser.add_argument(
        "--tcp-host",
        default="127.0.0.1",
        help="TCP bridge host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=8025,
        help="TCP bridge port (default: 8025)",
    )
    parser.add_argument(
        "--http-url",
        default="http://127.0.0.1:8020",
        help="HTTP gateway URL (default: http://127.0.0.1:8020)",
    )
    parser.add_argument(
        "--symbol",
        default="XAUUSD",
        help="Symbol to test with (default: XAUUSD)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Warmup iterations to discard (default: 5)",
    )

    args = parser.parse_args()

    mode: str = args.mode
    iterations: int = args.iterations
    warmup: int = args.warmup
    symbol: str = args.symbol
    tcp_host: str = args.tcp_host
    tcp_port: int = args.tcp_port
    http_url: str = args.http_url

    # Parse HTTP URL for mock mode
    from urllib.parse import urlparse

    parsed = urlparse(http_url)
    http_host = parsed.hostname or "127.0.0.1"
    http_port = parsed.port or 8020

    # For mock mode, use ephemeral ports to avoid conflicts
    if mode == "mock":
        tcp_port = 18025
        http_port = 18020

    print(f"MetaTraderBridge Bridge Latency Benchmark")
    print(f"  Mode:        {mode}")
    print(f"  Iterations:  {iterations} (+ {warmup} warmup)")
    print(f"  Symbol:      {symbol}")
    if mode == "live":
        print(f"  TCP:         {tcp_host}:{tcp_port}")
        print(f"  HTTP:        {http_url}")
    else:
        print(f"  TCP mock:    {tcp_host}:{tcp_port}")
        print(f"  HTTP mock:   {http_host}:{http_port}")
    print()

    all_results: list[BenchmarkResult] = []

    # --- TCP Benchmark ---
    tcp_cmds = tcp_commands(symbol)
    print(
        f"Running TCP benchmark ({len(tcp_cmds)} commands x {iterations} iterations)..."
    )

    if mode == "mock":
        tcp_results = asyncio.run(
            benchmark_tcp_mock(tcp_host, tcp_port, tcp_cmds, iterations, warmup)
        )
    else:
        tcp_results = asyncio.run(
            benchmark_tcp_live(tcp_host, tcp_port, tcp_cmds, iterations, warmup)
        )
    all_results.extend(tcp_results)
    print(f"  TCP done.")

    # --- HTTP Benchmark ---
    http_cmds = http_command_params(symbol)
    print(
        f"Running HTTP benchmark ({len(http_cmds)} commands x {iterations} iterations)..."
    )

    if mode == "mock":
        http_results = benchmark_http_mock(
            http_host, http_port, http_cmds, iterations, warmup
        )
    else:
        http_results = benchmark_http_live(http_url, http_cmds, iterations, warmup)
    all_results.extend(http_results)
    print(f"  HTTP done.")

    # --- Output ---
    print()
    print_table(all_results)
    print_summary(all_results)
    print_diagnostics(all_results)

    # Exit code: 0 if all succeeded, 1 if any failures
    any_failures = any(r.failures > 0 for r in all_results)
    sys.exit(1 if any_failures else 0)


if __name__ == "__main__":
    main()
