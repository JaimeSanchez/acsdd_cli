# DB-004: Create Doctrine Migration

**Manifest:** `_manifests/DB-004.yaml`
**Adapter:** `DB-004-PHP-SYM44-MYSQL8` (Symfony 4.4, Doctrine ORM, MySQL 8.0)
**Depends on:** DB-001 (entity inventory)

## Scope

Generates a migration file. Never executes it.

## Procedure

0. Before diffing, confirm `config/packages/doctrine.yaml` explicitly sets
   `dbal.default_table_options.charset: utf8mb4` and
   `collation: utf8mb4_0900_ai_ci`. Doctrine's own default collation
   (`utf8mb4_unicode_ci`) doesn't match what MySQL 8.0+ actually creates
   tables with by default — without an explicit pin, every diff run
   generates spurious `ALTER TABLE ... COLLATE` noise that has nothing to
   do with real entity changes. If the config is missing, flag it in
   `diff-report` rather than silently including the collation churn in the
   migration. This is the `schema:collation-charset-explicit` gate.
1. Run `bin/console doctrine:migrations:diff` (doctrine/doctrine-migrations-bundle
   ^2.0 syntax for Symfony 4.4) against the current entity mapping (DB-001's
   inventory), optionally scoped to `target-entity`.
2. Inspect the generated SQL **before** writing the migration file. If the
   diff contains `DROP TABLE`, `DROP COLUMN`, or a destructive `CHANGE`
   (narrowing/removing), stop and report it — do not silently include it.
   This is the `schema:no-destructive-diff` quality gate.
3. Check any new identifiers in the diff against MySQL 8's reserved-word list
   (`schema:no-reserved-word-identifiers` — see DB-002 for the list).
4. Write the migration to `migrations/Version{timestamp}.php` with both
   `up()` and `down()` implemented — `down()` must be a genuine inverse, not
   a stub, per the `migration:rollback-safe` quality gate.
5. Embed `migration-description` as a PHP comment at the top of the class.
6. **Never execute the migration** (`doctrine:migrations:migrate`) as part of
   this capability. Per `security_constraints:
   require-human-approval-before-migration-execution`, execution against any
   database — including local/dev — is a separate, explicitly human-approved
   step outside this capability's scope.

## Adapter notes (Symfony 4.4 / MySQL 8.0)

- `doctrine/doctrine-migrations-bundle ^2.0` command surface
  (`doctrine:migrations:diff`, `doctrine:migrations:migrate`) — the `^3.x`
  command syntax used in later Symfony versions differs and is out of scope
  for this adapter.
- **MySQL 8 default collation vs. Doctrine's default.** MySQL 8.0.1+
  defaults new tables to `utf8mb4_0900_ai_ci`; Doctrine DBAL (pre-4.0)
  defaults to `utf8mb4_unicode_ci` unless told otherwise. Left unpinned,
  this is a well-documented source of noisy, meaningless migration diffs on
  every run — see step 0 above.
- **Auth plugin.** MySQL 8 defaults to `caching_sha2_password` instead of
  5.7's `mysql_native_password`. Older PHP 7.1.x builds with an outdated
  `mysqlnd`/`pdo_mysql` can fail to connect against this default. Worth
  confirming the actual PHP patch version and MySQL driver support before
  relying on a default (non-`mysql_native_password`) MySQL 8 user account.
