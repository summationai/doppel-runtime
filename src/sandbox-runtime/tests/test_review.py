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
