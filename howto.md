# SDB quick start

The old MySQL, shell-script, and Apache-directory workflow has been retired.
The authoritative implementation is the Python `sdb` CLI backed by SQLite.

## Environment

```sh
conda activate sdf
sdb --help
```

Set the operator identity once per review session. Audited commands generate a
contextual reason when `--reason` is omitted:

```sh
export SDB_ACTOR="$USER"
```

For persistent defaults, copy `sdb.example.toml` to `sdb.toml` or
`~/.config/sdb/config.toml`. Project settings override user settings.

## Create or upgrade a database

```sh
sdb --database databases/sdb.sqlite init
```

Running `init` on an existing database applies pending Alembic migrations.
Back up authoritative SQLite databases before upgrading.

## Add and update targets

```sh
sdb --database databases/sdb.sqlite add 'HD 12345'
sdb --database databases/sdb.sqlite --offline add --ra 10 --dec -20
sdb --database databases/sdb.sqlite update 'HD 12345'
sdb --database databases/sdb.sqlite status 'HD 12345'
```

Use `sdb import` for durable CSV/TSV ingestion and `sdb sample` commands for
named target collections.

Ensure the configured whole-catalog snapshots are available before an update:

```sh
sdb reference ensure
sdb reference ensure --check
```

## Review

```sh
sdb --database databases/sdb.sqlite review serve --sample SAMPLE
sdb --database databases/sdb.sqlite history TARGET
```

The localhost review workspace exposes current hierarchy, catalog,
photometry-assignment, fit-eligibility, lifecycle, and immediate-relative
context. Every mutation uses preview/apply with append-only audit history.

## Readiness and export

```sh
sdb --database databases/sdb.sqlite sample readiness SAMPLE
sdb --database databases/sdb.sqlite photometry fitting-groups --sample SAMPLE
sdb --database databases/sdb.sqlite export-sample SAMPLE \
  --output-dir exports/SAMPLE
```

Legacy-compatible IPAC files remain the SDF input. Exports also write a
versioned joint-fit JSON sidecar; SDF does not consume that sidecar yet.

## Verification

```sh
conda run -n sdf scripts/release-check.sh
```

Use `sdb COMMAND --help` for the current command contract. The fuller operator
guide is `docs/operations.md` in the working repository.
