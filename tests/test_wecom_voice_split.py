# encoding:utf-8

import os

from voice import audio_convert


class FakeSegment:
    def __init__(self, duration_ms, bytes_per_ms=40):
        self.duration_ms = int(duration_ms)
        self.bytes_per_ms = int(bytes_per_ms)

    def __len__(self):
        return self.duration_ms

    def set_frame_rate(self, _rate):
        return self

    def set_channels(self, _channels):
        return self

    def __getitem__(self, item):
        start = int(item.start or 0)
        stop = int(item.stop if item.stop is not None else self.duration_ms)
        return FakeSegment(max(0, stop - start), self.bytes_per_ms)

    def export(self, path, format="amr"):
        with open(path, "wb") as handle:
            handle.write(b"#!AMR\n")
            handle.write(b"a" * max(0, self.duration_ms * self.bytes_per_ms))


class FakeAudioSegment:
    @staticmethod
    def from_file(path, parameters=None):
        return FakeSegment(120000, bytes_per_ms=1)


def test_split_audio_outputs_amr_under_wecom_limits(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fake")
    monkeypatch.setattr(audio_convert, "_pydub_available", True)
    monkeypatch.setattr(audio_convert, "AudioSegment", FakeAudioSegment)
    monkeypatch.setattr(audio_convert, "conf", lambda: {})

    duration_ms, paths = audio_convert.split_audio_by_wecom_voice_limits(
        str(source),
        output_dir=str(tmp_path),
        max_seconds=55,
        max_bytes=1900000,
    )

    assert duration_ms == 120000
    assert len(paths) == 3
    for path in paths:
        assert path.endswith(".amr")
        assert 5 < os.path.getsize(path) <= 1900000


def test_split_audio_shortens_when_55_seconds_exceeds_size(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fake")

    class LargeFakeAudioSegment:
        @staticmethod
        def from_file(path, parameters=None):
            return FakeSegment(55000, bytes_per_ms=100)

    monkeypatch.setattr(audio_convert, "_pydub_available", True)
    monkeypatch.setattr(audio_convert, "AudioSegment", LargeFakeAudioSegment)
    monkeypatch.setattr(audio_convert, "conf", lambda: {})

    _duration_ms, paths = audio_convert.split_audio_by_wecom_voice_limits(
        str(source),
        output_dir=str(tmp_path),
        max_seconds=55,
        max_bytes=1900000,
    )

    assert len(paths) > 1
    assert all(5 < os.path.getsize(path) <= 1900000 for path in paths)
