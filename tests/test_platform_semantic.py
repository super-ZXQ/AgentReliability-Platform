"""平台化阶段项目 4a：OpenJudge 语义评分适配器与计分测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentreliability.semantic import (
    STATE_LABELS_UNREVIEWED,
    STATE_MODEL_CONFIG,
    STATE_PARSE_FAILURE,
    STATE_REVISION_MISMATCH,
    STATE_SAMPLES_CHANGED,
    STATE_TIMEOUT,
    STATE_UPSTREAM_FAILURE,
    load_samples,
    run_semantic_evaluation,
    summarize,
)
from agentreliability.report import sha256_file

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "benchmarks" / "semantic_groundedness"
REAL_OPENJUDGE = REPO_ROOT / ".targets" / "openjudge"


def _fake_sample(**overrides) -> dict:
    sample = {
        "id": "s1",
        "split": "calibration",
        "label": "supported",
        "preferred_label": "supported",
        "diagnosis": "d",
        "observation": "o",
    }
    sample.update(overrides)
    return sample


def _prediction(sample: dict, predicted: str | None, **overrides) -> dict:
    item = {
        "id": sample["id"],
        "label": sample["label"],
        "preferred_label": sample["preferred_label"],
        "predicted": predicted,
        "score": 1.0 if predicted == "supported" else 0.0,
        "reason": "r",
    }
    item.update(overrides)
    return item


def _runner(payload: dict | None, returncode: int = 0, stderr: str = ""):
    def run(command, **kwargs):
        if payload is not None:
            out = Path(command[4])
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    return run


def _timeout_runner(command, **kwargs):
    raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 1))


def _copy_samples(tmp_path: Path, *, human_reviewed: bool = True) -> Path:
    target = tmp_path / "samples"
    shutil.copytree(SAMPLES, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["labels_human_reviewed"] = human_reviewed
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def test_summarize_counts_clear_borderline_and_errors():
    samples = [
        _fake_sample(id="a", label="supported", preferred_label="supported"),
        _fake_sample(id="b", label="unsupported", preferred_label="unsupported"),
        _fake_sample(id="c", label="borderline", preferred_label="supported"),
        _fake_sample(id="d", label="supported", preferred_label="supported"),
    ]
    predictions = [
        _prediction(samples[0], "supported"),
        _prediction(samples[1], "supported"),
        _prediction(samples[2], "supported"),
        _prediction(samples[3], None, error="boom"),
    ]
    metrics = summarize(predictions, samples)
    assert metrics["clear_total"] == 2
    assert metrics["clear_agreements"] == 1
    assert metrics["clear_accuracy"] == pytest.approx(0.5)
    assert metrics["borderline_total"] == 1
    assert metrics["borderline_prediction_distribution"] == {
        "supported": 1, "unsupported": 0
    }
    assert metrics["preferred_total"] == 3
    assert metrics["preferred_agreements"] == 2
    assert len(metrics["misjudgments"]) == 1
    assert len(metrics["disagreements"]) == 1
    assert len(metrics["prediction_errors"]) == 1


def test_samples_freeze_rejects_modified_file(tmp_path):
    target = _copy_samples(tmp_path)
    path = target / "v1.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        load_samples(target)
    assert "校验和不匹配" in str(exc.value)


def test_frozen_samples_match_manifest():
    manifest, samples, path = load_samples(SAMPLES)
    assert manifest["samples_sha256"] == sha256_file(path)
    splits = {item["split"] for item in samples}
    assert splits == {"calibration", "holdout"}
    labels = {item["label"] for item in samples}
    assert labels == {"supported", "unsupported", "borderline"}
    assert manifest["labels_human_reviewed"] is False


def test_holdout_gate_requires_human_reviewed_labels(tmp_path):
    manifest, run_dir = run_semantic_evaluation(
        split="holdout",
        out=tmp_path,
        mode="offline",
        openjudge_dir=REAL_OPENJUDGE,
        environ={},
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_LABELS_UNREVIEWED
    assert manifest["metrics"] == {}
    assert (run_dir / "report.html").exists()


def test_model_mode_without_config_is_not_run(tmp_path):
    manifest, _ = run_semantic_evaluation(
        split="calibration",
        out=tmp_path,
        mode="model",
        openjudge_dir=REAL_OPENJUDGE,
        environ={},
    )
    assert manifest["status"] == "NOT_RUN"
    assert manifest["state"] == STATE_MODEL_CONFIG
    assert "OPENJUDGE_API_KEY" in manifest["error"]


def test_holdout_metrics_with_human_reviewed_fixture(tmp_path):
    samples_dir = _copy_samples(tmp_path, human_reviewed=True)
    manifest, run_dir = run_semantic_evaluation(
        split="holdout",
        out=tmp_path / "out",
        mode="offline",
        openjudge_dir=REAL_OPENJUDGE,
        samples_dir=samples_dir,
        environ={},
    )
    assert manifest["status"] == "PASS"
    assert manifest["samples"]["labels_human_reviewed"] is True
    assert manifest["metrics"]["clear_total"] == 6
    assert manifest["metrics"]["borderline_total"] == 2
    assert manifest["execution_mode"] == "test-double"
    assert manifest["hard_gate"] is False
    assert (run_dir / "predictions.json").exists()


def test_real_openjudge_offline_calibration(tmp_path):
    if not (REAL_OPENJUDGE / ".venv" / "bin" / "python").exists():
        pytest.skip("本机缺少 OpenJudge 独立 venv")
    manifest, run_dir = run_semantic_evaluation(
        split="calibration",
        out=tmp_path,
        mode="offline",
        openjudge_dir=REAL_OPENJUDGE,
        environ={},
    )
    assert manifest["status"] == "PASS"
    assert manifest["grader"]["commit_verified"] is True
    assert manifest["execution_mode"] == "test-double"
    metrics = manifest["metrics"]
    assert metrics["clear_total"] == 7
    assert 0.0 <= metrics["clear_accuracy"] <= 1.0
    assert isinstance(metrics["disagreements"], list)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "/Users/" not in html
    assert "独立展示" in html


def test_revision_mismatch_is_reported(tmp_path):
    checkout = tmp_path / "openjudge"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=checkout, check=True)
    (checkout / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=checkout, check=True)
    manifest, _ = run_semantic_evaluation(
        split="calibration", out=tmp_path / "out", openjudge_dir=checkout, environ={}
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_REVISION_MISMATCH


def test_upstream_failure_and_timeout(tmp_path):
    manifest, _ = run_semantic_evaluation(
        split="calibration",
        out=tmp_path / "one",
        openjudge_dir=REAL_OPENJUDGE,
        runner=_runner(None, returncode=1, stderr="boom"),
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_UPSTREAM_FAILURE
    manifest, _ = run_semantic_evaluation(
        split="calibration",
        out=tmp_path / "two",
        openjudge_dir=REAL_OPENJUDGE,
        runner=_timeout_runner,
        environ={},
    )
    assert manifest["state"] == STATE_TIMEOUT


def test_parse_failure_when_predictions_missing(tmp_path):
    manifest, _ = run_semantic_evaluation(
        split="calibration",
        out=tmp_path,
        openjudge_dir=REAL_OPENJUDGE,
        runner=_runner({"status": "PASS", "predictions": "nope"}),
        environ={},
    )
    assert manifest["state"] == STATE_PARSE_FAILURE


def test_samples_changed_state_written(tmp_path):
    target = _copy_samples(tmp_path)
    (target / "v1.json").write_text(
        json.dumps({"version": "v1", "samples": []}), encoding="utf-8"
    )
    manifest, _ = run_semantic_evaluation(
        split="calibration",
        out=tmp_path / "out",
        openjudge_dir=REAL_OPENJUDGE,
        samples_dir=target,
        environ={},
    )
    assert manifest["status"] == "ERROR"
    assert manifest["state"] == STATE_SAMPLES_CHANGED
