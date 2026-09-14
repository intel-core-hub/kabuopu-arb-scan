#!/usr/bin/env python3
"""Phase 14a: read-only IBKR API control-plane RTT proxy measurement.

This measures elapsed local monotonic time from EClient.reqCurrentTime() to the
corresponding EWrapper.currentTime() callback, one request at a time.  The value is a
control-plane round-trip proxy through the connected TWS/IB Gateway session.  It is NOT
an order-entry latency, exchange-arrival latency, or one-way latency estimate.

No order API is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def empirical_quantile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank empirical quantile, intentionally simple and reproducible."""
    clean = sorted(float(v) for v in values if _finite(v) is not None)
    if not clean:
        return None
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0,1]")
    if q == 0:
        return clean[0]
    rank = max(1, math.ceil(q * len(clean)))
    return clean[min(rank - 1, len(clean) - 1)]


def summarize_samples(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [
        float(row["rtt_ms"])
        for row in rows
        if not bool(row.get("is_warmup"))
        and str(row.get("status", "")).upper() == "OK"
        and _finite(row.get("rtt_ms")) is not None
    ]
    return {
        "phase14_probe_kind": "IBKR_REQ_CURRENT_TIME_CONTROL_PLANE_RTT_PROXY",
        "phase14_is_order_arrival_latency": False,
        "phase14_is_exchange_latency": False,
        "phase14_one_way_inference_used": False,
        "phase14_live_money_allowed": False,
        "successful_samples": len(measured),
        "rtt_min_ms": min(measured) if measured else None,
        "rtt_median_ms": statistics.median(measured) if measured else None,
        "rtt_p90_ms": empirical_quantile(measured, 0.90),
        "rtt_p95_ms": empirical_quantile(measured, 0.95),
        "rtt_p99_ms": empirical_quantile(measured, 0.99),
        "rtt_max_ms": max(measured) if measured else None,
        "interpretation": (
            "Elapsed local monotonic time from reqCurrentTime() submission to currentTime() callback. "
            "This is a TWS/IB Gateway control-plane RTT proxy only; it is deliberately not halved into "
            "a one-way estimate and does not measure order processing or exchange arrival."
        ),
    }


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError("IBKR Python API is not installed; install requirements-ibkr.txt") from exc
    return EClient, EWrapper


def make_app():
    EClient, EWrapper = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self.response = threading.Event()
            self._lock = threading.Lock()
            self.pending_started_ns: Optional[int] = None
            self.pending_sent_at_utc: Optional[str] = None
            self.last_result: Optional[dict[str, Any]] = None
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId):  # noqa: N802, ANN001, ARG002
            self.ready.set()

        def currentTime(self, server_time):  # noqa: N802, ANN001
            received_ns = time.monotonic_ns()
            received_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                started = self.pending_started_ns
                sent_at = self.pending_sent_at_utc
                if started is None:
                    return
                self.last_result = {
                    "request_sent_at_utc": sent_at,
                    "callback_received_at_utc": received_at,
                    "server_epoch": int(server_time),
                    "rtt_ms": round((received_ns - started) / 1_000_000.0, 3),
                    "status": "OK",
                }
                self.pending_started_ns = None
                self.pending_sent_at_utc = None
            self.response.set()

        def error(self, reqId, *args):  # noqa: N802, ANN001
            # Compatible with both older and newer EWrapper.error signatures.
            error_code = -1
            error_string = ""
            if len(args) >= 2:
                if len(args) >= 3 and isinstance(args[1], int):
                    # Newer APIs may include errorTime before errorCode.
                    error_code = int(args[1])
                    error_string = str(args[2])
                else:
                    try:
                        error_code = int(args[0])
                    except (TypeError, ValueError):
                        error_code = -1
                    error_string = str(args[1])
            self.errors.append((int(reqId) if str(reqId).lstrip("-").isdigit() else -1, error_code, error_string))

        def measure_once(self, timeout_sec: float) -> dict[str, Any]:
            if timeout_sec <= 0:
                raise ValueError("timeout_sec must be positive")
            self.response.clear()
            with self._lock:
                if self.pending_started_ns is not None:
                    raise RuntimeError("reqCurrentTime measurement already pending")
                self.last_result = None
                self.pending_sent_at_utc = datetime.now(timezone.utc).isoformat()
                self.pending_started_ns = time.monotonic_ns()
            self.reqCurrentTime()
            if not self.response.wait(timeout_sec):
                with self._lock:
                    sent_at = self.pending_sent_at_utc
                    self.pending_started_ns = None
                    self.pending_sent_at_utc = None
                return {
                    "request_sent_at_utc": sent_at,
                    "callback_received_at_utc": "",
                    "server_epoch": "",
                    "rtt_ms": "",
                    "status": "TIMEOUT",
                }
            with self._lock:
                return dict(self.last_result or {"status": "ERROR", "rtt_ms": ""})

    return App()


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sample_index",
        "is_warmup",
        "request_sent_at_utc",
        "callback_received_at_utc",
        "server_epoch",
        "rtt_ms",
        "status",
    ]
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=914)
    p.add_argument("--samples", type=int, default=30)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--interval-sec", type=float, default=0.5)
    p.add_argument("--timeout-sec", type=float, default=5.0)
    p.add_argument("--output", default="data/phase14_api_rtt.csv")
    p.add_argument("--summary-json", default="data/phase14_api_rtt_summary.json")
    args = p.parse_args()
    if args.samples <= 0 or args.warmup < 0:
        p.error("--samples must be positive and --warmup non-negative")
    if args.interval_sec < 0 or args.timeout_sec <= 0:
        p.error("--interval-sec must be non-negative and --timeout-sec positive")
    return args


def main() -> int:
    args = _args()
    app = make_app()
    app.connect(args.host, args.port, clientId=args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(10.0):
        app.disconnect()
        raise RuntimeError("IBKR API connection did not become ready")

    rows: list[dict[str, Any]] = []
    total = args.warmup + args.samples
    try:
        for i in range(total):
            result = app.measure_once(args.timeout_sec)
            row = {
                "sample_index": i + 1,
                "is_warmup": i < args.warmup,
                **result,
            }
            rows.append(row)
            if i + 1 < total and args.interval_sec > 0:
                time.sleep(args.interval_sec)
    finally:
        app.disconnect()

    write_csv(args.output, rows)
    summary = summarize_samples(rows)
    summary.update(
        {
            "host": args.host,
            "port": args.port,
            "client_id": args.client_id,
            "requested_samples": args.samples,
            "warmup_samples": args.warmup,
            "timeout_count": sum(1 for r in rows if r.get("status") == "TIMEOUT"),
            "api_error_count": len(app.errors),
        }
    )
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["successful_samples"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
