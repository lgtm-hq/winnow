# ADR 0004: Capture-Date Resolver — Named Rules, Certainty, Undated by Default

## Status

Accepted — November 24, 2026 (number provisional; assigned at merge)

## Context

- Before this decision winnow had no date resolver. Discovery stored the modification
  time, shifted into the host timezone, as a file's creation date. The extractors kept
  the first value that parsed, so an editor's IFD0 `DateTime` or a `DateTimeOriginal` of
  2099 could become the capture time.
- The evidence model ([ADR 0003](0003-evidence-model.md)) now keeps every date a reader
  sees with its source, locator, raw text, precision and offset. Something has to decide
  from that evidence, and say why.
- Owner decisions of 2026-11-14 (#55, #396):
  - A date comes from the file's own evidence, never from the computer's timezone or
    clock.
  - A date found only in a camera file-name pattern may date a file, at WEAK certainty;
    `allow_filename_dates` is on by default.
  - Filesystem times never date a file; they are hints.
  - When the evidence does not support an answer, the result is undated, not a guess.

## Decision

`winnow/media/resolve_date.py` holds `resolve_capture_date(evidence, *, policy)` and
`resolve_digitized_date(evidence, *, policy)`. Both are pure functions over an
`EvidenceSet` and a `DatePolicy`. They read no file, no clock and no host timezone. Each
returns a `Decision` (ADR 0003) with the rule id, `rule_version` (`RULESET_VERSION`, now
1), and every date observation placed exactly once:

- in `chosen`;
- in `conflicts`;
- in `incomparable`;
- or in `rejected`, with a stable code.

Any change to the rule order or to screening bumps `RULESET_VERSION`.

### Policy

| Field                  | Default        | Meaning                                         |
| ---------------------- | -------------- | ----------------------------------------------- |
| `now`                  | none, required | aware scan start time; later values are future  |
| `allow_filename_dates` | `True`         | camera file-name patterns may date a file       |
| `gps_tolerance`        | `0`            | extra gap for a GPS fix to agree                |
| `agreement_tolerance`  | `0`            | extra gap for any other two values to agree     |
| `known_clock_defaults` | `()`           | documented make, model, locator and reset value |

Zero tolerance means two values agree only when their spans overlap at their stated
precision. #399 calibrates any positive tolerance with labelled data. Until then no
tolerance-based GPS confirmation is on by default.

### Step 1: screening

Every observation with a date claim is screened on its own, by these checks in order.
The first code that applies is recorded:

| Code                   | Applies when                                                        |
| ---------------------- | ------------------------------------------------------------------- |
| `epoch-container`      | a QuickTime container creation field read directly as the integer 0 |
| `not-capture`          | the claim is not `capture_time` (edit, digitizing, recording, ...)  |
| `placeholder`          | status `empty` or `placeholder`                                     |
| `unparsed`             | status `malformed` or `unsupported`, or no time value               |
| `foreign-origin`       | an embedded field whose origin is not the file                      |
| `unassociated-sidecar` | a sidecar not recorded in `dependencies` with an association        |
| `ambiguous-sidecar`    | more than one sidecar file of one kind offers values                |
| `no-rule`              | a capture field no rule reads (AAE, unknown EXIF field, ...)        |
| `offset-not-explicit`  | the value's offset origin is not one its rule accepts               |
| `placeholder`          | a `known_clock_defaults` entry matches make, model, locator and raw |
| `future`               | later than `now` (see below)                                        |
| `filename-disabled`    | a file-name date while `allow_filename_dates` is off                |

The `not-capture` claims are:

- edit: `modified_time`;
- release: `release_time`;
- the filesystem: `file_created_time`, `file_modified_time`, `file_changed_time`;
- arrival: `received_time`, `uploaded_time`;
- `digitized_time`, `recording_time`, `container_created_time`.

The digitized decision uses `not-digitized` instead.

`future`:

- An instant is future when the start of its span is after `now`.
- A wall clock has no known zone. It is future only when its first reading is after
  `now` in UTC plus 14 hours, the largest offset in use.
- Naive and aware datetimes are never ordered against each other, and a span that
  contains `now` is not future.

`epoch-container` needs a direct integer read. A demuxed `1970-01-01T00:00:00Z` stays
`not-capture`, because FFmpeg applies an epoch heuristic (#395).

### Step 2: the capture rules

The first rule with an eligible observation fires:

| Order | Rule id                   | Reads                                                      | Offsets accepted    | Certainty                    |
| ----- | ------------------------- | ---------------------------------------------------------- | ------------------- | ---------------------------- |
| 1     | `exif-original-offset`    | EXIF or XMP `DateTimeOriginal`                             | from the file       | PROBABLE; CONFIRMED with GPS |
| 2     | `quicktime-creationdate`  | `com.apple.quicktime.creationdate`                         | from the file       | PROBABLE; CONFIRMED with GPS |
| 4     | `exif-original-wallclock` | EXIF or XMP `DateTimeOriginal`                             | none (wall clock)   | PROBABLE                     |
| 5     | `iptc-xmp-date-created`   | IPTC 2:55/2:60, XMP `photoshop:DateCreated`                | none or from file   | PROBABLE; WEAK if month/year |
| 7     | `gps-only`                | EXIF GPS date and time, only when nothing else is eligible | UTC by spec or file | WEAK                         |
| 8     | `sidecar-takeout`         | Takeout `photoTakenTime`                                   | UTC by spec or file | PROBABLE                     |
| 9     | `sidecar-xmp`             | XMP sidecar `DateTimeOriginal` or `DateCreated`            | none or from file   | PROBABLE                     |
| 10    | `png-creation-time`       | PNG text `Creation Time`                                   | any but derived     | WEAK                         |
| 12    | `filename-pattern`        | a camera file-name pattern (policy permitting)             | none (wall clock)   | WEAK                         |
| 13    | `undated`                 | nothing above                                              | —                   | NONE                         |

Rules match an observation's claim, source and the field name in its locator (case
folded), so a reader's `#<n>` counter or namespace URI does not change the rule.

Every candidate of the firing rule must agree with every other at its stated precision.
Exactly one distinct finest value must remain. Otherwise the result is
`conflicting-capture` with `value=None`, `certainty=NONE` and every candidate in
`conflicts`. Neither input order nor the timestamps break a tie. A wall clock and an
instant of one rank cannot be compared, so they also abstain.

The digitized decision has two rules:

- `digitized-offset`: EXIF or XMP digitized time with an offset from the file.
- `digitized-wallclock`: the same with no offset.

Both are PROBABLE. Their conflict rule is `conflicting-digitized`. A digitized time
never fills the capture slot.

### Step 3: comparing the rest

Each value states a span: `1940` is the whole year and `10:10:10.12` a hundredth of a
second. A value with an offset is a span of UTC instants. One without is a span of wall
readings in an unknown zone. Every other eligible observation is compared with the
chosen value:

- **Agrees** (spans overlap, or are within the tolerance when it is positive): `chosen`.
  Copies and coarser values keep their provenance there. Only a GPS fix counts as an
  independent witness; family copies never raise certainty.
- **Incomparable** (a wall clock against an instant): `incomparable`. No timezone is
  ever derived, from GPS, from rounding, from siblings or from the host.
- **Differs**: a conflict. Certainty drops one level. A WEAK result that drops to NONE
  becomes `conflicting-capture`.
- **A GPS fix that differs** is rejected as `gps-disagrees`, not a conflict: a fix need
  not be contemporaneous with the shutter.

Container creation is compared as a hint only and never supplies or contradicts the
capture value: `container-compatible`, `container-differs` or `container-incomparable`.
With no chosen value it stays `not-capture`. Eligible observations of a decision without
a value are `lower-rank`.

### Step 4: certainty and warnings

| Warning                    | When                                                       | Effect          |
| -------------------------- | ---------------------------------------------------------- | --------------- |
| `zone-unknown`             | the value is a wall clock                                  | none            |
| `suspicious-clock-default` | 00:00:00 on 1 January 1904, 1970, 1980 or 2000, unprofiled | capped at WEAK  |
| `software-recorded`        | the file names software that wrote it                      | never CONFIRMED |
| `read-issues`              | the evidence has read issues                               | never CONFIRMED |

A lone timestamp is at most PROBABLE. CONFIRMED requires all of these:

- rule 1 or 2;
- a GPS fix that agrees within `gps_tolerance`;
- no conflict;
- nothing incomparable;
- none of the warnings above that block it.

A documented reset is rejected. A plausible but wrong camera clock cannot always be
detected; that uncertainty is what PROBABLE means.

## Consequences

### Positive

- Every date winnow reports can be explained from stored evidence and a named rule.
- Edit, copy, arrival, release, container and filesystem times can no longer become a
  capture date, and an unknown zone is never presented as an instant.
- Output is the same under any input order and any host timezone.

### Negative

- Many files that used to get a date (from the modification time) are now undated.
- CONFIRMED is rare by design: it needs an explicit offset and an agreeing GPS fix, and
  any `Software` tag blocks it, even a phone's firmware version.
- Rule matching depends on the locator spellings #395 and #402 write. Their tests must
  pin those locators against this resolver.

### Neutral

- Sidecar association is trusted from `dependencies` and `association_fingerprint` until
  #402 adds a per-observation association marker with an evidence schema bump.
- Nothing prints a decision yet; #397 shows them and #55 uses them.

## References

- Issue #396; parent epic #392; related #55, #394, #395, #397, #399, #401, #402.
- [ADR 0003](0003-evidence-model.md): the evidence model.
- EXIF 3.0 (CIPA DC-008-2023): `OffsetTimeOriginal` and GPS time.
- ExifTool QuickTime tags: UTC versus local time.
- IANA time zone database: offsets in use range from −12:00 to +14:00.
