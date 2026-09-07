"""Per-job cron hard ceilings and deterministic completion hooks."""

import json
from unittest.mock import Mock

import pytest

from cron.jobs import create_job, load_jobs, update_job


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path / "cron"


def _create(**kwargs):
    kwargs.setdefault("prompt", "bounded task")
    kwargs.setdefault("schedule", "every 1h")
    return create_job(**kwargs)


def test_positive_pin_round_trips_and_update_can_clear(tmp_cron_dir):
    job = _create(max_turns="24")
    assert job["max_turns"] == 24
    assert load_jobs()[0]["max_turns"] == 24
    updated = update_job(job["id"], {"max_turns": "default"})
    assert updated["max_turns"] is None


@pytest.mark.parametrize("invalid", [True, 0, -1, "unlimited", "3.5", "many"])
def test_invalid_pin_is_rejected_without_persisting(tmp_cron_dir, invalid):
    with pytest.raises(ValueError, match="max_turns"):
        _create(max_turns=invalid)
    assert load_jobs() == []


def test_per_job_pin_beats_global_limit():
    from cron.scheduler import _resolve_job_max_iterations

    assert _resolve_job_max_iterations(
        {"id": "bounded", "max_turns": 14},
        {"agent": {"max_turns": 60}},
    ) == 14


def test_absent_pin_preserves_global_limit():
    from cron.scheduler import _resolve_job_max_iterations

    assert _resolve_job_max_iterations({}, {"agent": {"max_turns": 60}}) == 60


def test_invalid_stored_pin_fails_closed_before_agent_run():
    from cron.scheduler import _resolve_job_max_iterations

    with pytest.raises(RuntimeError, match="refusing to start an unbounded"):
        _resolve_job_max_iterations(
            {"id": "corrupt", "max_turns": "unlimited"},
            {"agent": {"max_turns": None}},
        )


def test_completion_hook_runs_after_hard_limit(monkeypatch):
    from cron.scheduler import _run_completion_script

    runner = Mock(return_value=(True, "PUBLISH_ATTEMPT=immutable_existing"))
    monkeypatch.setattr("cron.scheduler._run_job_script_with_claim_heartbeat", runner)
    result = {
        "api_calls": 14,
        "completed": False,
        "turn_exit_reason": "max_iterations_reached(14/14)",
    }
    output = _run_completion_script(
        {"id": "bounded", "completion_script": "finish.py"},
        result,
        workdir="/tmp/project",
        cancel_event=None,
    )
    assert output == "PUBLISH_ATTEMPT=immutable_existing"
    assert result["completion_script"]["success"] is True
    runner.assert_called_once()


def test_completion_hook_failure_fails_closed(monkeypatch):
    from cron.scheduler import _run_completion_script

    monkeypatch.setattr(
        "cron.scheduler._run_job_script_with_claim_heartbeat",
        Mock(return_value=(False, "publisher failed")),
    )
    with pytest.raises(RuntimeError, match="completion script failed"):
        _run_completion_script(
            {"id": "bounded", "completion_script": "finish.py"},
            {"api_calls": 14},
            workdir="/tmp/project",
            cancel_event=None,
        )


def test_cli_lane_updates_and_surfaces_pin(tmp_cron_dir):
    from tools.cronjob_tools import cronjob

    job = _create()
    updated = json.loads(cronjob(action="update", job_id=job["id"], max_turns="14"))
    assert updated["success"] is True
    assert updated["job"]["max_turns"] == 14


def test_model_tool_schema_cannot_set_runtime_guards():
    import inspect
    import tools.cronjob_tools as module

    properties = module.CRONJOB_SCHEMA["parameters"]["properties"]
    assert "max_turns" not in properties
    assert "completion_script" not in properties
    assert 'args.get("max_turns")' not in inspect.getsource(module._cronjob_handler)
    assert 'args.get("completion_script")' not in inspect.getsource(module._cronjob_handler)
