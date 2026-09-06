<!-- markdownlint-disable MD024 -- duplicate headings are standard in changelogs -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security

## [0.20.0] - 2026-09-06

### Added

- **report**: add a stepwise SQLite schema migration runner shared by all stores (#203)
  (94971aa)

## [0.19.1] - 2026-09-06

### Changed

- **ci**: state that integration tests are opt-in locally and always run in CI (#198)
  (7fc2ef5)
- **hash**: one hamming_distance and one perceptual-digest parser across hash and dedup
  (#204) (c6cafd5)
- **typing**: remove the four type: ignore comments and guard against new ones (#199)
  (3f4bfa2)
- **core**: keep one SymlinkPolicy enum shared by config and PathValidator (#201)
  (2f20760)
- **ci**: adopt latest lgtm-ci (v0.69.x) — core workflows pinned to v0.52.3 (#202)
  (994eb9b)

### Fixed

- **cli**: stop --help from printing docstring Args/Raises sections (#200) (fcabdfe)

## [0.19.0] - 2026-08-26

### Added

- add org AI review via lgtm-ci reusable (#176) (127bf78)

### Changed

- **lint**: enforce module and function complexity budgets (#175) (5181b22)
- **fs**: split transaction into per-operation modules (#174) (7da57a9)

## [0.18.0] - 2026-07-18

### Added

- **media**: detect media types via content sniffing with MIME and override fallbacks
  (#171) (f47c78e)

### Changed

- **fs**: deduplicate shared cleanup and tombstone helpers (#172) (91bac95)
- **config**: split loader into path, reader, env, and writer modules (#170) (fda75d7)

## [0.17.0] - 2026-07-18

### Added

- **dedup**: duplicate finder and quality comparator (#154) (749e241)

## [0.16.0] - 2026-07-18

### Added

- **classify**: screenshot and photo/graphic classifiers (#151) (10e1f49)

## [0.15.0] - 2026-07-18

### Added

- **cli**: clean, info, and stats commands (#150) (8c330f0)

## [0.14.0] - 2026-07-18

### Added

- **media**: image, video, and audio processors (#144) (00c60de)

## [0.13.0] - 2026-07-18

### Added

- **report**: SQLite v2 report schema with FTS5 (#149) (9fae6ce)

## [0.12.0] - 2026-07-18

### Added

- **hash**: SQLite content-addressable perceptual-hash cache (#142) (86cff41)

## [0.11.0] - 2026-07-18

### Added

- **cli**: config command, init, and interactive REPL (#152) (eeaa434)

## [0.10.0] - 2026-07-18

### Added

- **pipeline**: reversible commands and PipelineContext DI (#146) (c2d46f4)

## [0.9.0] - 2026-07-18

### Added

- **hash**: image perceptual hasher (pHash/dHash/aHash/wHash) (#147) (63243e3)

### Changed

- **adr**: add ADR 0002 native hashing decision (#153) (5408274)
- **cli**: add CLI command reference (#148) (94916c8)
- **quality**: property-based tests for security-sensitive utilities (#145) (deb6fe9)

## [0.8.0] - 2026-07-18

### Added

- **cli**: Rich console, help command, and doctor diagnostics (#143) (68f7bb2)

### Changed

- **release**: Docker build, publish, and sign to GHCR (#141) (0ecdad9)

## [0.7.0] - 2026-07-17

### Added

- **fs**: add atomic filesystem operations and backups (#133) (5aed22e)

## [0.6.0] - 2026-07-17

### Added

- **config**: add Dynaconf YAML config loader (#131) (cd8b957)

## [0.5.0] - 2026-07-17

### Added

- **cli**: add shared Click option decorators (#127) (e3f3335)

## [0.4.0] - 2026-07-17

### Added

- **media**: add extensible format registry (#132) (cc17f70)

## [0.3.0] - 2026-07-17

### Added

- **security**: add path validation and symlink protection (#129) (c10f198)

### Changed

- **arch**: add ARCHITECTURE.md module boundaries (#130) (260a728)
- **repo**: rewrite README with install, usage, and badges (#128) (b8c58d7)
- **maintenance**: add weekly GHCR image cleanup (#126) (ca3e4e9)

## [0.2.1] - 2026-07-17

### Changed

- add Cursor Cloud dev environment instructions (AGENTS.md) (#122) (fcf8e0d)
- **ci**: adopt canonical emoji check names (#121) (66be32e)
- **ci**: adopt lgtm-ci v0.52.3 and fix path-filtered required checks (#119) (25614d1)
- **renovate**: drop linting-tools group superseded by org preset (#118) (966636e)
- **renovate**: drop package rules redundant with org preset (#115) (ae71217)

### Fixed

- **release**: allow release-version-pr PyPI egress (#134) (137a730)
- **deps**: bump setuptools to 83.0.0 to clear OSV vulnerability (#124) (c428879)

## [0.2.0] - 2026-07-01

### Features

- **core**: add exception hierarchy with structured context (#110) (fbc8838)

## [0.1.0] - 2026-07-01

### Features

- **models**: add Pydantic domain models and StrEnum types (#108) (6a70e8c)

### Documentation

- **adr**: add ADR 0001 API-first platform CLI-first phasing (#107) (a9b7de1)

### Other Changes

- **repo**: remove org ruleset payloads from winnow (4f9a4a0)

## [0.0.3] - 2026-06-13

### Bug Fixes

- **ci**: install release smoke test wheel in isolated venv (#105) (2590135)

## [0.0.2] - 2026-06-13

### Fixed

- **ci**: wire Homebrew tap dispatch and package smoke test (#103) (9b1ae51)

## [0.0.1] - 2026-06-09

### Fixed

- **ci**: repair release and PR title workflow startup failures (#101) (b34f766)

### Other Changes

- **repo**: bootstrap winnow foundation and org compliance (#100) (c634b89)
- Initial commit (c64fed7)

### Previously Unreleased

- Initial project skeleton with CLI entry point, dev tooling, and CI workflows.
