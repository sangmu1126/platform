"""testing/stats.py: 확정할 수 없는 측정 구간을 자원 표의 근거와 구분한다."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("stats", Path(__file__).resolve().parents[1] / "testing/stats.py")
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)

TARGETS = {"exporter": "bravo-pgbouncer-exporter"}
MiB = 2**20


def crictl(cid, cpu_ns, *, namespace="bravo-observability", pod="bravo-pgbouncer-exporter-5d9c7b-x2k4q", name="exporter"):
    return {
        "attributes": {"id": cid, "labels": {
            "io.kubernetes.container.name": name, "io.kubernetes.pod.namespace": namespace, "io.kubernetes.pod.name": pod}},
        "memory": {"workingSetBytes": {"value": str(20 * MiB)}},
        "cpu": {"usageCoreNanoSeconds": {"value": str(cpu_ns)}},
    }


def samples(*rounds):
    """각 라운드의 crictl 표본 목록 → summarize 입력(5초 간격, anon 8MiB)."""
    return [
        (5.0 * i, {name: [(*r, 8 * MiB) for r in found] for name, found in stats.match(entries, "bravo-observability", TARGETS).items()})
        for i, entries in enumerate(rounds)
    ]


def test_same_container_gives_numbers_from_its_own_counter():
    result = stats.summarize(samples([crictl("a", 0)], [crictl("a", 10_000_000)], [crictl("a", 40_000_000)]))["exporter"]
    assert result["cpu_avg"] == 40_000_000 / 1e9 / 10
    assert result["cpu_max"] == 30_000_000 / 1e9 / 5
    assert result["memory"] == 20 * MiB and result["anon"] == 8 * MiB


def test_pod_replacement_during_window_is_not_a_negative_number():
    # 리뷰 재현: 교체로 누적 CPU가 초기화되어 평균 -77.8m이 정상 결과로 나왔다
    result = stats.summarize(samples([crictl("old", 500_000_000)], [crictl("new", 1_000_000)], [crictl("new", 2_000_000)]))
    assert result["exporter"] == "확정 불가: 측정 중 컨테이너 교체"


def test_same_name_in_another_namespace_or_workload_is_not_the_target():
    other_ns = crictl("x", 1, namespace="stranger")
    other_workload = crictl("y", 1, pod="debug-exporter-6f7d8c-abcde")
    rows = stats.match([other_ns, other_workload], "bravo-observability", TARGETS)
    assert rows == {"exporter": []}
    result = stats.summarize(samples([other_ns], [other_ns]))
    assert result["exporter"] == "확정 불가: 표본 누락"


def test_no_container_missing_sample_or_two_containers_are_undetermined():
    assert stats.summarize(samples([], []))["exporter"] == "확정 불가: 표본 누락"
    assert stats.summarize(samples([crictl("a", 0)], [], [crictl("a", 5)]))["exporter"] == "확정 불가: 표본 누락"
    assert stats.summarize(samples([crictl("a", 0)]))["exporter"] == "확정 불가: 표본 누락"
    both = [crictl("a", 0), crictl("b", 0, pod="bravo-pgbouncer-exporter-7c6b5a-zz9yx")]
    assert stats.summarize(samples(both, both))["exporter"] == "확정 불가: 대상 컨테이너가 둘 이상"


def test_unreadable_anon_or_shrinking_counter_is_undetermined():
    rows = samples([crictl("a", 0)], [crictl("a", 5)])
    rows[1][1]["exporter"][0] = (*rows[1][1]["exporter"][0][:3], None)
    assert stats.summarize(rows)["exporter"] == "확정 불가: anon 읽기 실패"
    assert stats.summarize(samples([crictl("a", 9)], [crictl("a", 5)]))["exporter"] == "확정 불가: CPU 누적값이 줄어듦"


def test_exit_code_marks_any_undetermined_target(monkeypatch, capsys):
    good = samples([crictl("a", 0)], [crictl("a", 10)])
    monkeypatch.setattr(stats, "collect", lambda *a: good)
    assert stats.main(["node", "10", "5", "bravo-observability", "exporter=bravo-pgbouncer-exporter"]) == 0
    replaced = samples([crictl("old", 9)], [crictl("new", 1)])
    monkeypatch.setattr(stats, "collect", lambda *a: replaced)
    assert stats.main(["node", "10", "5", "bravo-observability", "exporter=bravo-pgbouncer-exporter"]) == 1
    assert "확정 불가: 측정 중 컨테이너 교체" in capsys.readouterr().out
