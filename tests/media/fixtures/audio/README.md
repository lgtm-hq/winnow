# Audio fixtures

Small files for the ffprobe audio reader tests (issue #400). Every file is written by
`ffmpeg` from a synthetic source by `generate_audio_fixtures.py`:

```sh
uv run python tests/media/fixtures/audio/generate_audio_fixtures.py
uv run python tests/media/fixtures/audio/record_ffprobe_outputs.py
```

The true values below come from the generation parameters in that script (`FIXTURES`,
`INCONSISTENT_FIXTURES`, `FRAME_SOURCES`, `FRAME_CUTS`), never from a reader. The tests
read them from the same table and compare what the reader reports with them: a duration
within 0.07 s (encoder delay and padding; the largest is `mp3.flv`, about 63 ms), a
bitrate within 5 % of the nominal one where the parameters fix it.

The script runs `ffmpeg` and `ffprobe` through winnow's own runner. With the same FFmpeg
build (9.0.2 here) it reproduces every committed file byte for byte; `mp2.m2ts` pins
`-mpegts_m2ts_mode 0` because the muxer would otherwise pick M2TS mode from the
extension.

Most sources are silence, which keeps lossless files to a few KB while the container
still has one packet per frame. Durations and packet sizes are chosen so that cutting 2
% off a file removes more than one packet: a smaller loss leaves no evidence (see the
known limits in `winnow/media/audio.py`). For that reason the AIFF fixture uses IMA
ADPCM (PCM AIFF packets are 4 KB), the MP3s are 6 s at 8 kbit/s, the WMA uses 200-byte
ASF packets, and `alac.m4a` and `delayed.mp4` have their index at the start. `aac.m4a`
keeps its index at the end and uses a tone so its audio data outweighs the index.

## True values

"Duration" is the length of the source. "Kind" says what the reader must report:
`declared` (checked by the packet walk, `truncated=false`) or `estimated` (measured from
packets, truncation not checked).

| File                 | `format_name`             | Codec          | Rate  | Ch  | Bits | Duration | Kind      |
| -------------------- | ------------------------- | -------------- | ----- | --- | ---- | -------- | --------- |
| `mp3.mp3`            | `mp3`                     | `mp3`          | 24000 | 1   | -    | 6.0      | declared  |
| `adts.aac`           | `aac`                     | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `ac3.ac3`            | `ac3`                     | `ac3`          | 48000 | 2   | -    | 1.0      | estimated |
| `eac3.eac3`          | `eac3`                    | `eac3`         | 48000 | 2   | -    | 1.0      | estimated |
| `flac.flac`          | `flac`                    | `flac`         | 8000  | 1   | 16   | 2.0      | declared  |
| `wavpack.wv`         | `wv`                      | `wavpack`      | 8000  | 1   | 16   | 2.0      | declared  |
| `tta.tta`            | `tta`                     | `tta`          | 8000  | 1   | 16   | 2.0      | declared  |
| `wav.wav`            | `wav`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | declared  |
| `aiff.aiff`          | `aiff`                    | `adpcm_ima_qt` | 8000  | 1   | -    | 2.0      | declared  |
| `aac.m4a`            | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `opus.ogg`           | `ogg`                     | `opus`         | 48000 | 1   | -    | 2.0      | estimated |
| `wma.wma`            | `asf`                     | `wmav2`        | 22050 | 1   | -    | 3.0      | declared  |
| `opus.mka`           | `matroska,webm`           | `opus`         | 48000 | 1   | -    | 2.0      | estimated |
| `pcm.avi`            | `avi`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `mp2.m2ts`           | `mpegts`                  | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `mp2.mpg`            | `mpeg`                    | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `mp3.flv`            | `flv`                     | `mp3`          | 22050 | 1   | -    | 2.0      | estimated |
| `mp2_named.mp3`      | `mp3`                     | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `alac.m4a`           | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 8000  | 1   | 16   | 2.0      | declared  |
| `adpcm_ms.wav`       | `wav`                     | `adpcm_ms`     | 8000  | 1   | -    | 2.0      | declared  |
| `ac3_mono.ac3`       | `ac3`                     | `ac3`          | 48000 | 1   | -    | 1.0      | estimated |
| `flac24.flac`        | `flac`                    | `flac`         | 8000  | 1   | 24   | 2.0      | declared  |
| `two_tracks.mka`     | `matroska,webm`           | `flac`         | 16000 | 2   | 16   | 2.0      | estimated |
| `cover.mp3`          | `mp3`                     | `mp3`          | 24000 | 1   | -    | 6.0      | declared  |
| `delayed.mkv`        | `matroska,webm`           | `opus`         | 48000 | 1   | -    | 1.0      | estimated |
| `delayed.mp4`        | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 1.0      | estimated |
| `delayed.avi`        | `avi`                     | `mp3`          | 24000 | 1   | -    | 1.0      | estimated |
| `video.wmv`          | `asf`                     | `wmav2`        | 22050 | 1   | -    | 1.0      | estimated |
| `vbr_noxing.mp3`     | `mp3`                     | `mp3`          | 44100 | 1   | -    | 2.0      | estimated |
| `cover.m4a`          | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `timecode.mov`       | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `offset.mka`         | `matroska,webm`           | `flac`         | 8000  | 1   | 16   | 2.0      | estimated |
| `offset_delayed.mkv` | `matroska,webm`           | `flac`         | 8000  | 1   | 16   | 1.0      | estimated |
| `delayed_audio.avi`  | `avi`                     | `mp3`          | 24000 | 1   | -    | 1.0      | estimated |
| `delayed_audio.wma`  | `asf`                     | `wmav2`        | 22050 | 1   | -    | 1.0      | estimated |
| `delayed_pcm.mkv`    | `matroska,webm`           | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `late_pcm.mka`       | `matroska,webm`           | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `frames.mp3`         | `mp3`                     | `mp3`          | 44100 | 1   | -    | 2.0      | declared  |
| `frames.m4a`         | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `latin1_tag.wav`     | `wav`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | declared  |
| `latin1_tag.aiff`    | `aiff`                    | `adpcm_ima_qt` | 8000  | 1   | -    | 2.0      | declared  |
| `latin1_tag.avi`     | `avi`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `delay_moov.mp4`     | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `vorbis.mka`         | `matroska,webm`           | `vorbis`       | 48000 | 2   | -    | 2.0      | estimated |
| `wma.mka`            | `matroska,webm`           | `wmav2`        | 22050 | 1   | -    | 2.0      | estimated |
| `mp2.mka`            | `matroska,webm`           | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `ac3.mka`            | `matroska,webm`           | `ac3`          | 32000 | 1   | -    | 2.0      | estimated |
| `eac3.mka`           | `matroska,webm`           | `eac3`         | 32000 | 1   | -    | 2.0      | estimated |
| `mp3_frag.mp4`       | `mov,mp4,m4a,3gp,3g2,mj2` | `mp3`          | 48000 | 1   | -    | 2.0      | estimated |

Notes:

- `two_tracks.mka` has two FLAC tracks: 8000 Hz mono (index 0, not default) and 16000 Hz
  stereo (index 1, default). The reader must pick index 1 and report two audio streams.
  It is `estimated` because the default track declares no duration of its own and the
  file has two streams.
- `cover.mp3` carries an 8x8 PNG as an attached picture; it is not a stream to read and
  does not count as a second stream.
- `delayed.mkv` and `delayed.mp4` hold a 2 s video track and 1 s of audio that starts at
  1 s. Audio that starts late declares nothing (amendment A9), so both are measured; the
  Matroska audio's `DURATION` tag is never used either (amendment A5).
- `delayed.avi` holds a 2 s MPEG-4 video and 1 s of MP3 audio from 1 s. `video.wmv`
  holds a 2 s WMV2 video and 1 s of WMA audio. In both the audio stream's duration field
  is not about the audio alone (it includes the delay, or is the whole file's length).
  AVI never declares a duration (amendment A9b); ASF declares nothing when another
  stream is present (amendment A8). Both are measured; before these rules both were
  reported `truncated`.
- `delayed_audio.avi` (MP3) and `delayed_audio.wma` (WMA) are audio-only and start 1 s
  late. The AVI packets start at 0 and the delay is only in the duration field, so AVI
  never declares a duration (amendment A9b); the ASF packets start at 1 s (amendment
  A9).
- `delayed_pcm.mkv` holds a 2 s video and 1 s of PCM audio from 1 s; `late_pcm.mka` is
  PCM audio alone starting 5 ms late. Their stream duration is the end of the file's
  timeline, so the duration is measured (amendment A9).
- `latin1_tag.wav`, `latin1_tag.aiff` and `latin1_tag.avi` carry a title tag holding the
  Latin-1 bytes `Gr\xfcn`, which are not valid UTF-8. ffprobe's tag output for them is
  not UTF-8 either, which made the reader call them `corrupt` before the header call
  stopped asking for tags (amendment A12a). Each is otherwise the same as `wav.wav`,
  `aiff.aiff` or `pcm.avi`.
- `delay_moov.mp4` (fragmented AAC with `+delay_moov`), `vorbis.mka`, `wma.mka`,
  `mp2.mka`, `ac3.mka` and `eac3.mka` start before zero and declare the packet span,
  which counts the encoder priming, so they are measured (amendment A13a). `opus.mka`
  does the same with its 6.5 ms pre-skip. `mp3_frag.mp4` is MP3 in fragmented MP4: it
  starts at zero with no edit list, so its stream duration counts the LAME priming.
  Before A13a all of them were checked against a duration 8 to 42 ms too long.

## Unsupported and empty fixtures

| File             | ffprobe `format_name`     | Codec       | Rate | Ch  | Bits | Duration | Reported                           |
| ---------------- | ------------------------- | ----------- | ---- | --- | ---- | -------- | ---------------------------------- |
| `au.au`          | `au`                      | `pcm_s16be` | 8000 | 1   | 16   | 1.0      | every fact unknown, `unsupported`  |
| `core_audio.bin` | Core Audio Format         | `pcm_s16le` | 8000 | 1   | 16   | 1.0      | every fact unknown, `unsupported`  |
| `empty.flac`     | `flac`                    | `flac`      | 8000 | 1   | 16   | 0        | facts kept, `inconsistent`         |
| `empty.aiff`     | `aiff`                    | `pcm_s16be` | 8000 | 1   | 16   | 0        | facts kept, `inconsistent`         |
| `empty.m4a`      | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`       | 8000 | 1   | -    | 0        | every fact unknown, `inconsistent` |
| `empty.mov`      | `mov,mp4,m4a,3gp,3g2,mj2` | `pcm_s16le` | 8000 | 1   | 16   | 0        | every fact unknown, `inconsistent` |

- `UNSUPPORTED_FIXTURES` are healthy files in formats outside the allow-list. The header
  call exits non-zero, which says winnow does not accept the format, not that the file
  is damaged (amendment A12b).
- `EMPTY_FIXTURES` are files ffmpeg writes with no audio packet (`-frames:a 0`). The
  FLAC declares no duration and the AIFF declares zero, so nothing positive is declared
  and an empty walk is not `truncated` (amendment A12d).
- `EMPTY_CONTAINER_FIXTURES` are an M4A and a MOV written the same way. ffprobe lists no
  stream at all for them, so every fact is unknown; the reader flags them `inconsistent`
  so the result says why (amendment A13c).

## Fixtures without an established duration

`INCONSISTENT_FIXTURES` are healthy files the reader must never call `truncated`; their
evidence does not establish the duration, so it is unknown with `inconsistent`.

| File               | `format_name`             | Codec | Rate  | Ch  | Duration | Reported                     |
| ------------------ | ------------------------- | ----- | ----- | --- | -------- | ---------------------------- |
| `delayed_frag.mp4` | `mov,mp4,m4a,3gp,3g2,mj2` | `aac` | 48000 | 1   | 1.0      | facts kept, duration unknown |
| `aac51.flv`        | `flv`                     | `aac` | 44100 | 6   | 1.0      | every audio fact unknown     |

- `delayed_frag.mp4` is fragmented MP4 (`+frag_keyframe+empty_moov`) with a 2 s video
  and 1 s of AAC from 0.1 s. The muxer stretches the first audio packet over the delay,
  so a packet is longer than twice the median (amendment A10b).
- `aac51.flv` is 1 s of AAC 5.1 in FLV. Its configuration packet has no time (amendment
  A11).
- `mp2_named.mp3` is MPEG-1 Layer II audio in a raw MPEG audio file.
- `vbr_noxing.mp3` is a VBR MP3 written without a Xing header.
- `chained.ogg` is two 1 s Ogg Opus links (serial numbers 100 and 200) joined back to
  back. The reader must report `inconsistent` with no duration.
- `cover.m4a` carries an 8x8 PNG as `covr` cover art; ffprobe prints
  `stream 0, timescale not set` for it (amendment A3). The index is at the start, so a
  cut removes audio.
- `timecode.mov` has a 2 s video track, the audio, and a `tmcd` timecode track; ffprobe
  prints `Unsupported codec with id 0 for input stream 2` for it (amendment A2). The
  video runs at 25 frame/s: at 5 frame/s the demuxer also warns about the frame rate.
- `offset.mka` is audio-only Matroska starting at 5 s, and `offset_delayed.mkv` a 2 s
  video with 1 s of audio from 1 s, the whole file starting at 0.5 s. Their `DURATION`
  tag and `format.duration` are end times, so the duration is measured (amendment A4).
- `frames.mp3` (CBR 32 kbit/s with an Info header) and `frames.m4a` (AAC 32 kbit/s,
  index first) end in audio data. They are not sized against the 2 % cut and are kept
  out of `FIXTURES`. `frames_minus{1,2,3}.mp3` and `.m4a` are the same files cut at the
  byte offset ffprobe reports for the first of the last one, two, or three audio
  packets; the header still declares 2 s. On FFmpeg 9.0.2 the reader reports
  `truncated=true` for two and three frames and `truncated=false` with 2.0 s for one:
  the encoder's end padding is longer than the frame removed (a known limit).

## Recorded ffprobe output

`recorded/*.json` holds what ffprobe 9.0.2 returned (exit status, stdout, stderr) for
the reader's two calls on selected fixtures, some cut to a fraction of their size, and
on 4 KB of zeros. Tests replay them without ffprobe, so the rules are exercised in CI,
which has no ffprobe. The header call asks only for the fields the rules read
(`-show_entries`, amendment A12a); rerun `record_ffprobe_outputs.py` whenever its
arguments change.
