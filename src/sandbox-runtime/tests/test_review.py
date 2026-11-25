import json

from doppel_sandbox.review import Job, extract_json, stamp

JOB = Job(reviewer="steven", pr=7, head_sha="abc1234", workdir="/work/abc1234", gateway_url="http://gateway:8700")


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"verdict": "clean"}\n```') == {"verdict": "clean"}
    assert extract_json('Here you go: {"a": {"b": 1}} thanks') == {"a": {"b": 1}}
    assert extract_json("no json") is None
    assert extract_json(None) is None


def test_the_runner_stamps_who_reviewed_what_not_the_model():
    verdict = stamp({"doppel": "someone-else", "pr": 99, "head_sha": "zzz", "verdict": "clean"}, JOB, "gpt-x")
    assert (verdict["doppel"], verdict["pr"], verdict["head_sha"], verdict["engine"]) == (
        "steven",
        7,
        "abc1234",
        "gpt-x",
    )


def test_a_job_file_holds_no_capability(tmp_path):
    (tmp_path / "job.json").write_text(json.dumps(JOB.__dict__))
    assert Job.load(tmp_path) == JOB
    assert "capability" not in json.dumps(JOB.__dict__).lower()


def test_a_verdict_may_say_which_earlier_findings_this_commit_fixes():
    from doppel_shared.verdict_schema import problems

    verdict = {"doppel": "security", "pr": 7, "head_sha": "a" * 40, "engine": "codex", "verdict": "clean",
               "summary": "Fixed.", "findings": [], "scope": {"files_reviewed": 1},
               "fixed": ["3a2750e8-F1"]}  # fmt: skip
    assert problems(verdict) == []
    assert problems({**verdict, "fixed": [1]}) != []


def test_a_job_file_carries_the_model_and_effort(tmp_path):
    job = Job(**{**JOB.__dict__, "model": "gpt-6-sol", "effort": "high"})
    (tmp_path / "job.json").write_text(json.dumps(job.__dict__))
    assert Job.load(tmp_path) == job


async def test_codex_refusing_the_model_reviews_again_on_its_defaults(monkeypatch, tmp_path):
    from doppel_sandbox import review as module

    tried = []

    async def fake(job, folder, capability, model, effort):
        tried.append((model, effort))
        if model:
            raise RuntimeError("model not available on this plan")
        return {"verdict": "clean"}

    monkeypatch.setattr(module, "_review", fake)
    job = Job(**{**JOB.__dict__, "model": "gpt-6-sol", "effort": "high"})
    assert await module.review(job, tmp_path, "cap") == {"verdict": "clean"}
    assert tried == [("gpt-6-sol", "high"), (None, None)]


async def test_an_answer_that_never_fits_isnt_retried_on_the_defaults(monkeypatch, tmp_path):
    import pytest
    from doppel_sandbox import review as module

    tried = []

    async def fake(job, folder, capability, model, effort):
        tried.append(model)
        raise module.NotAVerdict("no valid verdict")

    monkeypatch.setattr(module, "_review", fake)
    with pytest.raises(module.NotAVerdict):
        await module.review(Job(**{**JOB.__dict__, "model": "gpt-6-sol"}), tmp_path, "cap")
    assert tried == ["gpt-6-sol"]
