"""k3s 노드 안 컨테이너 자원 사용량 표본. `crictl stats -o json`을 주기적으로 읽어 컨테이너별 최대 메모리 · 평균 CPU를 낸다.

메모리는 두 가지를 낸다. working set에는 커널이 OOM 전에 회수하는 파일 캐시(디스크 큐 · TSDB)가 섞이므로,
limit을 정할 때는 cgroup memory.stat의 anon(프로세스가 실제로 쥔 메모리)을 함께 본다.

대상은 네임스페이스 · Deployment(Pod 이름) · 컨테이너 이름으로 고른다. 측정 구간 내내 같은 컨테이너 하나만
보였을 때만 수치를 낸다. CPU는 같은 컨테이너 ID의 누적값 차이로만 계산한다(평균은 구간 처음과 끝,
최대는 표본 간격마다). 표본 누락 · 컨테이너 교체 · 대상이 둘 이상이면 수치 대신 "확정 불가"와 사유를 내고
종료 코드 1로 끝난다. 자원 표의 근거로는 종료 코드 0인 실행만 쓴다.

사용: python3 stats.py <k3s 컨테이너 이름> <초> <간격초> <네임스페이스> 컨테이너=Deployment...
"""

import json
import re
import subprocess
import sys
import time
from itertools import pairwise


def node_exec(node: str, *cmd: str) -> str:
    return subprocess.run(["docker", "exec", node, *cmd], capture_output=True, text=True, check=True).stdout


def anon_bytes(node: str, container_id: str) -> int | None:
    try:
        pid = node_exec(node, "crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", container_id).strip()
        cgroup = node_exec(node, "cat", f"/proc/{pid}/cgroup").strip().removeprefix("0::")
        stat = node_exec(node, "cat", f"/sys/fs/cgroup{cgroup}/memory.stat")
    except subprocess.CalledProcessError:
        return None  # 표본과 읽기 사이에 컨테이너가 사라짐
    return next(int(line.split()[1]) for line in stat.splitlines() if line.startswith("anon "))


def match(stats: list, namespace: str, targets: dict) -> dict:
    """crictl stats 한 번 → {컨테이너 이름: [(ID, working set, CPU 누적 ns)]}. 대상 네임스페이스의 그 Deployment Pod만."""
    rows = {name: [] for name in targets}
    for s in stats:
        labels = s["attributes"]["labels"]
        name = labels.get("io.kubernetes.container.name")
        if name not in targets or labels.get("io.kubernetes.pod.namespace") != namespace:
            continue
        # Deployment Pod 이름: <Deployment>-<ReplicaSet 해시>-<Pod 접미>
        if not re.fullmatch(re.escape(targets[name]) + r"-[a-z0-9]+-[a-z0-9]+", labels.get("io.kubernetes.pod.name", "")):
            continue
        rows[name].append(
            (s["attributes"]["id"], int(s["memory"]["workingSetBytes"]["value"]), int(s["cpu"]["usageCoreNanoSeconds"]["value"]))
        )
    return rows


def sample(node: str, namespace: str, targets: dict) -> tuple:
    rows = match(json.loads(node_exec(node, "crictl", "stats", "-o", "json"))["stats"], namespace, targets)
    return time.monotonic(), {name: [(*r, anon_bytes(node, r[0])) for r in found] for name, found in rows.items()}


def summarize(samples: list) -> dict:
    """[(시각, {이름: [(ID, working set, CPU 누적 ns, anon)]})] → {이름: 수치 dict 또는 "확정 불가: 사유"}."""
    results = {}
    for name in samples[0][1]:
        seen = [(t, rows[name]) for t, rows in samples]
        if len(seen) < 2 or any(not found for _, found in seen):
            results[name] = "확정 불가: 표본 누락"
        elif any(len(found) > 1 for _, found in seen):
            results[name] = "확정 불가: 대상 컨테이너가 둘 이상"
        elif len({found[0][0] for _, found in seen}) > 1:
            results[name] = "확정 불가: 측정 중 컨테이너 교체"
        elif any(found[0][3] is None for _, found in seen):
            results[name] = "확정 불가: anon 읽기 실패"
        else:
            points = [(t, found[0]) for t, found in seen]
            rates = [(b[2] - a[2]) / 1e9 / (tb - ta) for (ta, a), (tb, b) in pairwise(points)]
            if any(rate < 0 for rate in rates):
                results[name] = "확정 불가: CPU 누적값이 줄어듦"
                continue
            (t0, first), (t1, last) = points[0], points[-1]
            results[name] = {
                "memory": max(p[1] for _, p in points),
                "anon": max(p[3] for _, p in points),
                "cpu_avg": (last[2] - first[2]) / 1e9 / (t1 - t0),
                "cpu_max": max(rates),
            }
    return results


def collect(node: str, seconds: int, interval: int, namespace: str, targets: dict) -> list:
    samples = [sample(node, namespace, targets)]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(interval)
        samples.append(sample(node, namespace, targets))
    return samples


def main(argv: list) -> int:
    node, seconds, interval, namespace, *specs = argv
    targets = dict(spec.split("=", 1) for spec in specs)
    results = summarize(collect(node, int(seconds), int(interval), namespace, targets))
    for name in sorted(results):
        r = results[name]
        if isinstance(r, str):
            print(f"  {name:12s} {r}")
        else:
            print(
                f"  {name:12s} 최대 메모리 {r['memory'] / 2**20:6.1f}MiB (anon {r['anon'] / 2**20:6.1f}MiB)"
                f"  평균 CPU {r['cpu_avg'] * 1000:6.1f}m  최대 CPU(간격) {r['cpu_max'] * 1000:6.1f}m"
            )
    return 0 if all(isinstance(r, dict) for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
