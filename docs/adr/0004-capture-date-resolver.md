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
| `agreement_tolerance`  | `0`            | extra gap for two lower-rank values to agree    |
| `known_clock_defaults` | `()`           | documented make, model, locator and reset value |

Issue #396 types `now` as aware UTC. The policy accepts any timezone-aware datetime and
converts it to UTC; a naive one is refused.

Zero tolerance means two values agree only when their spans overlap at their stated
precision. #399 calibrates any positive tolerance with labelled data. Until then no
tolerance-based GPS confirmation is on by default. Tolerance never joins two candidates
of the firing rule (step 2).

### Stale evidence

When the evidence carries the collector's file-level `file-changed` issue, its
observations may describe a previous file state. The decision is `undated`, every date
observation is rejected as `stale-evidence`, and the warning `stale-evidence` is set.
Retrying the collection belongs to the caller (#55, #397). A failed or unavailable
optional reader is different: the remaining evidence is valid, so the file can still be
dated, with the `read-issues` warning.

### Step 1: screening

Every observation with a date claim is screened on its own, by these checks in order.
The first code that applies is recorded:

| Code                  | Applies when                                                        |
| --------------------- | ------------------------------------------------------------------- |
| `zero-container-time` | a QuickTime container creation field whose raw value is exactly `0` |
| `not-capture`         | the claim is not `capture_time` (edit, digitizing, recording, ...)  |
| `placeholder`         | status `empty` or `placeholder`                                     |
| `unparsed`            | status `malformed` or `unsupported`, or no time value               |
| `sidecar-unverified`  | any sidecar value (see "Sidecars" below)                            |
| `foreign-origin`      | an embedded field whose origin is not the file                      |
| `no-rule`             | a capture field no rule reads (an unknown EXIF field, ...)          |
| `offset-not-explicit` | the value's offset origin is not one its rule accepts               |
| `placeholder`         | a `known_clock_defaults` entry matches make, model, locator and raw |
| `future`              | later than `now` (see below)                                        |
| `filename-disabled`   | a file-name date while `allow_filename_dates` is off                |

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

`zero-container-time` names what was read: a direct integer read of zero. It does not
claim that a writer left the clock unset. #396's `epoch-container` code, which needs a
documented writer profile, is reserved until #395 PR B or #398 documents one. A demuxed
`1970-01-01T00:00:00Z` is not a zero read: FFmpeg applies an epoch heuristic (#395). It
is a container hint like any other container creation time (step 3).

A reset profile matches the make against `device` observations whose locator names the
make field, and the model against those naming the model field.

#### Sidecars

A sidecar date may be rated only when the evidence establishes a positive, verified,
unique association between that sidecar and this file. Evidence schema version 1 has no
such signal:

- `dependencies` lists every file read, ambiguous candidates included;
- `association_fingerprint` describes the matching rules, not a result.

So every sidecar date is a hint (`sidecar-unverified`) and is never chosen. Rules 8 and
9 below cannot fire until #402 supplies the signal. A sidecar observation never
suppresses any other observation.

What #402 has to supply, with an `EVIDENCE_SCHEMA_VERSION` bump as ADR 0003 describes:

- for each sidecar observation, a recorded association result for this file;
- that result is `verified-unique` only when the sidecar matched this file and no other
  candidate under the documented matching rules;
- ambiguous candidates (one stem XMP shared by a RAW and a JPEG, two Takeout JSON files
  for one name) are kept with a non-unique result, so the resolver can show them as
  hints.

The resolver will then admit rules 8 and 9 for `verified-unique` observations only, and
`RULESET_VERSION` will be bumped.

### Step 2: the capture rules

The first rule with an eligible observation fires:

| Order | Rule id                   | Reads                                                      | Offsets accepted    | Certainty                    |
| ----- | ------------------------- | ---------------------------------------------------------- | ------------------- | ---------------------------- |
| 1     | `exif-original-offset`    | EXIF or XMP `DateTimeOriginal`                             | from the file       | PROBABLE; CONFIRMED with GPS |
| 2     | `quicktime-creationdate`  | `com.apple.quicktime.creationdate`                         | from the file       | PROBABLE; CONFIRMED with GPS |
| 4     | `exif-original-wallclock` | EXIF or XMP `DateTimeOriginal`                             | none (wall clock)   | PROBABLE                     |
| 5     | `iptc-xmp-date-created`   | IPTC 2:55/2:60, XMP `photoshop:DateCreated`                | none or from file   | PROBABLE; WEAK if month/year |
| 7     | `gps-only`                | EXIF GPS date and time, only when nothing else is eligible | UTC by spec or file | WEAK                         |
| 8     | `sidecar-takeout`         | Takeout `photoTakenTime` (awaits #402)                     | UTC by spec or file | PROBABLE                     |
| 9     | `sidecar-xmp`             | XMP sidecar original or `DateCreated` (awaits #402)        | none or from file   | PROBABLE                     |
| 10    | `png-creation-time`       | PNG text `Creation Time`                                   | any but derived     | WEAK                         |
| 12    | `filename-pattern`        | a camera file-name pattern (policy permitting)             | none (wall clock)   | WEAK                         |
| 13    | `undated`                 | nothing above                                              | —                   | NONE                         |

Rules match an observation's claim, source and the field name in its locator (case
folded), so a reader's `#<n>` counter or namespace URI does not change the rule. No rule
assumes a file-name pattern is UTC.

#### The native EXIF original is the authority

**Difference from #396.** The issue's table puts an embedded EXIF or XMP
`DateTimeOriginal` at the same rank: rank 1 with an offset, rank 4 without. Under the
orchestrator's ruling on PR #486, the camera's native EXIF original is the authority for
the written local date and time. So when a file has an eligible EXIF original, an
embedded XMP `exif:DateTimeOriginal` is a copy. It never competes for the value,
whatever its rank, and it is placed relative to the EXIF value instead (`_date_copies`):

| EXIF original | XMP copy                                                 | Outcome                                                            |
| ------------- | -------------------------------------------------------- | ------------------------------------------------------------------ |
| zoned         | zoned, spans overlap (equal, coarser or finer)           | compatible: `chosen`, the EXIF value and offset are kept           |
| zoned         | zoned, spans disjoint                                    | contradiction: `conflicting-capture`                               |
| naive         | zoned                                                    | `incomparable`: never chosen and never lowers the EXIF's certainty |
| zoned         | naive, same written digits                               | compatible: `chosen`                                               |
| zoned         | naive, other digits                                      | `incomparable`: other digits give no zone mapping                  |
| naive         | naive, same written digits                               | compatible: `chosen`                                               |
| naive         | naive, other digits, both full readings at one precision | contradiction: `conflicting-capture`                               |
| naive         | naive, other digits otherwise                            | hint: `copy-differs`                                               |

Examples on a month boundary:

- EXIF `2019-08-01T00:10:10.555+09:00` with XMP `2019-07-31T15:10:10Z` gives August 1 at
  `+09:00`.
- EXIF `2019-08-01T00:10:10+09:00` with XMP `2019-07-31T15:10:10.555Z` gives the same
  date: the finer XMP digits are supporting evidence only.
- Naive EXIF `2019-08-01T00:10:10` with XMP `2019-07-31T15:10:10Z`, with or without a
  UTC GPS stamp, gives August 1 under `exif-original-wallclock`, never July.

Without an EXIF original (a PNG, or an export that carries only XMP), the XMP original
dates the file under rules 1 and 4 as the table says. Only `DateTimeOriginal` is treated
this way. An XMP `photoshop:DateCreated` is its own field and is compared as in step 3.

The firing rule's candidates must give one value:

1. Every pair must overlap exactly at its stated precision. Tolerance never joins two
   candidates of one rule.
2. **Same property, one zoned.** IPTC and XMP `DateCreated` are one property. When only
   one of the two carries an offset, they are compared by the clock readings they write.
   An IPTC `10:10:10+09:00` and an XMP date `2019-07-04` agree, and the fuller timestamp
   is kept. This never gives the wall clock a zone. Any other wall clock and instant
   cannot be compared, so such a pair abstains.
3. **Preferred representation.** The kept value is the one with an offset, then the
   finest, then the source in this order: EXIF, QuickTime, IPTC, PNG text, XMP, file
   name. An EXIF original never meets its XMP copy here (see above). In practice the
   ties are between IPTC and XMP `DateCreated`, or between candidates of one source.
4. **Calendar guard.** Every other candidate must either state exactly the same instant
   at the same precision as the kept value, or write the same calendar date at the
   precision both state. Otherwise the result abstains. An IPTC
   `2019-08-01T00:10:10+09:00` beside a finer XMP `DateCreated`
   `2019-07-31T15:10:10.000Z` therefore abstains rather than giving July.

When these fail, the result is `conflicting-capture` with `value=None`, `certainty=NONE`
and every candidate in `conflicts`. Neither input order nor the timestamps break a tie.

The digitized decision has two rules:

- `digitized-offset`: EXIF or XMP digitized time with an offset from the file.
- `digitized-wallclock`: the same with no offset.

Both are PROBABLE. Their conflict rule is `conflicting-digitized`. A digitized time
never fills the capture slot.

### Step 3: comparing the rest

Each value states a span: `1940` is the whole year and `10:10:10.12` a hundredth of a
second. A value with an offset is a span of UTC instants. One without is a span of wall
readings in an unknown zone. Two representations of one property where only one is zoned
compare their written readings, as in step 2. Every other eligible observation is
compared with the chosen value:

- **Agrees** (spans overlap, or are within the tolerance when it is positive): `chosen`.
  Copies and coarser values keep their provenance there. Only a GPS fix from another
  provenance family counts as an independent witness; copies never raise certainty.
- **Incomparable** (a wall clock against an instant of another property):
  `incomparable`. No timezone is ever derived, from GPS, from rounding, from siblings or
  from the host.
- **Differs**: a conflict. Certainty drops one level. A WEAK result that drops to NONE
  becomes `conflicting-capture`.
- **A GPS fix that differs** is rejected as `gps-disagrees`, not a conflict: a fix need
  not be contemporaneous with the shutter.
- **A file-name date that differs** is rejected as `filename-differs`, not a conflict: a
  name proves no capture clock, so it never vetoes or downgrades an embedded date.

Container creation is compared as a hint only and never supplies or contradicts the
capture value: `container-compatible`, `container-differs` or `container-incomparable`.
With no chosen value it stays `not-capture`. Eligible observations of a decision without
a value are `lower-rank`.

### Step 4: certainty and warnings

| Warning                    | When                                                       | Effect                         |
| -------------------------- | ---------------------------------------------------------- | ------------------------------ |
| `zone-unknown`             | the value is a wall clock                                  | none                           |
| `suspicious-clock-default` | 00:00:00 on 1 January 1904, 1970, 1980 or 2000, unprofiled | WEAK; undated if contradicted  |
| `editing-software`         | a `software` value's leading token(s) name a known editor  | never CONFIRMED                |
| `read-issues`              | a reader failed or was unavailable                         | never CONFIRMED                |
| `stale-evidence`           | the file changed during collection                         | undated (see "Stale evidence") |

The known editors are a short list in `winnow/media/_date_policy.py` (`KNOWN_EDITORS`).
Each entry cites a public source for the string its program writes: Adobe Photoshop (and
Lightroom, whose string starts the same), GIMP, darktable and RawTherapee. Any other
`Software` text does not block CONFIRMED, including camera firmware such as `10.0.2`,
`HDR+ 1.0.570503588zd` or `00WW_3_380_SP02`. Names match on a token boundary: `GIMP`,
`GIMP 2.10` and `Adobe Photoshop Lightroom 6.0` match; `Adobe PhotoshopCompatible ...`
and a name inside another word do not. An entry is added only with a source.

A reset-like value that stands alone is kept, with the warning and capped at WEAK. When
a different capture field contradicts it, the result is `conflicting-capture`: the
reset-like value is not asserted over the contradiction. The undated decision keeps the
`suspicious-clock-default` warning, so it says both things.

A lone timestamp is at most PROBABLE. CONFIRMED requires all of these:

- rule 1 or 2;
- a GPS fix from another provenance family that agrees within `gps_tolerance`;
- no conflict;
- no incomparable capture field, except a file name or a date-only `DateCreated` that
  writes the chosen value's calendar day;
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
- Sidecar dates date nothing until #402 records verified unique associations.
- CONFIRMED needs an explicit offset and an independent agreeing GPS fix, so it stays
  uncommon. #399 measures how often.
- Rule matching depends on the locator spellings #395 and #402 write. Their tests must
  pin those locators against this resolver.

### Neutral

- Nothing prints a decision yet; #397 shows them and #55 uses them.

## References

- Issue #396; parent epic #392; related #55, #57, #394, #395, #397, #399, #401, #402.
- [ADR 0003](0003-evidence-model.md): the evidence model.
- EXIF 3.0 (CIPA DC-008-2023): `OffsetTimeOriginal` and GPS time.
- IPTC Photo Metadata standard: Date Created and its XMP mapping.
- ExifTool QuickTime tags: UTC versus local time.
- IANA time zone database: offsets in use range from −12:00 to +14:00.
