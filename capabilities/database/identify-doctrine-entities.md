# DB-001: Identify Doctrine Entities

**Manifest:** `capabilities/_manifests/DB-001.yaml`
**Adapter:** `DB-001-PHP-SYM44-MYSQL8` (Symfony 4.4, Doctrine ORM, MySQL 8.0)
**Depended on by:** DB-002, DB-003, DB-004

## Scope

Read-only. Makes no file or database changes. This is the shared prerequisite
step: the other three DB capabilities all declare a dependency on this one
rather than re-implementing entity discovery.

## Procedure

1. Scan `src/Entity/` for classes annotated with `@ORM\Entity` (Symfony 4.4 /
   Doctrine 2.6+ uses annotations, not PHP 8 attributes — this adapter
   targets annotation-based mapping).
2. For each entity, extract: table name, fields (name, type, nullable,
   length), relations (`OneToMany`, `ManyToOne`, `ManyToMany`, `OneToOne`
   with target entity + join column), and indexes/unique constraints.
3. Cross-check against the live MySQL schema via
   `bin/console doctrine:schema:validate --skip-sync` (mapping validity) and,
   if a DB connection is available in the session context,
   `doctrine:schema:validate` (full sync check) to flag any existing drift
   between entities and the actual database — this drift must be surfaced in
   the output, not silently reconciled.
4. If `target-entity` is given, filter the inventory to that class; otherwise
   inventory all entities.
5. Emit `entity-inventory` (JSON) and a human-readable summary in
   `diff-report` noting any entity/schema drift found.

## Adapter notes (Symfony 4.4 / MySQL 8.0)

- Symfony 4.4 requires PHP `^7.1.3`; this adapter assumes PHP 7.2–7.4 and
  does not assume PHP 8 compatibility.
- Uses annotation-based mapping (`doctrine/annotations`) — not PHP 8
  attributes, which weren't the convention until Symfony 6.2+.
- **Note on the MySQL version in the profile:** "MySQL 8.15" isn't a MySQL
  release string — MySQL 8.0's point releases go up to `8.0.46` (which
  reached End-of-Life in April 2026), and the next LTS is `8.4`. Confirm the
  exact patch version for the profile.
