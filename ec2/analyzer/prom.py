"""Prometheus에서 판단에 쓸 현재 값을 읽어온다. 예측값은 forecast.py가 직접 계산한 걸 받아서 씀."""
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from typing import List, Optional

import config
import forecast
from decide import Metrics

log = logging.getLogger("analyzer")


def _query(expr: str) -> list:
    url = f"{config.PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(f"쿼리 실패: {expr}")
    return body["data"]["result"]


def _scalar(expr: str) -> Optional[float]:
    result = _query(expr)
    return float(result[0]["value"][1]) if result else None


def node_ready(node: str) -> bool:
    """K3s 노드가 Ready인지 (kube-state-metrics 기준)."""
    v = _scalar(f'max(kube_node_status_condition{{node="{node}", condition="Ready", status="true"}})')
    return v == 1.0


def read_metrics(forecasts: List["forecast.Forecast"]) -> Metrics:
    steal = {
        r["metric"].get("vm", "unknown"): float(r["value"][1])
        for r in _query("beet:vm_cpu_steal:ratio")
    }
    # 포화 시점 범위 = CPU·메모리 예약률 중 "가장 빠른 포화"가 더 이른 쪽
    sats = [dict(f.saturation, resource=name, current=f.current)
            for name, f in (("CPU", forecast.get(forecasts, "cluster_cpu_requests_ratio")),
                            ("메모리", forecast.get(forecasts, "cluster_mem_requests_ratio")))
            if f and f.saturation and f.saturation.get("earliest") is not None]
    sat = min(sats, key=lambda s: s["earliest"]) if sats else None
    return Metrics(
        capacity=_capacity_or_none(forecasts),
        steal_by_vm=steal,
        requests_saturation=sat,
        cpu_requests_now=_scalar("beet:cluster_cpu_requests:ratio"),
        openstack_quota_used=_scalar("beet:openstack_quota_used:ratio"),
        aws_nodes=int(_scalar("beet:aws_node:count") or 0),
        vm3_nodes=int(_scalar("beet:vm3_node:count") or 0),
        host_cpu=_scalar('1 - avg(rate(node_cpu_seconds_total{layer="physical", mode="idle"}[2m]))'),
        host_mem=_scalar('1 - sum(node_memory_MemAvailable_bytes{layer="physical"})'
                         ' / sum(node_memory_MemTotal_bytes{layer="physical"})'),
        host_mem_avail_gb=_scalar('sum(node_memory_MemAvailable_bytes{layer="physical"}) / 1024^3'),
    )


def _capacity_or_none(forecasts):
    """증설·회수용 지표를 못 읽어도 None으로 넘김 → 증설·회수만 보류하고 재배치·포화 알림은 계속 돌게."""
    try:
        return capacity_context(forecasts)
    except Exception as e:
        log.warning("용량 지표 확인 실패 → 증설·회수 보류, 나머지 판단은 계속: %s", e)
        return None


def fresh(expr):
    """Validate source sample timestamps, rather than the instant-query evaluation time."""
    rows = _query(expr)
    stamps = _query(f"timestamp({expr})")
    now = time.time()
    def identity(row):
        return tuple(sorted((k, v) for k, v in row["metric"].items() if k != "__name__"))
    # For selectors with several metric names, use separate calls: timestamp removes __name__.
    if len(rows) != len(stamps) or not rows:
        raise RuntimeError(f"Missing telemetry: {expr}")
    stamp_map = {identity(r): float(r["value"][1]) for r in stamps}
    for row in rows:
        stamp = stamp_map.get(identity(row))
        value = float(row["value"][1])
        if stamp is None or not -10 <= now - stamp <= 120 or not math.isfinite(value):
            raise RuntimeError(f"Stale/invalid telemetry: {expr}")
    return rows, min(stamp_map.values())


