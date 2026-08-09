# DB-002: Create Doctrine Entity

**Manifest:** `_manifests/DB-002.yaml`
**Adapter:** `DB-002-PHP-SYM44-MYSQL8` (Symfony 4.4, Doctrine ORM, MySQL 8.0)
**Depends on:** DB-001 (entity inventory)

## Scope

Creates a new entity only. Never touches an existing entity (that's DB-003)
and never generates a migration (that's DB-004).

## Procedure

1. Run DB-001 (or reuse its output if already available in session context)
   to get the current entity inventory.
2. Validate `entity-spec` describes: entity name, table name (or let
   Doctrine default it), fields with types, and relations.
3. Check the name doesn't collide with an entity found in DB-001's inventory.
4. Check the proposed table name and every column name against MySQL 8's
   reserved-word list (MySQL 8 added several new reserved words vs. 5.7,
   mostly around window functions — e.g. `RANK`, `GROUPS`, `LEAD`, `LAG`,
   `CUME_DIST`). A collision doesn't have to block the operation, but it
   must be flagged so the identifier gets backtick-quoted consistently
   rather than failing at migration time. This is the
   `schema:no-reserved-word-identifiers` quality gate.
5. Generate `src/Entity/{Name}.php` with annotation mapping consistent with
   the conventions already present in `src/Entity/` — match existing style:
   explicit typed properties, getters/setters, repository class association
   (`@ORM\Entity(repositoryClass=...)`). Do **not** use constructor property
   promotion or `readonly` properties — both are PHP 8.0+ features and this
   adapter targets Symfony 4.4's PHP `^7.1.3` constraint.
6. Generate the matching `src/Repository/{Name}Repository.php` if the repo's
   convention (per DB-001's scan) is one repository per entity.
7. Do **not** run DB-004 automatically — migration generation is a separate,
   explicit step so the diff can be reviewed first.

## Adapter notes (Symfony 4.4 / MySQL 8.0)

- Same PHP/annotation constraints as DB-001 — see that doc.
- **MySQL 8 reserved words.** A column or table name that was safe under 5.7
  can silently need escaping under 8.0. See step 4 above.
