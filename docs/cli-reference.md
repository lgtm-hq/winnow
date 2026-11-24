# CLI Reference

This document covers every command and option available on the current Winnow CLI
surface, plus the standard flag conventions that all future subcommands will follow.

> **Status:** Pre-alpha. Only the root command (`winnow`) ships today. Subcommands are
> tracked in [open issues](https://github.com/lgtm-hq/winnow/issues) (see the parent
> epic at [#2](https://github.com/lgtm-hq/winnow/issues/2)).

---

## Commands

### `winnow`

Root command. When invoked without a subcommand it prints the current version and exits
with code 0.

#### Usage

```text
winnow [OPTIONS] COMMAND [ARGS]...
```

#### Options

| Flag         | Description                 |
| ------------ | --------------------------- |
| `--version`  | Show the version and exit.  |
| `--no-color` | Disable colored output.     |
| `--help`     | Show this message and exit. |

#### Examples

```bash
winnow              # winnow X.Y.Z — use --help for commands.
winnow --version    # winnow, version X.Y.Z
winnow --help       # show available options
```

The version shown is illustrative; the CLI prints the installed `winnow-media` version.

### `winnow live-photos`

Reports Apple Live Photo pairs (a HEIC/JPEG still and a MOV clip sharing a content
identifier) under a directory. Read-only; always exits 0 on a successful scan.

```text
winnow live-photos [OPTIONS] DIRECTORY
```

| Flag                             | Default | Description                              |
| -------------------------------- | ------- | ---------------------------------------- |
| `--recursive` / `--no-recursive` | `true`  | Include files in subdirectories.         |
| `--unpaired`                     | `false` | List unpaired stills and videos instead. |
| `--format`, `-f`                 | `table` | `table` or `json`; other choices exit 2. |

The default table lists Still, Video, Verified, and Content Identifier per pair;
`--unpaired` lists Path and Kind (`still` or `video`) for orphans. `--format json` emits
the whole scan (`pairs`, `unpaired_stills`, `unpaired_videos`) with string paths
regardless of `--unpaired`. `csv` and `markdown` are accepted by the shared `--format`
option but raise a usage error (`format not supported by live-photos`).

```bash
winnow live-photos ~/Pictures                  # table of pairs
winnow live-photos --unpaired ~/Pictures       # orphans only
winnow live-photos -f json ~/Pictures | jq .   # machine-readable scan
```

### `winnow stats`

Summarizes the media under a directory. Read-only; exits 0 when the scan completes, even
if some entries, or DIRECTORY itself, could not be read (they are counted, see below).

```text
winnow stats [OPTIONS] DIRECTORY
```

| Flag                             | Default | Description                                                       |
| -------------------------------- | ------- | ----------------------------------------------------------------- |
| `--recursive` / `--no-recursive` | `true`  | Include files in subdirectories.                                  |
| `--fast`                         | `false` | Type by file name extension only; files are opened, but not read. |
| `--config FILE`                  |         | Path to configuration file.                                       |

`stats` walks the files the organize pipeline's discovery walks: entries whose name
starts with `.` (winnow's `.winnow-backups` and `.winnow-staging` included) are skipped,
`paths.exclude_patterns`, `symlink_policy` and `organize.max_depth` come from the loaded
configuration, and under the default `symlink_policy: skip` symlinks are not followed.
Directories below `organize.max_depth`, and directories already visited through another
path, are not entered and are not counted as skipped entries. A symlink loop (a link to
itself) is a skipped symlink, or an unreadable file under `symlink_policy: follow`.
Under `follow`, a file reached under several names is counted once, under the first name
that gives it a media type, as discovery does; the other names are skipped duplicates
and are not read again. A file is matched by the device and inode of the file actually
opened, so a link changed during the scan is counted as the file it pointed to when
read. Two hard links to one file therefore count once, where discovery counts both.

Each file is identified the way discovery identifies it: by its content, so a JPEG named
`IMG_0001.bin` is an image and a text file named `clip.mp4` is unknown. When no content
rule recognises a file, the detector falls back to its extension (an empty `zero.jpg` is
an image by name). Those files are counted in `Typed by extension only`, not in the
per-type rows. `--fast` types every file by its extension instead; the table then says
`extension only`. It still opens each file, without reading it, so a file that cannot be
opened is unreadable rather than counted by name.

Sizes and times come from the file that was opened, so a file replaced during the scan
is counted as the file that was read.

The table has one row per metric, in this order:

| Row                                | Meaning                                                                                                                         |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `Classified by`                    | `content`, or `extension only` with `--fast`.                                                                                   |
| `Total files`                      | Files read and classified.                                                                                                      |
| `Image` / `Video` / `Audio files`  | Files per media type, decided by content (by extension with `--fast`).                                                          |
| `Unknown files`                    | Files that get no media type.                                                                                                   |
| `Typed by extension only`          | Files no content rule recognised, typed by their extension, with a count per type. `n/a (extension only)` with `--fast`.        |
| `Extension does not match content` | Content and extension both give a type, and they differ (a JPEG named `.mp3`). `n/a (extension only)` with `--fast`.            |
| `Extension gives no type`          | Content gives a type, the extension none (`IMG_0001.bin`, no suffix). `n/a (extension only)` with `--fast`.                     |
| `Unreadable files`                 | Entries that could not be inspected, opened or read, including files deleted during the scan. Not in the totals.                |
| `Unreadable directories`           | Directories that could not be listed, DIRECTORY included. Their contents are not counted or estimated.                          |
| `Skipped entries`                  | Entries the walk policy skipped, with a count per reason: hidden or winnow folder, excluded by pattern, symlink, and others.    |
| `Total size`                       | Combined size of the files read.                                                                                                |
| `Earliest` / `Latest modified`     | File modification times (UTC) of the files read; `n/a` when none.                                                               |
| `Coverage`                         | `complete`, or `incomplete: ...` when any file or directory could not be read.                                                  |
| `Unreadable ... examples`          | After `Coverage`, only when there are read errors: up to 10 file paths and up to 10 directory paths, the first in sorted order. |

Content detection reads the start of each file and, for some formats, a short tail or a
few structures inside it, not the whole file. For Matroska and MPEG transport streams it
may hand the open file to ffprobe, which is limited in time, not in bytes read.

```bash
winnow stats ~/Pictures                   # by content, recursive
winnow stats --no-recursive ~/Pictures    # top level only
winnow stats --fast ~/Pictures            # by extension only, no content read
```

### `winnow info`

Shows metadata for one file. Read-only.

```text
winnow info [OPTIONS] FILE
```

| Flag     | Default | Description                                                        |
| -------- | ------- | ------------------------------------------------------------------ |
| `--fast` | `false` | Type by file name extension only; read no content and no metadata. |

`Media type` comes from the detector, as in `stats`: from the content, or from the
extension when no content rule recognises the file, which reads
`<type> (from the file name)`. With `--fast` it comes from the extension and reads
`<type> (extension only)`. `Extension check` compares the two: `matches content`,
`mismatch: content says image, extension says audio`, `extension gives no type`,
`type is from the file name, not the content`,
`content gives no type; extension says video` (or `... extension gives no type`), or
`not checked (extension only)` with `--fast`.

Images add `Image format`, `Color mode` and `Dimensions`. Video and audio add `Duration`
and `Codec` when ffprobe can read them (see [External tools](#external-tools)). Metadata
that cannot be read is left out; the type rows remain. `--fast` reads no metadata and
starts no ffprobe; it shows `Metadata: not read (extension only)`.

Size and times come from the file that was read. If FILE is replaced while it is read,
`info` reads it again once, then fails with `file changed while it was read`.

A file that cannot be read (deleted after the command started, permission denied, or not
a regular file such as a FIFO) is an error: `winnow info` prints the message on stderr
and exits 1. Only existence and kind are checked before the command runs, so a
permission problem is reported this way rather than as a usage error.

```bash
winnow info IMG_0001.bin          # image, extension gives no type
winnow info --fast IMG_0001.bin   # unknown (extension only)
```

### `winnow doctor`

Checks the local environment and prints one row per check. Exits 1 when a hard check
fails.

```text
winnow doctor
```

| Check           | Fails when                 | Warns when                                      |
| --------------- | -------------------------- | ----------------------------------------------- |
| Python          | older than 3.11            | -                                               |
| FFmpeg          | `ffmpeg` is not on `PATH`  | -                                               |
| ffprobe         | `ffprobe` is not on `PATH` | its version is unreadable or outside FFmpeg 9.x |
| Configuration   | the config file is invalid | -                                               |
| Cache directory | -                          | the directory or its parent is not writable     |
| Extras          | -                          | an optional extra is not installed              |

The ffprobe row shows the version banner and whether it is in the tested range. The
audio reader's rules were measured on FFmpeg 9.0.2; on another major version any ffprobe
warning makes the audio facts unknown (see [External tools](#external-tools)).

### `winnow init`

Creates a configuration file through guided prompts.

| Flag            | Description                                                     |
| --------------- | --------------------------------------------------------------- |
| `--yes`, `-y`   | Overwrite an existing configuration file without prompting.     |
| `--config FILE` | Path to configuration file (defaults to the working directory). |

`init` uses the standard `--yes` flag rather than a bespoke `--force`; without it the
command asks before overwriting an existing file.

### `winnow config set`

Sets one dotted key (`winnow config set cache.enabled false`) in the configuration file.
The value is read as JSON when it is valid JSON and kept as a string otherwise.

| Flag            | Description                 |
| --------------- | --------------------------- |
| `--config FILE` | Path to configuration file. |

The target is the `--config` path, else the discovered configuration file, else the
working-directory default. `set` reads the file as written, changes only that key where
the file sets it (in a nested mapping or a flat `a.b`/`a__b` key), and writes the file
back in its own format:

- Values from the `.local` companion file and `WINNOW_*` variables never reach the file,
  and other keys keep their cast markers (`workers: "@int 4"`).
- In a YAML file only the text of the key's value is replaced, and a new key is added as
  whole lines after the mapping it joins. The rest of the file is never rewritten: every
  other line, comment, blank line, anchor, quote style, null or boolean spelling,
  document marker and byte order mark stays exactly as it was. A scalar is written on
  the key's line; a non-empty list or mapping is written as a block below the key, in
  the file's own indents; an empty one as `[]` or `{}`; inside a flow collection, in
  flow style. A null or boolean that `set` writes is `null`, `true` or `false`. A
  comment after a scalar or flow collection on the key's own line keeps its place when
  the new value is also written on that line (a scalar, `[]` or `{}`, or any value
  inside a flow collection), as it still follows the same key.
- A JSON file is re-serialised with the indent it uses. Keys, their order and values are
  kept, while spacing, number forms and escapes can change: a compact one-line file
  `{"workers":2}` comes back as `{"workers": 3}`, and an inline array such as
  `"source_dirs": ["/a", "/b"]` in an indented file is spread over one line per item.
- CRLF line endings, a UTF-8 byte order mark and a missing final newline are kept.
- The file is replaced, not rewritten in place, so the user running `set` becomes its
  owner. It keeps its permission bits, but not setuid, setgid or sticky bits. A
  symlinked file is written through its link; a link to a missing file creates that file
  and keeps the link.
- Setting `follow_symlinks` or `symlink_policy` also sets the other only when the file
  would otherwise contradict itself.
- When the file already sets the key to the value as written, nothing is written.
  `"@int 4"` and `4` count as different values.

The result, read with the companion file, is validated before anything is written. These
fail with an error and leave the file untouched:

- a TOML (`.toml`, `.tml`), INI (`.ini`, `.conf`, `.properties`) or Python (`.py`) file;
- a file you cannot write;
- a symlink loop;
- a file that mixes CRLF and LF line endings or holds a lone CR;
- a YAML file that holds more than one document;
- an edit that would drop or move a comment: a comment inside the text being replaced,
  such as a block scalar's header comment, and a comment after it on its last line
  unless the old and the new value are each a scalar or flow collection on the key's own
  line. A comment after a block list or mapping, even of one entry, or after a block
  scalar's header is refused, and is never carried over to an item or key the new value
  happens to keep. A comment line between a key and the first entry of its list or
  mapping blocks replacing that list or mapping, as it describes the entries; a comment
  on the key's own line does not. A whole-document `null` with a comment after it
  (`null # note`) is refused too, as the comment would then follow the new key;
- a YAML file with a comment whose place cannot be established: a comment after a key's
  colon when the value starts on the next line and a blank line follows;
- a `NaN` or infinite number, or a value the file's format cannot hold;
- a file with a duplicate key, a JSON file with a byte order mark, or a YAML file that
  does not parse;
- a key whose value is an alias, or whose value holds an anchor (on a key or a value)
  that an alias or merge key outside it refers to, and a key inside a mapping reached
  through an alias or merged elsewhere with a `<<` merge key;
- a file that nests values more than 100 levels deep or holds a value that contains
  itself through a recursive anchor (`x: &r {self: *r}`), and a value that contains
  itself or would nest more than 100 levels deep with its key. The depth counts what the
  file gives once read: a `@json` cast string's value and a flat key's parts (`a.b.c: 1`
  is three levels) count too. Every command that reads the file refuses it the same way;
- a key in, or a value holding, a YAML mapping written with an explicit `?` key or with
  a key that has no `:`;
- a new key for a flow mapping (`{...}`) that holds a comment, which would then follow
  the new key;
- a YAML file that holds a U+0085, U+2028 or U+2029 line break;
- an edit whose new lines would not start at the file's indentation (such as a block
  list or mapping below an anchor or tag on a line of its own at another column), whose
  result does not parse, or that would change another setting; the message says the edit
  could not be made safely, not that the file is invalid;
- a key below a value written as a cast string (`routing: "@json {...}"`); set the whole
  value instead;
- a key a later entry or a flat key elsewhere in the file would override or merge into,
  or that would not read back as exactly the value given, both as written and with cast
  markers applied (an earlier `'@json ["*.jpg"]'` for the same key would join the list);
- an invalid result.

A file that does not exist yet is created: a `.json` path as JSON, any other path that
is not refused above as YAML.

### `winnow config reset`

Resets the configuration file to validated defaults.

| Flag            | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| `--dry-run`     | Print `Would reset configuration to defaults at <path>` only. |
| `--yes`, `-y`   | Skip the confirmation prompt.                                 |
| `--config FILE` | Path to configuration file.                                   |

Both `--dry-run` and the success message resolve the same target: the explicit
`--config` path, else the discovered configuration file, else the working-directory
default.

### `winnow cache`

Inspects and manages the perceptual-hash cache database (`cache.db` under
`cache.directory`). The database is never created by these commands: when the file does
not exist they print `no cache database at <path>` and exit 0.

#### `winnow cache show`

Prints a `Cache` table with one `hash` row (entry count) and a footer
`Database: <path> (<size>)`. Hit rate is not shown because the counters are per-process.
Without a database the table is still printed: the `hash` row reads `0` and the
`Database:` footer is replaced by `no cache database at <path>`.

| Flag            | Description                 |
| --------------- | --------------------------- |
| `--config FILE` | Path to configuration file. |

#### `winnow cache clear`

Removes every cached entry.

| Flag            | Description                                                |
| --------------- | ---------------------------------------------------------- |
| `--dry-run`     | Print `Would remove N entries.` without deleting anything. |
| `--yes`, `-y`   | Skip the `Remove N cached entries?` prompt.                |
| `--config FILE` | Path to configuration file.                                |

An empty cache prints `Nothing to remove.`; a declined prompt prints `Aborted.` and
exits 0; success prints `Removed N entries.`

#### `winnow cache prune`

Removes entries whose source files no longer exist.

| Flag            | Description                                             |
| --------------- | ------------------------------------------------------- |
| `--dry-run`     | List each stale path, then `N stale path(s) (dry run).` |
| `--yes`, `-y`   | Skip the `Prune N stale path(s)?` prompt.               |
| `--config FILE` | Path to configuration file.                             |

No stale entries prints `No stale entries.`; a declined prompt prints `Aborted.` and
exits 0; success prints `Pruned N entries.` (`N` counts rows, so a path cached under
several algorithms removes more rows than the prompt's path count).

---

## External tools

Audio and video facts come from `ffprobe` (part of FFmpeg); there is no Python fallback.
Without `ffprobe` every audio fact is unknown and flagged `backend_unavailable`, and
video metadata is empty.

The audio reader opens the file once and hands it to `ffprobe` as its standard input
(through the `fd` protocol, or through an extension-less link to `/dev/fd/0` in a
private temporary directory when `ffprobe` lacks that protocol), so the file name and
extension never influence a value. It reports these facts, each unknown when `ffprobe`
does not establish it: container, codec, sample rate, channels, bit depth (PCM and
lossless codecs only), bitrate (the stream's own), duration, and the number of audio
streams. Flags explain gaps:

- `corrupt`: the packet walk established damage: it failed after the header was
  accepted, or `ffprobe` flagged a packet corrupt without a warning. With FFmpeg 9.0.2 a
  packet flagged corrupt always comes with a `Packet corrupt` warning, so such a file is
  `unrecognized_output` instead. A cut file is `truncated`, `unrecognized_output`,
  `unsupported`, `estimated`, or `inconsistent` (some cuts of fragmented MP4), or
  `truncated=false` when the cut is no longer than the tolerance below, except in
  fragmented MP4, and in MP4 whose frame count counts packets when the cut removed one,
  where it is `estimated`.
- `unsupported`: no accepted format recognised the file (the header call failed), as for
  AU, Core Audio Format, garbage, or a playlist. It is not a claim of damage.
- `unrecognized_output`: `ffprobe` wrote a warning that is not known to be harmless for
  this file, or header output that is not valid UTF-8 or JSON or lacks the shape
  `ffprobe` writes. It is not proof of damage, but every value it could affect is
  unknown.
- `inconsistent`: packet times contradict each other or the declared duration, an MP4
  packet is longer than twice the median packet, an FLV packet has no time, the file
  changed (device, inode, size, or modification time) while it was read, or the audio
  stream has no codec, sample rate, or channel count.
- `no_audio_stream`: the file has no audio stream besides cover art, as for a video-only
  file or an empty M4A or MOV file. It is not a claim of damage.
- `estimated`: nothing declares the duration, so it was measured from the packets.
- `timeout`: an `ffprobe` call ran too long or wrote too much and was stopped.
- `backend_unavailable`: `ffprobe` is missing, could not start, was killed by a signal,
  exited 126 or 127, or its version could not be read.

`truncated` is `true`, `false`, or not checked. It is `false` only after a packet walk
found the audio present matches the declared duration; it says nothing more.

`truncated=true` and `corrupt` claim damage. `inconsistent`, `estimated`,
`unrecognized_output`, `unsupported`, and `no_audio_stream` do not: they say the
evidence does not establish a value, and a healthy file may carry them. A stream with no
packet at all is `truncated` only when a positive duration is declared; otherwise its
duration is unknown and `inconsistent`, as for an empty FLAC or AIFF file. A result with
every fact unknown always carries at least one flag.

Each `ffprobe` call runs in its own process group with a time limit and bounded output;
on a breach the whole group is stopped. On Linux its address space is capped at 1 GiB.

Known limits:

- Damage inside an intact packet is not detected; that needs a full decode.
- A damaged header byte can change a declared value (channels, sample rate, codec
  variant, duration) and leave no evidence.
- Audio that ends no more than max(5 ms, two time-base units of the stream) before the
  declared duration counts as complete. Encoder padding hides more. Measured with FFmpeg
  9.0.2 on 99 MP3 files (9 sample rates, 11 CBR and VBR settings): one removed frame is
  never detected; with two removed, 52 still report `truncated=false` with the full
  duration; with three, 5. In MP4 whose frame count counts packets, a cut that removes
  one leaves fewer packets than the index counts, so a loss the padding hides is
  `estimated`, never `truncated=false`: of 36 faststart AAC (M4A) files, one removed
  frame is `estimated` in all 36; two are `truncated` in 28 and `estimated` in 8; three
  are `truncated` in all. Wherever the duration is checked, a cut no longer than the
  tolerance reports `truncated=false`, except in fragmented MP4 and in such an MP4 cut
  by a whole packet, where it is `estimated`.
- An MP3 is `estimated` only when `ffprobe` says it estimated the duration (no Xing/Info
  or VBRI header). With such a header the duration is declared and checked.
- A cut exactly on a page or frame boundary of Ogg, MPEG-TS, ADTS, or AC-3 leaves no
  evidence. Those formats are always `estimated`, never `truncated=false`.
- A Matroska `DURATION` tag is never used. Matroska audio without its own stream
  duration, or the format-level duration of any file, is checked only when the audio is
  the file's only non-picture stream and the file starts at zero; otherwise the duration
  is `estimated`.
- Audio whose first packet comes more than two time-base units after zero is `estimated`
  in every container: its duration field can be the end of the file's timeline. Cuts in
  such files are not detected.
- AVI audio is always `estimated`: its duration field includes a start delay while the
  packets start at zero. ASF (WMA, WMV) audio is `estimated` when the file has any other
  non-picture stream, such as video: there the field is the whole file's play length.
- MP4, M4A, and MOV audio with a packet longer than twice the median packet reports no
  duration and `inconsistent`; fragmented MP4 stretches its first packet across a start
  delay. On 256 healthy generated MP4, M4A, and MOV files the largest packet was 1.0
  times the median. Other containers are not checked this way.
- AAC 5.1 in FLV reports every audio fact unknown and `inconsistent`: `ffprobe` gives a
  configuration packet no time and can report a wrong sample rate.
- Stereo PCM in Matroska can report no duration and `inconsistent`, because `ffprobe`
  gives its last packet no duration.
- ADTS at 8 kHz is `estimated` up to about 170 ms long.
- AAC and MP3 in Matroska are `estimated`: their duration counts the encoder priming as
  audio, one priming block over the real length at every sample rate. In MP4 without an
  edit list (fragmented MP4, or `-use_editlist 0`) the packets hold the priming, so
  every codec except PCM and lossless ones is `estimated` unless the declared duration
  is more than the tolerance above the packet span; a cut shortens the span, so it is
  still reported `truncated`, unless it is on a fragment boundary of a fragmented MP4
  without a fragment index: that shortens the declared duration too, and the result is
  `estimated`. Measured with FFmpeg 9.0.2: AAC 1024 samples (128 ms at 8 kHz, 93 ms at
  11.025 kHz, 21 ms at 48 kHz), MP3 1105 samples (138 ms at 8 kHz, 100 ms at 11.025 kHz,
  23 ms at 48 kHz; 32 to 104 ms in MP4, 160 ms at 8 kHz), Vorbis in MP4 17 to 134 ms,
  Opus in MP4 6.5 ms, MP2 in MP4 8 to 70 ms, AC-3 and E-AC-3 with `-use_editlist 0` 6 to
  44 ms, AAC from `aac_at` 47 to 324 ms, and DTS with no excess (`estimated` at its
  exact length). The measured length still includes it.
- MP3 with `-use_editlist 0` at 24 to 48 kHz runs about one packet past its declared
  duration, so a cut of exactly one packet looks healthy and is `estimated` (8 of 2,257
  packet-boundary cuts).
- Other audio in Matroska or MP4 whose declared duration is the packet span of a stream
  starting before zero is `estimated`: the duration counts the priming. Measured with
  FFmpeg 9.0.2: fragmented AAC with `delay_moov` (as AAC above), Vorbis (21 to 134 ms),
  WMA (6 to 60 ms), MP2 (8 to 30 ms), AC-3 and E-AC-3 below 48 kHz (6 to 8 ms), and Opus
  (8 ms), all in Matroska. A cut never looks like priming: it shortens the span but not
  the declared duration. The exception is a cut on a fragment boundary of a fragmented
  MP4 without a fragment index, which shortens both and is `estimated`.
- A checked duration is exact to one codec frame: the packets can run past the true
  length. On 3,220 healthy generated files every checked duration was within one median
  packet of the true length, plus 1 ms in Matroska, whose timestamps are whole
  milliseconds. Above the 5 ms tolerance were WMA in ASF (up to 60 ms), AAC LC from
  `aac_at` in faststart MP4 or MOV (6 to 108 ms, under one frame), AC-3 at 44.1 kHz
  stream-copied into faststart MP4 (27.8 ms, under one frame), FLAC and WavPack in
  Matroska at 44.1 kHz (23 ms), and DTS in Matroska at 44.1 kHz (12 ms against an 11 ms
  packet).
- A fragmented MP4 is never `truncated=false`. Without a fragment index (`sidx` or
  `mfra`) its declared duration is the sum of the fragments present, so a cut on a
  fragment boundary shortens it along with the packets. `ffprobe` shows nothing that
  tells an indexed file apart, so every fragmented MP4 is treated as unindexed: it is
  `truncated` when the declared duration is longer than the packets, and otherwise
  `estimated`, healthy files included. A file counts as fragmented when its packets
  outnumber the samples its `moov` box indexes. For PCM, whose MP4 samples are audio
  samples, the index counts more samples than there are packets; such a file counts as
  fragmented when its packets span more samples than the index counts, by more than the
  tolerance. A file that keeps its first fragment in `moov` and is cut at its first
  `moof` has lost every fragment, looks unfragmented, and reports `truncated=false`. So
  does a cut at a later `moof` when no fragment before it held audio, as with ALAC at 8
  kHz beside video (512 ms packets, 200 ms fragments), and a cut that leaves one
  fragment of a PCM file written with `+delay_moov`, or that leaves no more than the
  tolerance of PCM audio past the indexed samples.
- ADPCM in MP4 and MOV has no established duration: ffprobe's packet durations do not
  match the decoded audio, so the duration is unknown with `inconsistent`, never
  `truncated`. ADPCM in WAV and Matroska keeps its checks; in AVI it is `estimated`.
- HE-AAC in MP4 is `estimated`: its declared duration is 89 to 376 ms past the true
  length (2 to 3 packets), more than a codec frame. The measured length reported instead
  is 133 to 508 ms past the true length: always over, and by 3 to 4 packets (43 ms
  packets at 48 kHz, 128 ms at 16 kHz).
- HE-AAC's channel count is unknown: ffprobe reports 2 channels for mono HE-AAC as well
  as stereo.
- In MP4, a first packet longer than the median packet by more than the tolerance makes
  the duration `estimated`. Without an edit list, B-frame video stretches the first
  audio packet by its delay (40 ms at 25 fps with one B-frame). A two-packet file whose
  first packet is the longer one is `estimated` too.
- Chained Ogg reports no duration and `inconsistent`.
- A WAV or AIFF written to a pipe makes no damage claim. The AIFF is `unsupported`. A
  short piped WAV is all unknown and a long one keeps its stream facts, both
  `unrecognized_output`, when the data does not end on a 4096-byte packet; otherwise it
  is `estimated` with the right duration. An MS ADPCM WAV whose `fact` chunk ffprobe
  ignores is `estimated`.
- An AIFF with an `APPL` chunk signed `stoc` is reported `truncated=true` although it
  plays: FFmpeg 9.0.2 reads no packet from it.
- A file changed and then restored with the same size and modification time while it is
  read is not noticed.

Audio tags and dates are not read yet; they will come from ExifTool.

---

## Environment

Winnow resolves its per-user directories from the environment at call time, in the order
listed. Relative overrides are taken as given; `~` is expanded.

| Variable            | Purpose                                           | Fallback                                              |
| ------------------- | ------------------------------------------------- | ----------------------------------------------------- |
| `WINNOW_CONFIG_DIR` | Per-user config directory (`.winnow-config.yaml`) | `$XDG_CONFIG_HOME/winnow`, then `~/.config/winnow`    |
| `WINNOW_DATA_DIR`   | Per-user data directory (`sessions.db` saga log)  | `$XDG_DATA_HOME/winnow`, then `~/.local/share/winnow` |

`WINNOW_*` settings overrides (see `winnow config`) use the same `WINNOW` prefix but map
onto configuration fields, not directories. `__` separates nested keys
(`WINNOW_CACHE__ENABLED=false`). A value is read as a TOML 1.0 value when it is one
(`true`, `True`, `42`, `0.5`, `["*.jpg", "*.png"]`, `"quoted"`), and as a plain string
otherwise, the same on every Python version.

A leading cast marker casts the rest of a value, in the environment and in config file
values alike (`WINNOW_WORKERS="@int 4"`, `workers: "@int 4"`): `@int`, `@float`,
`@bool`, `@str`, `@json`, `@none`, `@null`, `@note`, `@comment`, `@upper`, `@lower`,
`@title`, `@capitalize`, `@strip`, `@lstrip`, `@rstrip`, `@split`, `@casefold` and
`@swapcase`. Other markers (`@format`, `@jinja`, `@get`, `@read_file`, `@merge`, `@del`,
...) are an error.

### Config file layering

A `<name>.local.<ext>` file next to the config file (`.winnow-config.local.yaml` for
`.winnow-config.yaml`) is read after it and overrides it: mappings merge key by key,
lists are joined, and other values replace. A top-level key containing `.` or `__` sets
a nested setting (`routing.review: Review`, `routing__review: Review`).

The config file's format follows its suffix, in any letter case. `.toml` and `.tml`
(`c.TOML` too) are read as TOML 1.0, and `.json` as JSON. `.ini`, `.conf`, `.properties`
and `.py` files are rejected with a `ConfigError` that names the format. Any other file,
with or without a suffix (`.yaml`, `.yml`, `.YAML`, `.txt`, `cfg`), is read as YAML.

---

## Standard Flag Conventions

`winnow/cli/standards.py` defines reusable Click option factories that every upcoming
subcommand will use. The sections below document each flag and the composite decorators
that group them, so contributors pick the right factory and users know what to expect
across the CLI.

### Individual options

| Flag         | Short | Type    | Default | Description                              |
| ------------ | ----- | ------- | ------- | ---------------------------------------- |
| `--dry-run`  | —     | flag    | `false` | Preview changes without modifying files. |
| `--yes`      | `-y`  | flag    | `false` | Skip confirmation prompts.               |
| `--no-color` | —     | flag    | `false` | Disable colored output (root only).      |
| `--format`   | `-f`  | choice  | `table` | Output format (see values below).        |
| `--output`   | `-o`  | path    | none    | Output file path.                        |
| `--workers`  | `-w`  | int ≥ 1 | `4`     | Number of worker threads.                |
| `--config`   | —     | path    | none    | Path to configuration file.              |

`--format` accepts: `json`, `csv`, `table`, `markdown`.

#### Cache options

The cache group is applied as a unit via `cache_options()`:

| Flag                            | Type | Default | Description                |
| ------------------------------- | ---- | ------- | -------------------------- |
| `--enable-cache` / `--no-cache` | flag | `true`  | Enable or disable caching. |
| `--cache-path`                  | path | none    | Cache directory path.      |

### Cross-command conventions

| Convention        | Rule                                                                              |
| ----------------- | --------------------------------------------------------------------------------- |
| Confirmation skip | `--yes`/`-y` everywhere; never `--force`                                          |
| Worker count      | `--workers`/`-w`; purpose-specific variants only when genuinely distinct          |
| Cache toggle      | `--enable-cache/--no-cache`                                                       |
| Output format     | `--format`/`-f` with `OutputFormat` choices; document dumps are listed exceptions |
| Color             | `--no-color` on the root command only; subcommands read `ctx.obj["no_color"]`     |
| Destructive ops   | `--dry-run` + confirmation unless `--yes`                                         |

`tests/cli/test_flag_conventions.py` sweeps every registered command against these
rules. New destructive commands or format exceptions register themselves in that
module's tables (`_DESTRUCTIVE`, `_YES_ONLY`, `_FORMAT_EXCEPTIONS`).

### Composite option groups

Subcommands attach options through one of three compositor decorators defined in
`winnow.cli.standards`:

| Compositor                   | Included options                      |
| ---------------------------- | ------------------------------------- |
| `standard_command_options`   | `--config`, `--dry-run`, `--yes`      |
| `reporting_command_options`  | standard + `--format`, `--output`     |
| `processing_command_options` | standard + `--workers`, cache options |

Note the usage asymmetry: the compositors above are applied directly
(`@standard_command_options`) because they accept the callback, while the individual
option factories are functions that must be **called** first to return a decorator
(`@dry_run_option()`, `@yes_option()`).

---

## Exit codes

Every `winnow` invocation ends with one of four exit codes. Commands raise `WinnowError`
subclasses and let them propagate; the root `WinnowGroup` (`winnow/cli/errors.py`)
renders them once, as an error panel on stderr, and picks the code. Scripts can
therefore tell "you called it wrong" (2), "it failed" (1), and "you stopped it" (130)
apart.

| Code | Meaning                                                                                                                             | Source                                                              |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 0    | Success, including "nothing to do" and a decline the command handles itself                                                         | command returns                                                     |
| 1    | Failure: any `WinnowError` subclass, a command-reported failure, or a declined `click.confirm(..., abort=True)` prompt (`Aborted!`) | `WinnowGroup.invoke` / `ctx.exit(ExitCode.FAILURE)` / Click `Abort` |
| 2    | Usage error: bad flag, missing argument, unknown command                                                                            | Click                                                               |
| 130  | Interrupted by Ctrl-C                                                                                                               | `WinnowGroup.invoke`                                                |

Declined prompts split by how the command asks: `clean`, `cache clear`, `cache prune`,
and the REPL print `Aborted.` and return (0); `config reset` and `init` use
`click.confirm(..., abort=True)`, so a decline is Click's `Abort` and exits 1.

Use the `ExitCode` enum from `winnow.cli.errors` rather than bare integers when a
command needs to report a failure itself (for example `doctor` exiting with
`ExitCode.FAILURE` when a hard check fails).

---

## Adding a subcommand

1. Choose the appropriate compositor from `winnow.cli.standards` and decorate the
   callback (e.g. `@standard_command_options`).
2. Register the command on `main` in `winnow/cli/__init__.py` via
   `main.add_command(your_command)`.
3. Use `--dry-run` for any write operation and `--yes` for destructive prompts.
4. Read `ctx.obj["no_color"]` from the root context to honour the user's colour output
   preference.
5. Raise `WinnowError` subclasses for domain failures instead of wrapping them in
   `click.ClickException`; the root handler maps them to exit code 1.
