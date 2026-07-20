import os
import re
from collections import Counter, defaultdict

import requests
from fastmcp import FastMCP
from kubernetes import client, config


app = FastMCP("kubernetes-mcp")

PROMETHEUS_URL = os.getenv(
  "PROMETHEUS_URL",
  "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090",
)

config.load_incluster_config()
core = client.CoreV1Api()
custom = client.CustomObjectsApi()


def _parse_cpu(value: str) -> float:
  if value.endswith("n"):
    return float(value[:-1]) / 1e9
  if value.endswith("u"):
    return float(value[:-1]) / 1e6
  if value.endswith("m"):
    return float(value[:-1]) / 1e3
  return float(value)


def _parse_mem_bytes(value: str) -> float:
  units = {
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "Pi": 1024 ** 5,
    "Ei": 1024 ** 6,
    "K": 1000,
    "M": 1000 ** 2,
    "G": 1000 ** 3,
    "T": 1000 ** 4,
    "P": 1000 ** 5,
    "E": 1000 ** 6,
  }
  for suffix, mul in units.items():
    if value.endswith(suffix):
      return float(value[:-len(suffix)]) * mul
  return float(value)


def _prom_query(query: str) -> list:
  resp = requests.get(
    f"{PROMETHEUS_URL}/api/v1/query",
    params={"query": query},
    timeout=10,
  )
  resp.raise_for_status()
  data = resp.json()
  if data.get("status") != "success":
    return []
  return data.get("data", {}).get("result", [])


def _prom_sample_to_bytes(sample: dict) -> float:
  value = sample.get("value", [None, "0"])[1]
  try:
    return float(value)
  except (TypeError, ValueError):
    return 0.0


@app.tool()
def get_cluster_health() -> dict:
  """Summarize node readiness and pod phase health cluster-wide."""
  nodes = core.list_node().items
  pods = core.list_pod_for_all_namespaces().items

  ready = 0
  not_ready_nodes = []
  for node in nodes:
    is_ready = False
    for cond in node.status.conditions or []:
      if cond.type == "Ready" and cond.status == "True":
        is_ready = True
    if is_ready:
      ready += 1
    else:
      not_ready_nodes.append(node.metadata.name)

  phases = Counter((pod.status.phase or "Unknown") for pod in pods)
  unhealthy_pods = []
  for pod in pods:
    if pod.status.phase not in ("Running", "Succeeded"):
      unhealthy_pods.append(
        {
          "namespace": pod.metadata.namespace,
          "name": pod.metadata.name,
          "phase": pod.status.phase,
        }
      )

  return {
    "nodes_total": len(nodes),
    "nodes_ready": ready,
    "nodes_not_ready": not_ready_nodes,
    "pod_phase_counts": dict(phases),
    "unhealthy_pods": unhealthy_pods[:30],
  }


@app.tool()
def get_pod_issues(namespace: str = "all", limit: int = 40) -> dict:
  """Return failing pods, restart-heavy pods, and recent warning events."""
  if namespace == "all":
    pods = core.list_pod_for_all_namespaces().items
    events = core.list_event_for_all_namespaces().items
  else:
    pods = core.list_namespaced_pod(namespace).items
    events = core.list_namespaced_event(namespace).items

  issues = []
  for pod in pods:
    cs_list = pod.status.container_statuses or []
    restarts = sum(cs.restart_count for cs in cs_list)
    waiting_reasons = []
    for cs in cs_list:
      if cs.state and cs.state.waiting:
        waiting_reasons.append(cs.state.waiting.reason)

    if pod.status.phase != "Running" or restarts > 0 or waiting_reasons:
      issues.append(
        {
          "namespace": pod.metadata.namespace,
          "name": pod.metadata.name,
          "phase": pod.status.phase,
          "restarts": restarts,
          "waiting_reasons": waiting_reasons,
        }
      )

  warning_events = [
    {
      "namespace": e.metadata.namespace,
      "reason": e.reason,
      "object": f"{e.involved_object.kind}/{e.involved_object.name}",
      "message": e.message,
      "last_timestamp": str(e.last_timestamp or e.event_time or ""),
    }
    for e in events
    if e.type == "Warning"
  ]

  warning_events.sort(key=lambda x: x["last_timestamp"], reverse=True)
  issues.sort(key=lambda x: (x["phase"] != "Running", x["restarts"]), reverse=True)

  return {
    "namespace": namespace,
    "issues": issues[:limit],
    "recent_warning_events": warning_events[:limit],
  }


