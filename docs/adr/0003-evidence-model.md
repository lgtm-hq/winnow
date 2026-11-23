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
the rule that made them. The models live in `winnow/models/evidence.py`; the collector
lives in `winnow/media/evidence.py`.

- `Observation`: `id`, `claim`, `source`, `locator`, `origin`, `raw`, `value`
  (`TimeValue`, a string, or `None`), `status`, `family`, `reader`, `reader_version`.
- `TimeValue`: a naive `wall` clock reading, an `offset_minutes` with its
  `offset_origin` (`source`, `utc-by-spec` or `derived`), and a `precision` from year to
  subsecond. `instant()` is an aware UTC moment only when the offset is known.
- `EvidenceSet`: the schema version, a `FileSnapshot` taken before reading, every
  observation in collection order, every `ReadIssue`, the sidecar snapshots and the
  association fingerprint. `EvidenceSet.model_dump_json()` is the stored and `--json`
  form.
- `Decision`: the claim, the value, a `Certainty`, `rule_id` and `rule_version`, the
  chosen, rejected (with a reason code), conflicting and incomparable observations, and
  warnings. Its indices are local to one `EvidenceSet`.

### Rules for everyone writing observations

- Never drop a value because another one already answered. Duplicates and empty,
  placeholder or malformed values are kept with their `ParseStatus`.
- Never shift a value with the host timezone. `wall` is what the file says;
  `offset_minutes` comes only from the file or from a specification that states UTC. A
  `utc-by-spec` offset is 0. Offsets lie within -12:00..+14:00.
- Partial dates keep their precision: `1985` is `YEAR`, never 1 January 1985. Components
  below the precision hold documented anchors (month 1, day 1, `00:00:00.000000`) and
  are never rendered, compared or offset. A date-only value has no offset and no
  instant.
- A raw fraction longer than six decimal places stays in `raw`; a reader reports the
  truncation instead of claiming full precision.
- A locator names an XML field by namespace URI and local name, never by prefix.
  Repeated occurrences of one field get distinct locators. `family` is a correlation
  hint, not proof of independent provenance: copied EXIF and XMP fields cannot raise
  certainty. Unmapped fields use `Claim.UNKNOWN`.
- A `SCREEN_CAPTURE` observation names the filename rule or embedded marker it came
  from; a file name is not visual proof.

### Observation identity

`observation_id` is a SHA-256 over a canonical JSON object (sorted keys, compact, UTF-8)
of the evidence schema version, the source snapshot's fingerprint, the origin, the exact
locator, the raw value, the parsed value, the status, the reader and the reader version.
The observation's position and its neighbours are not part of it, so reordering a
collection cannot change an ID. The collector assigns every ID. Durable references
between files use observation IDs with the stored evidence, never a bare index.

### Collection

`collect_evidence` stats the file, runs detection once (or reuses a given
`DetectionResult`), records each `TypeSignal` as a `MEDIA_TYPE` observation and the
container, major brand and compatible brands as `CONTAINER` observations, runs each
reader in order and stats the file again.

- A file that cannot be stat'ed, is not a regular file or cannot be opened is
  `unreadable` and has no observations, not even ones read from its name. Its `snapshot`
  is `None` when it could not be stat'ed.
- A reader that raises gives a `failed` issue for that reader only.
- A different size, `mtime_ns`, `ctime_ns`, device or inode after reading gives a
  `file-changed` issue. Equal stats are a freshness hint, never proof of unchanged
  content; a consumer that needs exact identity reads the file again.

## Consequences

### Positive

- Every later decision can be explained from stored evidence: the values found, the rule
  that chose and how certain the result is.
- Unknown is expressible everywhere: no offset, no date, no snapshot.
- Detection is public (`winnow.media.detect`, `DetectionResult`, `TypeSignal`) and its
  result is reused rather than read twice.

### Negative

- Every stored observation carries its locator, raw value and reader version, so stored
  evidence is larger than a single parsed value.
- An observation ID changes whenever the file's stat or the reader version changes, so a
  reader upgrade invalidates stored IDs.

### Neutral

- `MediaMetadata.captured_at` stays until #55 removes it with a schema bump.
- Any change to the stored shape or to the identity rule bumps
  `EVIDENCE_SCHEMA_VERSION`.

## References

- Issue #394; parent epic #392; related #55, #42, #61, #395, #396, #397, #402.
- #393: structured content detection (PRs #449 and #454).
- EXIF 3.0 (CIPA DC-008-2023): date, offset and subsecond tags.
- XMP specification part 1: date type.
