from caal.lmstudio_model import reload_local_lmstudio_model


class Response:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_remote_endpoint_is_never_reloaded(monkeypatch):
    monkeypatch.setattr(
        "caal.lmstudio_model.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert reload_local_lmstudio_model("https://example.com/v1", "model") is False


def test_unloads_alias_and_reloads_model_key(monkeypatch):
    posts = []
    get_responses = iter(
        [
            Response(
                {
                    "models": [
                        {
                            "key": "generic-model",
                            "loaded_instances": [{"id": "caal-instance"}],
                        }
                    ]
                }
            ),
            Response({"data": [{"id": "caal-instance"}]}),
        ]
    )
    monkeypatch.setattr(
        "caal.lmstudio_model.requests.get", lambda *args, **kwargs: next(get_responses)
    )

    def post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr("caal.lmstudio_model.requests.post", post)
    commands = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(command, **kwargs):
        commands.append(command)
        return Result()

    monkeypatch.setattr("caal.lmstudio_model.subprocess.run", run)
    assert reload_local_lmstudio_model(
        "http://127.0.0.1:1234/v1",
        "caal-instance",
        lms_bin="/opt/lms",
        context_length=8192,
    )
    assert posts == [
        (
            "http://127.0.0.1:1234/api/v1/models/unload",
            {"instance_id": "caal-instance"},
        ),
    ]
    assert commands == [
        [
            "/opt/lms",
            "load",
            "generic-model",
            "--context-length",
            "8192",
            "--parallel",
            "4",
            "--identifier",
            "caal-instance",
            "--yes",
        ]
    ]
