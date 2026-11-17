# Audio fixtures

Small synthetic audio files for `tests/media/test_audio.py` and
`tests/media/test_audio_content.py`. They are committed so the tinytag path is tested
everywhere, including CI runners without FFmpeg. No test writes audio at test time.

## How they were made

Every file is a 0.3 s, 440 Hz sine tone from FFmpeg's `lavfi` `sine` source, written by
`generate_audio_fixtures.py` in this directory with FFmpeg 9.0.2 (`Lavf63.1.102`). Each
file carries the tags `title=Tone`, `artist=Winnow`, `album=Samples`, `date=2019-07-04`
and `track=3/9`, as far as its format stores them. The files hold nothing but the tone
and those tags.

| File        | Source          | FFmpeg codec arguments                       |
| ----------- | --------------- | -------------------------------------------- |
| `tone.mp3`  | 44.1 kHz stereo | `-c:a libmp3lame -b:a 64k`                   |
| `tone.flac` | 44.1 kHz stereo | `-c:a flac`                                  |
| `tone.wav`  | 8 kHz mono      | `-c:a pcm_s16le`                             |
| `aac.m4a`   | 44.1 kHz stereo | `-c:a aac -b:a 64k`                          |
| `alac.m4a`  | 8 kHz mono      | `-c:a alac`                                  |
| `tone.opus` | 48 kHz stereo   | `-c:a libopus -b:a 64k`                      |
| `tone.aiff` | 8 kHz mono      | `-c:a pcm_s16be`                             |
| `tone.aac`  | 44.1 kHz stereo | `-c:a aac -b:a 64k -f adts` (raw ADTS)       |
| `adpcm.wav` | 8 kHz mono      | `-c:a adpcm_ms` (MS ADPCM in WAV)            |
| `flac.oga`  | 8 kHz mono      | `-c:a flac -f ogg` (FLAC in Ogg)             |
| `ima4.aifc` | 8 kHz mono      | `-c:a adpcm_ima_qt -f aiff` (IMA4 in AIFF-C) |
| `tone.wma`  | 8 kHz mono      | `-c:a wmav2 -b:a 32k`                        |
| `tone.ac3`  | 8 kHz mono      | `-c:a ac3 -ar 32000 -b:a 64k`                |
| `tone.tta`  | 8 kHz mono      | `-c:a tta`                                   |
| `tone.wv`   | 8 kHz mono      | `-c:a wavpack`                               |

Each file is under 20 kB (`test_committed_fixtures_are_small`).

## Regenerating

```bash
uv run python tests/media/fixtures/audio/generate_audio_fixtures.py
```

Another FFmpeg build writes different encoder strings and can shift durations or
bitrates slightly. Regenerate only together with the recorded rows in
`tests/media/test_audio.py` (`_COMMITTED_EXPECTED` and `_COMMITTED_TAGS`), whose
comments also record what `main` (mutagen) read from these exact files.