def capacity_context(forecasts):
    """Read a coherent single-host observation. Fail closed on missing or duplicate exporters."""
    times = []
    def rows(expr):
        values, stamp = fresh(expr)
        times.append(stamp)
        return values
    def single(expr):
        values = rows(expr)
        if len(values) != 1 or values[0]["metric"].get("host") != config.PHYSICAL_HOST:
            raise RuntimeError("Physical host inventory changed; update placement policy")
        return float(values[0]["value"][1])
    def by_node(expr, label="node"):
        result = {}
        for row in rows(expr):
            name = row["metric"][label]
            if name in result:
                raise RuntimeError("Duplicate node telemetry")
            result[name] = float(row["value"][1])
        return result
    host_mem = single('node_memory_MemAvailable_bytes{layer="physical"}')
    disk = single('node_filesystem_avail_bytes{layer="physical",mountpoint="/"}')
    nova_mem = single("openstack_nova_memory_available_bytes") - single("openstack_nova_memory_used_bytes")
    nova_cpu = single("openstack_nova_vcpus_available") - single("openstack_nova_vcpus_used")
    nova_disk = single("openstack_nova_free_disk_bytes")
    for expr in ('up{job="node-host"}', 'up{job="openstack"}'):
        if single(expr) != 1:
            raise RuntimeError("Physical host or cloud exporter is down")
    rows('node_cpu_seconds_total{layer="physical",mode="idle"}')
    cpu_rows = _query('1-avg by(host)(rate(node_cpu_seconds_total{layer="physical",mode="idle"}[2m]))')
    if len(cpu_rows) != 1 or cpu_rows[0]["metric"].get("host") != config.PHYSICAL_HOST:
        raise RuntimeError("Physical CPU inventory is ambiguous")
    cpu_ratio = float(cpu_rows[0]["value"][1])
    if not math.isfinite(cpu_ratio) or not 0 <= cpu_ratio <= 1:
        raise RuntimeError("Invalid physical CPU utilization")
    cpus = by_node('kube_node_status_allocatable{resource="cpu"}')
    memory = by_node('kube_node_status_allocatable{resource="memory"}')
    ready = by_node('kube_node_status_condition{condition="Ready",status="true"}')
    available = by_node('node_memory_MemAvailable_bytes{job="node-vm",layer="vm"}', "vm")
    total = by_node('node_memory_MemTotal_bytes{job="node-vm",layer="vm"}', "vm")
    if not set(cpus) == set(memory) == set(ready) == set(available) == set(total):
        raise RuntimeError("Node telemetry is incomplete")
    nodes = {name: {"cpu": cpus[name], "memory": memory[name], "ready": ready[name] == 1,
                    "used_memory": max(0, total[name] - available[name])} for name in cpus}
    demand = {}
    for resource, key in (("cpu", "cluster_cpu_requests_ratio"), ("memory", "cluster_mem_requests_ratio")):
        capacity = sum(n[resource] for n in nodes.values())
        current = _scalar(f'beet:cluster_{"mem" if resource == "memory" else "cpu"}_requests:ratio')
        if current is None or not math.isfinite(current) or current < 0:
            raise RuntimeError("Missing resource request ratio")
        absolute_key = "cluster_cpu_requested_cores" if resource == "cpu" else "cluster_mem_requested_bytes"
        prediction = forecast.get(forecasts, absolute_key)
        predicted = max(current * capacity, prediction.upper, prediction.current) if prediction else current * capacity
        if not math.isfinite(predicted):
            raise RuntimeError("Invalid resource forecast")
        demand[resource] = predicted
    demand["memory"] = max(demand["memory"], sum(n["used_memory"] for n in nodes.values()))
    return {"observed_at": min(times), "nodes": nodes, "demand": demand,
            "host": {"name": config.PHYSICAL_HOST, "observed_at": min(times),
                     "free_vcpus": max(0, nova_cpu), "free_memory": max(0, min(host_mem, nova_mem)),
                     "free_disk": max(0, min(disk, nova_disk)), "cpu_used_ratio": cpu_ratio}}