@app.tool()
def get_node_pressure() -> list:
  """Report node pressure conditions (Memory/Disk/PID) and readiness."""
  nodes = core.list_node().items
  out = []
  for node in nodes:
    cond_map = {c.type: c.status for c in (node.status.conditions or [])}
    out.append(
      {
        "node": node.metadata.name,
        "ready": cond_map.get("Ready", "Unknown"),
        "memory_pressure": cond_map.get("MemoryPressure", "Unknown"),
        "disk_pressure": cond_map.get("DiskPressure", "Unknown"),
        "pid_pressure": cond_map.get("PIDPressure", "Unknown"),
      }
    )
  return out


@app.tool()
def get_namespace_resource_usage(top_k: int = 10) -> dict:
  """Aggregate current CPU and memory usage by namespace via metrics.k8s.io."""
  metrics = custom.list_cluster_custom_object(
    group="metrics.k8s.io",
    version="v1beta1",
    plural="pods",
  )

  usage = defaultdict(lambda: {"cpu_cores": 0.0, "memory_bytes": 0.0})
  for item in metrics.get("items", []):
    ns = item.get("metadata", {}).get("namespace", "unknown")
    for c in item.get("containers", []):
      u = c.get("usage", {})
      cpu = u.get("cpu", "0")
      mem = u.get("memory", "0")
      usage[ns]["cpu_cores"] += _parse_cpu(cpu)
      usage[ns]["memory_bytes"] += _parse_mem_bytes(mem)

  rows = []
  for ns, vals in usage.items():
    rows.append(
      {
        "namespace": ns,
        "cpu_cores": round(vals["cpu_cores"], 4),
        "memory_mib": round(vals["memory_bytes"] / (1024 ** 2), 2),
      }
    )
  rows.sort(key=lambda x: (x["cpu_cores"], x["memory_mib"]), reverse=True)

  return {"top_namespaces": rows[:max(1, top_k)]}


@app.tool()
def get_storage_usage(top_k: int = 10) -> dict:
  """Summarize disk occupancy in bytes for PVCs and node filesystems."""
  pvc_results = _prom_query(
    f"topk({max(1, top_k)}, sum by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes))"
  )
  namespace_results = _prom_query(
    "sum by (namespace) (kubelet_volume_stats_used_bytes)"
  )
  node_results = _prom_query(
    f"topk({max(1, top_k)}, (node_filesystem_size_bytes - node_filesystem_avail_bytes))"
  )

  pvc_rows = []
  for sample in pvc_results:
    metric = sample.get("metric", {})
    pvc_rows.append(
      {
        "namespace": metric.get("namespace", "unknown"),
        "persistentvolumeclaim": metric.get("persistentvolumeclaim", "unknown"),
        "used_bytes": int(_prom_sample_to_bytes(sample)),
      }
    )

  namespace_rows = []
  for sample in namespace_results:
    metric = sample.get("metric", {})
    namespace_rows.append(
      {
        "namespace": metric.get("namespace", "unknown"),
        "used_bytes": int(_prom_sample_to_bytes(sample)),
      }
    )
  namespace_rows.sort(key=lambda x: x["used_bytes"], reverse=True)

  node_rows = []
  for sample in node_results:
    metric = sample.get("metric", {})
    node_rows.append(
      {
        "node": metric.get("node", metric.get("instance", "unknown")),
        "instance": metric.get("instance", "unknown"),
        "mountpoint": metric.get("mountpoint", "unknown"),
        "fstype": metric.get("fstype", "unknown"),
        "used_bytes": int(_prom_sample_to_bytes(sample)),
      }
    )

  return {
    "namespace_pvc_usage": namespace_rows[:max(1, top_k)],
    "top_pvcs": pvc_rows[:max(1, top_k)],
    "top_node_filesystems": node_rows[:max(1, top_k)],
  }


@app.tool()
def get_bandwidth_top_talkers(window: str = "10m", top_k: int = 10) -> dict:
  """Top pods by aggregate RX+TX bandwidth from Prometheus."""
  if not re.match(r"^[0-9]+[smhdw]$", window):
    window = "10m"

  query = (
    f"topk({max(1, top_k)},"
    "sum by (namespace,pod) ("
    f"rate(container_network_receive_bytes_total[{window}]) + "
    f"rate(container_network_transmit_bytes_total[{window}])"
    "))"
  )

  results = _prom_query(query)
  items = []
  for r in results:
    metric = r.get("metric", {})
    value = r.get("value", [None, "0"])[1]
    items.append(
      {
        "namespace": metric.get("namespace", "unknown"),
        "pod": metric.get("pod", "unknown"),
        "bytes_per_sec": float(value),
      }
    )

  return {"window": window, "top_talkers": items}


if __name__ == "__main__":
  app.run(transport="streamable-http", host="0.0.0.0", port=8080)
