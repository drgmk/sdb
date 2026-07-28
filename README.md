# SDB

SDB is a Python 3.12 application for building an inspectable SQLite database of
stellar targets, catalog provenance, normalized photometry, multiple-system
structure, and operator decisions for SED work.

The current implementation provides:

- deterministic SIMBAD/Gaia-backed identity with coordinate-only fallback;
- versioned catalog, metadata, hierarchy, and curated-dataset ingestion;
- system-aware photometry ownership and fit-eligibility review;
- durable samples, update/readiness workflows, and incremental exports;
- SDF-compatible IPAC output plus versioned joint-fit sidecars; and
- a localhost interactive review workspace.

Use `sdb --help` and `sdb COMMAND --help` as the command reference. The normal
starting sequence is:

```sh
conda activate sdf
sdb --database databases/sdb.sqlite init
export SDB_ACTOR="$USER"
sdb --database databases/sdb.sqlite --offline add --ra 10 --dec -20
sdb --database databases/sdb.sqlite status sdbid-v3-004000.00-200000.0
```

Copy `sdb.example.toml` to `sdb.toml` or
`~/.config/sdb/config.toml` for reference-provider, freshness, operator, and
mirror defaults. `sdb reference ensure` then checks every configured reference
provider and fetches only missing or stale snapshots.

Run the deterministic release check with:

```sh
conda run -n sdf scripts/release-check.sh
```

`plan.md` is the chronological implementation record. The operator guide and
completed refactor review live under `docs/` in the working repository.
