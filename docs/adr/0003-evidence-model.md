# ADR 0003: Evidence Model — Observations Kept Apart from Decisions

## Status

Accepted — November 23, 2026 (number provisional; assigned at merge)

## Context

- winnow must say what each file is and when it was made, and explain why. Before this
  decision each extractor kept the first value that parsed and dropped the rest:
  `MediaMetadata.captured_at` held one naive `datetime` with no source, so an IFD0
  `DateTime` written by an editor could not be told apart from a camera capture time,
  and an EXIF offset or subsecond value was lost.
- Content detection (#393) already returns structured evidence, a `DetectionResult` with
  every `TypeSignal`, but nothing stored it next to dates or identifiers.
- Later work (date readers #395 and #402, the resolver #396, `explain_file` #397, the
  catalogue #401, relationship evidence #404, scan integration #45) needs one shape to
  store observations and decisions in.

## Decision

Every observation a reader makes about a file is kept with its source, its exact
locator, its raw value and its parsed value. Decisions are stored separately and name
the rule that made them. The models live in `winnow/models/evidence.py`. The collector
lives in `winnow/media/evidence.py`.

- `Observation` has these fields: `id`, `claim`, `source`, `locator`, `origin`, `raw`,
  `value` (a `TimeValue`, a string, or `None`), `status`, `family`, `reader` and
  `reader_version`. Every text field must be valid Unicode. Bytes are written as `hex:`
  followed by their hexadecimal digits.
- `TimeValue` holds a naive `wall` clock reading, an `offset_minutes` with its
  `offset_origin`, a `precision` from year to subsecond, and, for subseconds, the
  `fraction_digits` the source gave. The `offset_origin` is `source` or `utc-by-spec`.
  The third origin, `derived`, is reserved for a rule in #396 that derives an offset
  from other evidence in the file. It is never the host's zone, and never a rounded
  difference of two naive times. `instant()` returns an aware UTC moment only when an
  offset is known.
- `EvidenceSet` holds:
  - the schema version;
  - a `FileSnapshot` taken before reading;
  - every observation, in collection order;
  - every `ReadIssue`;
  - the sidecar snapshots and the association fingerprint.

  `EvidenceSet.model_dump_json()` is the stored and `--json` form. A set with another
  `schema_version` is rejected, not read as this one.

- `Decision` holds the claim, the value, a `Certainty`, `rule_id` and `rule_version`,
  the chosen, rejected (each with a reason code), conflicting and incomparable
  observations, and warnings. Its indices are local to one `EvidenceSet`.

### Rules for everyone writing observations

- Never drop a value because another one already answered. Duplicates and empty,
  placeholder or malformed values are kept with their `ParseStatus`.
- Never shift a value with the host timezone.
  - `wall` is what the file says. Text input must be the full
    `YYYY-MM-DDTHH:MM:SS[.ffffff]` form. A date alone is rejected rather than read as
    midnight.
  - `offset_minutes` is an integer. It comes only from the file or from a specification
    that states UTC. A `utc-by-spec` offset is 0. Offsets lie within -12:00..+14:00.
  - A wall time and offset whose UTC instant falls outside `datetime`'s range are
    rejected. This lets `instant()` keep a single meaning for `None` ("no offset")
    instead of also meaning "out of range". It also means `instant()` never raises.
- Partial dates keep their precision. `1985` is `YEAR`, never 1 January 1985.
  - Components below the precision hold documented anchors: month 1, day 1, `00:00:00`,
    and zero for fraction digits past `fraction_digits`. They are never rendered,
    compared or offset.
  - A date-only value has no offset and no instant.
  - For a minute or a fraction of a second, `instant()` is the start of that interval.
  - `render()` writes a four-digit year on every platform and exactly the fraction
    digits the source gave.
- `wall` keeps at most six fraction digits. A source with more has `fraction_digits`
  above 6: the value is truncated, and `raw` keeps every digit. A reader reports the
  truncation and does not claim the extra digits.
- Locators:
  - A locator names an XML field by namespace URI and local name, never by prefix.
  - Repeated occurrences of one field get a `#<n>` counter scoped to that field, in
    emitted order. An unrelated field appearing or disappearing therefore does not move
    a locator.
  - `family` is a correlation hint, not proof of independent provenance: copied EXIF and
    XMP fields cannot raise certainty.
  - Unmapped fields use `Claim.UNKNOWN`.
- A `SCREEN_CAPTURE` observation names the filename rule or embedded marker it came
  from. A file name is not visual proof.

### Paths

Paths are stored as given. They are not resolved, case-folded or Unicode-normalised, so
an NFC and an NFD spelling of one file are two paths, as the walk saw them.

In JSON and in hashes, a path whose bytes are valid UTF-8 is written as that text.
Otherwise it is written as `hex:` and its bytes in lowercase hexadecimal. This happens
when the bytes are not valid UTF-8, which Linux allows and APFS refuses, or when the
text itself starts with `hex:`. The encoding is reversible, so every file a filesystem
allows can be stored and hashed.

### Observation identity

`observation_id` is a SHA-256 over a canonical JSON object (sorted keys, compact, UTF-8)
of:

- the evidence schema version;
- the snapshot fingerprint;
- the origin;
- the claim, source and family;
- the exact locator;
- the raw and parsed values;
- the status;
- the reader and reader version.

The snapshot fingerprint is a SHA-256 over the snapshot's path, size, `mtime_ns`,
device, inode, `ctime_ns` and `birthtime_ns`.

Claim, source and family are included even though the issue's list omits them. With
them, two observations share an ID only when they say the same thing about the same
field occurrence of the same file state.

The observation's position and its neighbours are not part of the hash. The collector
assigns every ID.

IDs are local to one file state. A copy, a move or a rename changes the path, the inode
or `ctime`, and so changes every ID. Identity across paths is established by content
verification (#6, #404), never by matching IDs. Durable references between files use
observation IDs with the stored evidence, never a bare index.

### Collection

`collect_evidence` does the following, in order:

1. Stats the file.
2. Obtains a `DetectionResult`.
3. Records each `TypeSignal` as a `MEDIA_TYPE` observation, at locator
   `detect:<source>:<field>#<n>`.
4. Records the container, the major brand and each compatible brand as `CONTAINER`
   observations.
5. Runs each reader in order.
6. Stats the file again.

The following rules apply:

- **Unreadable.** These cases are `unreadable` and have no observations:
  - a file that cannot be stat'ed, is not a regular file, or cannot be opened;
  - a file whose content detection could not read.

  No observation is read from such a file's name either. `snapshot` is `None` only when
  the stat failed.

- **Reused detection.** `DetectionResult.snapshot` is the `fstat` of the file detection
  read, and is `None` when the content was not read. A given result is used only when
  its snapshot matches the file's on path, size, `mtime_ns`, `ctime_ns`, device and
  inode. Otherwise the file is detected afresh. A matching result is never detected
  again.
- **Changed.** The file gets a `file-changed` issue in each of these cases:
  - detection read a different state than the snapshot;
  - the stat differs after reading;
  - the file vanished.

  Equal stats are a freshness hint, never proof of unchanged content. A consumer that
  needs exact identity reads the file again.

- **Failed readers.** A reader that raises gets a `failed` issue for that reader only.
- **Foreign origins.** An observation whose `origin` is not the collected path is not
  stored, and its reader gets a `failed` issue.

### Sidecar evidence (#402)

Sidecar evidence (#402) will follow these rules:

- It assigns each sidecar observation's ID from the sidecar's own snapshot, taken before
  it is read.
- It records that snapshot in `dependencies`.
- Any per-observation association warning it needs, for example for retained ambiguous
  candidates, is added with an `EVIDENCE_SCHEMA_VERSION` bump.

`ReadIssue.message`, `Decision.warnings` and `family` are not that channel.

### Stored version 1 example

Abridged from `tests/models/data/evidence_v1.json`, which a test pins byte for byte:

```json
{
  "schema_version": 1,
  "snapshot": {
    "path": "/photos/2019/IMG_0001.HEIC",
    "size": 2048576,
    "mtime_ns": 1562231410123456789,
    "device": 16777229,
    "inode": 670575813,
    "ctime_ns": 1562231411000000001,
    "birthtime_ns": 1562231409000000000
  },
  "observations": [
    {
      "id": "sha256:a2a2ae76d4d78e409008b3045edc5e6200f2470862db14c38cb9ee7f0a0a2f28",
      "claim": "capture_time",
      "source": "exif",
      "locator": "ExifIFD:DateTimeOriginal#0",
      "origin": "/photos/2019/IMG_0001.HEIC",
      "raw": "2019:07:04 10:10:10 | 123 | +09:00",
      "value": {
        "wall": "2019-07-04T10:10:10.123000",
        "offset_minutes": 540,
        "offset_origin": "source",
        "precision": "subsecond",
        "fraction_digits": 3
      },
      "status": "ok",
      "family": "exif",
      "reader": "pillow",
      "reader_version": "12.0.0"
    },
    {
      "claim": "modified_time",
      "value": {
        "wall": "2021-01-01T09:00:00",
        "offset_minutes": null,
        "offset_origin": null,
        "precision": "second",
        "fraction_digits": null
      }
    }
  ],
  "dependencies": [{ "path": "hex:2f70686f746f732f636166e92e6a7067" }]
}
```

## Consequences

### Positive

- Every later decision can be explained from stored evidence: the values found, the rule
  that chose and how certain the result is.
- Unknown is expressible everywhere: no offset, no date, no snapshot, content not read.
- Detection is public (`winnow.media.detect`, `DetectionResult`, `TypeSignal`). Its
  result carries the snapshot it was read under, so it is reused safely rather than read
  twice.

### Negative

- Every stored observation carries its locator, raw value and reader version, so stored
  evidence is larger than a single parsed value.
- An observation ID changes with any snapshot field (including the path), the reader
  version or the schema version. A reader upgrade therefore invalidates stored IDs.

### Neutral

- `MediaMetadata.captured_at` stays until #55 removes it with a schema bump.
- Any change to the stored shape or to the identity rule bumps
  `EVIDENCE_SCHEMA_VERSION`. Reading older stored versions is the job of whoever stores
  them (#401).

## References

- Issue #394; parent epic #392; related #55, #42, #61, #395, #396, #397, #401, #402,
  #404.
- #393: structured content detection (PRs #449 and #454).
- EXIF 3.0 (CIPA DC-008-2023): date, offset and subsecond tags.
- XMP specification part 1: date type.
