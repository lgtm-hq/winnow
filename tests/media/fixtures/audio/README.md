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

| File                      | `format_name`             | Codec          | Rate  | Ch  | Bits | Duration | Kind      |
| ------------------------- | ------------------------- | -------------- | ----- | --- | ---- | -------- | --------- |
| `mp3.mp3`                 | `mp3`                     | `mp3`          | 24000 | 1   | -    | 6.0      | declared  |
| `adts.aac`                | `aac`                     | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `ac3.ac3`                 | `ac3`                     | `ac3`          | 48000 | 2   | -    | 1.0      | estimated |
| `eac3.eac3`               | `eac3`                    | `eac3`         | 48000 | 2   | -    | 1.0      | estimated |
| `flac.flac`               | `flac`                    | `flac`         | 8000  | 1   | 16   | 2.0      | declared  |
| `wavpack.wv`              | `wv`                      | `wavpack`      | 8000  | 1   | 16   | 2.0      | declared  |
| `tta.tta`                 | `tta`                     | `tta`          | 8000  | 1   | 16   | 2.0      | declared  |
| `wav.wav`                 | `wav`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | declared  |
| `aiff.aiff`               | `aiff`                    | `adpcm_ima_qt` | 8000  | 1   | -    | 2.0      | declared  |
| `aac.m4a`                 | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `opus.ogg`                | `ogg`                     | `opus`         | 48000 | 1   | -    | 2.0      | estimated |
| `wma.wma`                 | `asf`                     | `wmav2`        | 22050 | 1   | -    | 3.0      | declared  |
| `opus.mka`                | `matroska,webm`           | `opus`         | 48000 | 1   | -    | 2.0      | estimated |
| `pcm.avi`                 | `avi`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `mp2.m2ts`                | `mpegts`                  | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `mp2.mpg`                 | `mpeg`                    | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `mp3.flv`                 | `flv`                     | `mp3`          | 22050 | 1   | -    | 2.0      | estimated |
| `mp2_named.mp3`           | `mp3`                     | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `alac.m4a`                | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 8000  | 1   | 16   | 2.0      | declared  |
| `adpcm_ms.wav`            | `wav`                     | `adpcm_ms`     | 8000  | 1   | -    | 2.0      | declared  |
| `ac3_mono.ac3`            | `ac3`                     | `ac3`          | 48000 | 1   | -    | 1.0      | estimated |
| `flac24.flac`             | `flac`                    | `flac`         | 8000  | 1   | 24   | 2.0      | declared  |
| `two_tracks.mka`          | `matroska,webm`           | `flac`         | 16000 | 2   | 16   | 2.0      | estimated |
| `cover.mp3`               | `mp3`                     | `mp3`          | 24000 | 1   | -    | 6.0      | declared  |
| `delayed.mkv`             | `matroska,webm`           | `opus`         | 48000 | 1   | -    | 1.0      | estimated |
| `delayed.mp4`             | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 1.0      | estimated |
| `delayed.avi`             | `avi`                     | `mp3`          | 24000 | 1   | -    | 1.0      | estimated |
| `video.wmv`               | `asf`                     | `wmav2`        | 22050 | 1   | -    | 1.0      | estimated |
| `vbr_noxing.mp3`          | `mp3`                     | `mp3`          | 44100 | 1   | -    | 2.0      | estimated |
| `cover.m4a`               | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `timecode.mov`            | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `offset.mka`              | `matroska,webm`           | `flac`         | 8000  | 1   | 16   | 2.0      | estimated |
| `offset_delayed.mkv`      | `matroska,webm`           | `flac`         | 8000  | 1   | 16   | 1.0      | estimated |
| `delayed_audio.avi`       | `avi`                     | `mp3`          | 24000 | 1   | -    | 1.0      | estimated |
| `delayed_audio.wma`       | `asf`                     | `wmav2`        | 22050 | 1   | -    | 1.0      | estimated |
| `delayed_pcm.mkv`         | `matroska,webm`           | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `late_pcm.mka`            | `matroska,webm`           | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `frames.mp3`              | `mp3`                     | `mp3`          | 44100 | 1   | -    | 2.0      | declared  |
| `frames.m4a`              | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | declared  |
| `latin1_tag.wav`          | `wav`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | declared  |
| `latin1_tag.aiff`         | `aiff`                    | `adpcm_ima_qt` | 8000  | 1   | -    | 2.0      | declared  |
| `latin1_tag.avi`          | `avi`                     | `pcm_u8`       | 8000  | 1   | 8    | 1.0      | estimated |
| `delay_moov.mp4`          | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `vorbis.mka`              | `matroska,webm`           | `vorbis`       | 48000 | 2   | -    | 2.0      | estimated |
| `wma.mka`                 | `matroska,webm`           | `wmav2`        | 22050 | 1   | -    | 2.0      | estimated |
| `mp2.mka`                 | `matroska,webm`           | `mp2`          | 48000 | 1   | -    | 2.0      | estimated |
| `ac3.mka`                 | `matroska,webm`           | `ac3`          | 32000 | 1   | -    | 2.0      | estimated |
| `eac3.mka`                | `matroska,webm`           | `eac3`         | 32000 | 1   | -    | 2.0      | estimated |
| `mp3_frag.mp4`            | `mov,mp4,m4a,3gp,3g2,mj2` | `mp3`          | 48000 | 1   | -    | 2.0      | estimated |
| `aac_frag.mp4`            | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `vorbis_frag.mp4`         | `mov,mp4,m4a,3gp,3g2,mj2` | `vorbis`       | 48000 | 2   | -    | 2.0      | estimated |
| `opus_frag.mp4`           | `mov,mp4,m4a,3gp,3g2,mj2` | `opus`         | 48000 | 1   | -    | 2.0      | estimated |
| `aac_fragments.mp4`       | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`          | 48000 | 1   | -    | 2.0      | estimated |
| `alac_fragments.mp4`      | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 48000 | 1   | 16   | 2.0      | estimated |
| `pcm_moov_fragments.mp4`  | `mov,mp4,m4a,3gp,3g2,mj2` | `pcm_s16le`    | 8000  | 1   | 16   | 1.0      | estimated |
| `alac_moov_fragments.mp4` | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 48000 | 1   | 16   | 1.0      | estimated |
| `alac_indexed.mp4`        | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 48000 | 1   | 16   | 2.0      | estimated |
| `alac_bframes.mp4`        | `mov,mp4,m4a,3gp,3g2,mj2` | `alac`         | 8000  | 1   | 16   | 2.0      | estimated |

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
- `aac_frag.mp4`, `vorbis_frag.mp4` and `opus_frag.mp4` are AAC, Vorbis and Opus in
  fragmented MP4. Like `mp3_frag.mp4` they start at zero with no edit list, so the
  packets hold the priming and the stream duration equals their span: 21 ms over for AAC
  and Vorbis, 6.5 ms for Opus. Nothing is declared and the span is measured (amendment
  A14a). Before A14a, Vorbis and Opus were checked against that duration. A 2 % cut of
  `aac_frag.mp4` removes only the fragment index at the end, not audio, so it is still
  `estimated` with the full length.
- `aac_fragments.mp4` and `alac_fragments.mp4` are fragmented MP4 in four 0.5 s
  fragments (`+empty_moov -frag_duration 500000`) with no fragment index besides the
  trailing `mfra`. `alac_indexed.mp4` is the ALAC file with a `sidx` index at the front
  (`+global_sidx+default_base_moof`; without `default_base_moof` the inserted `sidx`
  shifts the data away from the offsets the fragments record, and FFmpeg 9.0.2 cannot
  decode the file). ffprobe shows no difference between an indexed and an unindexed
  file, so every fragmented MP4 is measured, lossless included (amendment A15a). Before
  A15a the ALAC files were checked.
- `pcm_moov_fragments.mp4` (8 kHz) and `alac_moov_fragments.mp4` (48 kHz) are 1 s in
  four 0.25 s fragments written with `-frag_duration 250000` and no `empty_moov`, so the
  first fragment sits inside `moov`. Their `nb_frames` counts that fragment's MP4
  samples: 3 packets for ALAC, but 2,048 audio samples for PCM, more than the 8 packets
  the walk finds. The PCM file is then told fragmented by its span in samples (8,000)
  against `nb_frames` (amendment A15f-1). Before A15f-1 the PCM file and its cut were
  `truncated=false`.
- `alac_bframes.mp4` is 2 s of ALAC at 8 kHz beside a 25 frame/s H.264 video with one
  B-frame, written with `-use_editlist 0`. The muxer stretches the first audio packet
  from 512 ms to 552 ms by the video's 40 ms delay. That is under twice the median, so
  A10b does not see it, but more than the A5 tolerance over the median, so the duration
  is measured: 2.04 s (amendment A15b). Before A15b it was checked at 2.04 s.

## HE-AAC fixture

`heaac.m4a` is 2 s of stereo silence at 48 kHz encoded as HE-AAC by Apple's AudioToolbox
encoder (`aac_at -profile:a 4`) into faststart M4A. ffprobe reports `profile` `HE-AAC`.
The source is stereo because FFmpeg reports two channels for any HE-AAC stream, mono
included: parametric stereo may follow. Its declared duration runs about 90 ms past the
audio, more than its 43 ms packets, so the reader reports the measured span, `estimated`
(amendment A15b). The span counts the priming too: 2.133 s, so the tests hold it to 80
to 170 ms over the true length rather than the usual tolerance.

`heaac_mono.m4a` is the same with a mono source. ffprobe reports two channels for it as
well, so the reader must report the channel count unknown (amendment A15f-4). Its span
is also 2.133 s.

Only macOS builds of FFmpeg have `aac_at`; elsewhere the generator keeps the committed
files.

## Fragment cuts

`FRAGMENT_CUTS` are fragmented fixtures cut where their last `moof` box starts, so the
last `moof`+`mdat` pair and the trailing `mfra` are gone. The generator finds that
offset from ffprobe's packet offsets and sizes: it is where one fragment's packet data
ends and the next packet does not follow on.

| File                          | Cut from                  | Reported                                  |
| ----------------------------- | ------------------------- | ----------------------------------------- |
| `aac_fragments_cut.mp4`       | `aac_fragments.mp4`       | `estimated`, 1.536 s                      |
| `alac_fragments_cut.mp4`      | `alac_fragments.mp4`      | `estimated`, 1.536 s                      |
| `pcm_moov_fragments_cut.mp4`  | `pcm_moov_fragments.mp4`  | `estimated`, 0.768 s                      |
| `alac_moov_fragments_cut.mp4` | `alac_moov_fragments.mp4` | `estimated`, 0.768 s                      |
| `alac_indexed_cut.mp4`        | `alac_indexed.mp4`        | `truncated=true`, the `sidx` declares 2 s |

Without an index the declared duration is the sum of the fragments present, so it
shrinks with the packets; the reader must never report such a cut complete. Before A15a,
`alac_fragments_cut.mp4` was reported `truncated=false` with 1.536 s, and before A15f-1
`pcm_moov_fragments_cut.mp4` was reported `truncated=false` with 0.768 s.

## Unsupported and empty fixtures

| File             | ffprobe `format_name`     | Codec       | Rate | Ch  | Bits | Duration | Reported                              |
| ---------------- | ------------------------- | ----------- | ---- | --- | ---- | -------- | ------------------------------------- |
| `au.au`          | `au`                      | `pcm_s16be` | 8000 | 1   | 16   | 1.0      | every fact unknown, `unsupported`     |
| `core_audio.bin` | Core Audio Format         | `pcm_s16le` | 8000 | 1   | 16   | 1.0      | every fact unknown, `unsupported`     |
| `empty.flac`     | `flac`                    | `flac`      | 8000 | 1   | 16   | 0        | facts kept, `inconsistent`            |
| `empty.aiff`     | `aiff`                    | `pcm_s16be` | 8000 | 1   | 16   | 0        | facts kept, `inconsistent`            |
| `empty.m4a`      | `mov,mp4,m4a,3gp,3g2,mj2` | `aac`       | 8000 | 1   | -    | 0        | every fact unknown, `no_audio_stream` |
| `empty.mov`      | `mov,mp4,m4a,3gp,3g2,mj2` | `pcm_s16le` | 8000 | 1   | 16   | 0        | every fact unknown, `no_audio_stream` |

- `UNSUPPORTED_FIXTURES` are healthy files in formats outside the allow-list. The header
  call exits non-zero, which says winnow does not accept the format, not that the file
  is damaged (amendment A12b).
- `EMPTY_FIXTURES` are files ffmpeg writes with no audio packet (`-frames:a 0`). The
  FLAC declares no duration and the AIFF declares zero, so nothing positive is declared
  and an empty walk is not `truncated` (amendment A12d).
- `EMPTY_CONTAINER_FIXTURES` are an M4A and a MOV written the same way. ffprobe lists no
  stream at all for them, so every fact is unknown; the reader flags them
  `no_audio_stream` so the result says why (amendments A13c and A14b).

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
  `truncated=true` for two and three frames. One frame is missed because the encoder's
  end padding is longer than the frame removed (a known limit): the MP3 is
  `truncated=false` with 2.0 s, and the M4A, whose walk finds one packet fewer than
  `nb_frames` indexes, is compared in samples and is `estimated` at 2.005 s (amendment
  A15f-1). Before A15f-1 the M4A was `truncated=false` with 2.0 s.
- `mp3_frag_minus{1,3,10}.mp4` and `aac_frag_minus{1,3,10}.mp4` are `mp3_frag.mp4` and
  `aac_frag.mp4` cut the same way, at the packet boundary of the first of the last one,
  three, or ten packets. The stream duration still declares the full span, which is now
  more than the A5 tolerance above the shorter span, so the declared duration stands and
  every cut is `truncated=true` (amendment A14a). Cuts in the middle of a packet cannot
  show this: ffprobe reports them as warnings.

## Recorded ffprobe output

`recorded/*.json` holds what ffprobe 9.0.2 returned (exit status, stdout, stderr) for
the reader's two calls on selected fixtures, some cut to a fraction of their size, and
on 4 KB of zeros. Tests replay them without ffprobe, so the rules are exercised in CI,
which has no ffprobe. The header call asks only for the fields the rules read
(`-show_entries`, amendment A12a); rerun `record_ffprobe_outputs.py` whenever its
arguments change.
