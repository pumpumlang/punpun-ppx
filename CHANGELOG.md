# Changelog

## 1.5.0 — 2026-09-11

- Coordinated PPX with the PunPun 1.5.0 halfway release so platform packaging
  can embed an exact matching package-manager client.
- Kept the stable format-1 lockfile, deterministic package archive, dependency
  path bridge and `outdated` behavior from 1.4.5 unchanged.
- Reserved the larger PPX discovery/ecosystem improvements for the second half
  of the PunPun 1.5 series rather than changing package semantics mid-release.

## 1.4.5 — 2026-09-11

- Added a deterministic `ppx paths` bridge so the `pp` launcher can pass every
  materialized dependency to the compiler automatically.
- Replaced the placeholder `ppx outdated` response with current/latest package
  reporting and optional JSON output.
- Fixed unconstrained resolution so stable releases win over newer prerelease
  builds unless the requirement explicitly opts into a prerelease.
- Coordinated PPX versioning with the PunPun 1.4.5 SDK. Release packaging now
  rejects a mismatched PPX checkout instead of shipping a stale client.
- Added direct regression coverage for dependency paths, cached package
  identity, semantic-version selection, and package archive reproducibility.

## 1.3.0 — 2026-09-10

- Split PPX into its own repository with the client, reference registry, and
  static package catalog under one owner.
- Added direct dependency graph editing, deterministic format-1 lockfiles,
  package integrity verification, signing, audit, and registry publishing.
