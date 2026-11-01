"""Request-level model selection and lazy-loading behavior."""

from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber, make_wav_bytes
from transcription_service.core.config import Settings
from transcription_service.domain import AudioMode
from transcription_service.main import create_app


class LargerTranscriber(FakeTranscriber):
    model_id = "test/larger-whisper"


def test_model_selection_is_lazy_cached_and_independent_of_audio_mode(settings: Settings) -> None:
    small = FakeTranscriber()
    large = LargerTranscriber()
    loaded: list[Settings] = []

    def factory(model_settings: Settings) -> FakeTranscriber:
        loaded.append(model_settings)
        return large if model_settings.model_id == settings.larger_model_id else small

    with TestClient(create_app(settings, transcriber_factory=factory)) as client:
        assert len(loaded) == 1
        for profile in (None, "larger", "larger", "faster"):
            data = {"language": "he", "audio_mode": "music"}
            if profile is not None:
                data["model_profile"] = profile
            response = client.post(
                "/v1/transcriptions",
                files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
                data=data,
            )
            assert response.status_code == 200
            expected = large if profile == "larger" else small
            assert response.json()["model"] == expected.model_id
            assert response.json()["text"] == "שלום עולם"
            if profile is None:
                assert len(loaded) == 1

        assert len(loaded) == 2
        assert loaded[1].model_id == settings.larger_model_id
        assert loaded[1].model_revision == settings.larger_model_revision
        assert large.requested_audio_modes == [AudioMode.MUSIC, AudioMode.MUSIC]
        assert small.requested_audio_modes == [AudioMode.MUSIC, AudioMode.MUSIC]


def test_rejects_arbitrary_model_choices_without_loading_them(settings: Settings) -> None:
    loaded: list[str] = []

    def factory(model_settings: Settings) -> FakeTranscriber:
        loaded.append(model_settings.model_id)
        return FakeTranscriber()

    with TestClient(create_app(settings, transcriber_factory=factory)) as client:
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
            data={"model_profile": "untrusted/arbitrary-model"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert loaded == [settings.model_id]


def test_failed_large_model_load_can_retry_and_does_not_break_faster(settings: Settings) -> None:
    attempts = 0

    def factory(model_settings: Settings) -> FakeTranscriber:
        nonlocal attempts
        if model_settings.model_id == settings.larger_model_id:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("private model download error")
            return LargerTranscriber()
        return FakeTranscriber()

    with TestClient(create_app(settings, transcriber_factory=factory)) as client:
        for profile, expected_status in (("larger", 500), ("faster", 200), ("larger", 200)):
            response = client.post(
                "/v1/transcriptions",
                files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
                data={"model_profile": profile},
            )
            assert response.status_code == expected_status
            assert "private model download error" not in response.text
            assert client.get("/health/ready").status_code == 200
        assert attempts == 2


def test_language_is_validated_against_the_selected_model(settings: Settings) -> None:
    class FrenchModel(LargerTranscriber):
        supported_languages = frozenset({"fr"})

    large = FrenchModel()

    def factory(model_settings: Settings) -> FakeTranscriber:
        return large if model_settings.model_id == settings.larger_model_id else FakeTranscriber()

    with TestClient(create_app(settings, transcriber_factory=factory)) as client:
        for language, expected_status in (("fr", 200), ("en", 422)):
            response = client.post(
                "/v1/transcriptions",
                files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
                data={"model_profile": "larger", "language": language},
            )
            assert response.status_code == expected_status
        assert large.requested_languages == ["fr"]
