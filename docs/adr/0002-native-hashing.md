# ADR 0002: Native Hashing — Rust Batch Hasher vs Pure Python imagehash

## Status

Accepted — July 18, 2026

## Context

- Winnow must compute perceptual hashes (pHash, dHash, aHash) over potentially
  large media libraries to detect near-duplicate images and videos.
- Two implementation strategies are under evaluation:
  - **Pure Python + numpy (`imagehash`):** delegates numerical operations to
    numpy (compiled C), installs on any platform without a native build
    toolchain, and carries no manylinux wheel complexity.
  - **Rust native extension:** a bespoke batch hasher written in Rust, exposed
    via PyO3, compiled to platform wheels, with potential for SIMD acceleration
    and sub-GIL parallelism.
- At evaluation time winnow is pre-alpha with no hashing subcommands shipped.

## Methodology

Lightweight microbenchmarks were run on the development host (Python 3.12,
Ubuntu 24.04, 8-core VM) using `imagehash 4.3.2` with Pillow-generated
synthetic RGB images (no JPEG decode overhead). Three algorithms (pHash, dHash,
aHash) were measured across three representative resolutions, and pHash
threading scaling was measured at 1 024 × 768.

**Results are provisional:** synthetic images skip the JPEG/HEIF decode step
that dominates real-world scan time for large-resolution inputs.

### Single-threaded throughput — imagehash 4.3.2 (synthetic images)

| Resolution    | Algorithm | ms / image | img / s |
|:--------------|:----------|:----------:|:-------:|
| 256 × 256     | pHash     | 2.19       | 458     |
| 256 × 256     | dHash     | 0.29       | 3 487   |
| 256 × 256     | aHash     | 0.26       | 3 921   |
| 1 024 × 768   | pHash     | 3.12       | 321     |
| 1 024 × 768   | dHash     | 2.75       | 364     |
| 1 024 × 768   | aHash     | 2.64       | 379     |
| 3 840 × 2 160 | pHash     | 30.54      | 33      |
| 3 840 × 2 160 | dHash     | 27.04      | 37      |
| 3 840 × 2 160 | aHash     | 26.48      | 38      |

Peak memory per pHash call (1 024 × 768): **64.5 KB**.

### Threading scaling — pHash, 1 024 × 768, 100 images

| Workers | Total (s) | img / s |
|:-------:|:---------:|:-------:|
| 1       | 0.44      | 226     |
| 2       | 0.16      | 632     |
| 4       | 0.08      | 1 194   |
| 8       | 0.09      | 1 159   |

numpy releases the GIL during array operations; scaling is near-linear to
4 workers before OS-scheduling overhead causes diminishing returns.

### Conservative Rust-extension estimates (provisional)

Published benchmarks of Rust perceptual-hash crates (e.g. `img-hash`) suggest
3–8× improvement in raw resize + hash computation over imagehash. However:

- At small resolutions (≤ 256 × 256) the dominant cost is Python/C boundary
  overhead and Pillow construction, not the hash algorithm itself.
- At large resolutions (≥ 1 K) the dominant cost is JPEG/HEIF decode, which a
  Rust hasher alone does not accelerate unless it also owns the decode pipeline
  — a significantly larger scope than a drop-in hash extension.
- The threading experiment shows imagehash already scales to ~1 200 img/s on
  4 cores, well beyond typical interactive batch sizes.

## Decision

**Adopt `imagehash` (pure Python + numpy) as the perceptual-hash engine.
Defer the Rust native extension.**

### Rationale

1. **Sufficient throughput.** A personal library of 50 000 images at the
   measured pHash throughput (1 024-pixel input) completes in approximately
   42 s on 4 cores — well within acceptable interactive batch time.
2. **numpy releases the GIL; threading is free.** Equivalent parallelism to a
   Rust extension is already available via Python's `ThreadPoolExecutor` at
   zero build cost.
3. **I/O and decode dominate at scale.** For real-world JPEG/HEIF libraries,
   image decode — not hash computation — will be the binding constraint. A Rust
   extension that only replaces the hash step does not close that gap.
4. **YAGNI at pre-alpha.** Winnow has no hashing subcommands yet. Optimising
   an unimplemented hotspot violates YAGNI.
5. **Wheel distribution complexity.** A PyO3 extension requires manylinux
   wheels built per Python version (3.11, 3.12, 3.13) per platform (Linux,
   macOS, Windows), adding CI matrix breadth and release friction before the
   project has a stable release pipeline.

### Deferred work — re-evaluate when any of the following hold

- A production profile shows hash computation (not I/O or decode) is ≥ 30 %
  of total scan time on a real media library.
- Batch sizes routinely exceed 100 000 images per session.
- A manylinux CI pipeline already exists for another native dependency,
  reducing the incremental wheel-build cost to near zero.

## Consequences

### Positive

- `pip install winnow-media` works on all supported platforms with no native
  build toolchain required.
- `imagehash` (MIT-licensed) is actively maintained and widely deployed.
- Threading parallelism via `concurrent.futures` is available immediately with
  zero additional dependencies.
- No CI matrix expansion for platform-specific wheel builds.

### Negative

- Raw single-core hash throughput is conservatively 3–8× slower than an
  optimised Rust implementation for large images.
- Adopting a Rust extension later will require reworking the hash interface
  and adding wheel-build CI infrastructure at that time.

### Neutral

- The hashing interface should be defined behind a `Protocol` or abstract base
  class from day one so a future Rust backend is a drop-in replacement without
  caller changes.
- Benchmarks in this ADR use synthetic images (no decode cost). A follow-up
  ADR must incorporate real JPEG/HEIF timing before any native-extension
  decision is revisited.

## References

- Parent issue: [#44](https://github.com/lgtm-hq/winnow/issues/44)
- imagehash library: <https://github.com/JohannesBuchner/imagehash>
- img-hash Rust crate: <https://github.com/abonander/img_hash>
- PyO3 packaging guide: <https://pyo3.rs/latest/building-and-distribution>
- Prior ADR: [0001-api-first-platform.md](0001-api-first-platform.md)
