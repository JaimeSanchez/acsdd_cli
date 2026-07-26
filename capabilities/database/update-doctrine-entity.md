# DB-003: Update Doctrine Entity

**Manifest:** `capabilities/_manifests/DB-003.yaml`
**Adapter:** `DB-003-PHP-SYM44-MYSQL8` (Symfony 4.4, Doctrine ORM, MySQL 8.0)
**Depends on:** DB-001 (entity inventory)

## Scope

Additive/modifying changes to an existing entity only. Field narrowing,
removal, or relation removal is rejected — that's a destructive change and
out of scope for this capability. Never generates a migration (that's DB-004).

## Procedure

1. Run DB-001 (or reuse its output if already available) and require
   `target-entity` to exist in that inventory — fail closed if it doesn't.
2. Apply only the changes described in `entity-spec`: add fields, add
   relations, widen a column, add an index. Reject narrowing, removal, or
   relation removal — this is the `schema:no-destructive-diff` gate.
3. Check any newly introduced column/table names against MySQL 8's
   reserved-word list (see DB-002 for the list) — same
   `schema:no-reserved-word-identifiers` gate applies here.
4. Preserve existing annotations, formatting, and unrelated fields exactly —
   this is a targeted edit, not a regeneration of the file.
5. Do **not** run DB-004 automatically, for the same reason as DB-002.

## Adapter notes (Symfony 4.4 / MySQL 8.0)

- Same PHP/annotation constraints as DB-001 — see that doc.
