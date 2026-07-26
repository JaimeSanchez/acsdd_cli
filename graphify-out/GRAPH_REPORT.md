# Graph Report - .  (2026-07-26)

## Corpus Check
- Corpus is ~9,474 words - fits in a single context window. You may not need a graph.

## Summary
- 151 nodes · 246 edges · 16 communities (14 shown, 2 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 29 edges (avg confidence: 0.83)
- Token cost: 159,405 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Capability Manifest Validation|Capability Manifest Validation]]
- [[_COMMUNITY_CLI Command Router|CLI Command Router]]
- [[_COMMUNITY_Profile Validation|Profile Validation]]
- [[_COMMUNITY_Stack Detection|Stack Detection]]
- [[_COMMUNITY_Discovery Report Generation|Discovery Report Generation]]
- [[_COMMUNITY_Doctrine Capability Docs|Doctrine Capability Docs]]
- [[_COMMUNITY_Profile Generator|Profile Generator]]
- [[_COMMUNITY_Convention Detection|Convention Detection]]
- [[_COMMUNITY_Catalog Builder|Catalog Builder]]
- [[_COMMUNITY_Architecture Detection|Architecture Detection]]
- [[_COMMUNITY_Health Metrics|Health Metrics]]
- [[_COMMUNITY_Package Overview|Package Overview]]

## God Nodes (most connected - your core abstractions)
1. `ProfileGenerator` - 15 edges
2. `main()` - 15 edges
3. `validate_manifest()` - 11 edges
4. `load_manifest()` - 10 edges
5. `StackDetector` - 10 edges
6. `validate_catalog()` - 9 edges
7. `validate_profile_file()` - 9 edges
8. `_load_all()` - 8 edges
9. `capability_validate()` - 8 edges
10. `catalog_verify()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `DB-001: Identify Doctrine Entities` --references--> `ProfileValidationResult`  [AMBIGUOUS]
  capabilities/_manifests/DB-001.yaml → src/acsdd/profile/validator.py
- `ProfileGenerator` --shares_data_with--> `DB-001: Identify Doctrine Entities`  [INFERRED]
  src/acsdd/profile/_discovery_impl.py → capabilities/_manifests/DB-001.yaml
- `build_catalog_markdown()` --references--> `Capability Catalog — Inventory (generated)`  [EXTRACTED]
  src/acsdd/catalog/builder.py → capabilities/CATALOG.md
- `ACSDD CLI README` --references--> `capability()`  [EXTRACTED]
  README.md → src/acsdd/cli.py
- `ACSDD CLI README` --references--> `catalog()`  [EXTRACTED]
  README.md → src/acsdd/cli.py

## Hyperedges (group relationships)
- **ACSDD CLI Command Groups** — acsdd_cli_cli, acsdd_cli_capability, acsdd_cli_catalog, acsdd_cli_profile [EXTRACTED 1.00]
- **PROFILE-001 Repository Discovery Pipeline** — profile_discovery_impl_stackdetector, profile_discovery_impl_symfonystructuredetector, profile_discovery_impl_conventiondetector, profile_discovery_impl_architecturedetector, profile_discovery_impl_healthmetrics, profile_discovery_impl_profilegenerator [EXTRACTED 1.00]
- **DB-00x Doctrine Capability Family (shared DB-001 prerequisite)** — manifests_db_001, manifests_db_002, manifests_db_003, manifests_db_004 [EXTRACTED 1.00]

## Communities (16 total, 2 thin omitted)

### Community 0 - "Capability Manifest Validation"
Cohesion: 0.09
Nodes (30): capability_validate(), Validate one manifest (PATH) or every manifest in --manifests-dir     against th, iter_manifests(), load_manifest(), ManifestLoadError, Loads capability manifest YAML files from a directory or single path., Raised when a manifest file can't be parsed at all (bad YAML, etc.)., Load a single manifest file and return its raw dict (not yet     schema-validate (+22 more)

### Community 1 - "CLI Command Router"
Cohesion: 0.12
Nodes (24): capability(), capability_list(), capability_show(), catalog(), catalog_build(), catalog_verify(), cli(), _default_capabilities_dir() (+16 more)

### Community 2 - "Profile Validation"
Cohesion: 0.17
Nodes (13): profile_discover(), profile_validate(), Run PROFILE-001 discovery against REPO_PATH (wraps the existing     profile-disc, Validate a Profile YAML file against the Appendix B schema., get_schema(), ProfileValidationResult, Validates an ACSDD Engineering Profile YAML file against the Appendix B JSON Sch, validate_profile_file() (+5 more)

### Community 3 - "Stack Detection"
Cohesion: 0.25
Nodes (5): Returns raw match score per stack_id for this detector's path., Backward-compatible single-winner detection.         Returns (stack_id, stack_in, Returns the best-scoring stack per role, e.g.         {"backend": ("php-symfony", Detects technology stack(s) from repository files.      A repo is not assumed to, StackDetector

### Community 4 - "Discovery Report Generation"
Cohesion: 0.27
Nodes (8): generate_discovery_report(), generate_recommendations(), main(), _normalize_version(), Symfony-specific enrichment, invoked only when the backend stack is     php-symf, Generate the discovery report markdown., Generate recommendations markdown., SymfonyStructureDetector

### Community 5 - "Doctrine Capability Docs"
Cohesion: 0.44
Nodes (10): Capability Catalog — Inventory (generated), Procedure doc: Create Doctrine Entity, Procedure doc: Create Doctrine Migration, Procedure doc: Identify Doctrine Entities, Procedure doc: Update Doctrine Entity, DB-001: Identify Doctrine Entities, DB-002: Create Doctrine Entity, DB-003: Update Doctrine Entity (+2 more)

### Community 6 - "Profile Generator"
Cohesion: 0.33
Nodes (3): ProfileGenerator, Generates the ACSDD Engineering Profile draft., Only present when the backend stack is php-symfony. Surfaces         directory-s

### Community 7 - "Convention Detection"
Cohesion: 0.29
Nodes (5): ConventionDetector, prune_excluded_dirs(), Detects code conventions and quality gates., Returns dict of detected tools by category., In-place filter for os.walk's dirs list, skipping vendored/build dirs.

### Community 8 - "Catalog Builder"
Cohesion: 0.29
Nodes (7): build_catalog_markdown(), _doc_link(), Builds capabilities/CATALOG.md directly from the manifest files under capabiliti, Best-effort: find a procedure doc under capabilities/<category-dir>/     whose c, manifests: {capability_id: raw_manifest_dict}, already schema-valid., test_build_catalog_markdown_empty_categories_marked_none_yet(), test_build_catalog_markdown_lists_all_capabilities()

### Community 9 - "Architecture Detection"
Cohesion: 0.4
Nodes (3): ArchitectureDetector, Detects architecture patterns from directory structure., Returns (pattern, confidence).

## Ambiguous Edges - Review These
- `ProfileValidationResult` → `DB-001: Identify Doctrine Entities`  [AMBIGUOUS]
  capabilities/_manifests/DB-001.yaml · relation: references

## Knowledge Gaps
- **44 isolated node(s):** `acsdd — command-line tool for the ACSDD framework.  Subcommand groups:   acsdd c`, `Walk up from cwd looking for a capabilities/_manifests dir, falling     back to`, `ACSDD — AI-Collaborative Software Development & Delivery CLI.`, `Work with individual capability manifests.`, `Returns ({capability_id: raw_dict}, [load_errors]).` (+39 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `ProfileValidationResult` and `DB-001: Identify Doctrine Entities`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `ProfileGenerator` connect `Profile Generator` to `Stack Detection`, `Discovery Report Generation`, `Doctrine Capability Docs`, `Convention Detection`, `Architecture Detection`, `Health Metrics`?**
  _High betweenness centrality (0.268) - this node is a cross-community bridge._
- **Why does `DB-001: Identify Doctrine Entities` connect `Doctrine Capability Docs` to `Profile Validation`, `Profile Generator`?**
  _High betweenness centrality (0.253) - this node is a cross-community bridge._
- **Why does `main()` connect `Discovery Report Generation` to `Profile Validation`, `Stack Detection`, `Profile Generator`, `Convention Detection`, `Architecture Detection`, `Health Metrics`?**
  _High betweenness centrality (0.241) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `ProfileGenerator` (e.g. with `DB-001: Identify Doctrine Entities` and `SymfonyStructureDetector`) actually correct?**
  _`ProfileGenerator` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `validate_manifest()` (e.g. with `test_validate_manifest_passes()` and `test_validate_manifest_bad_id_pattern()`) actually correct?**
  _`validate_manifest()` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `load_manifest()` (e.g. with `test_load_manifest()` and `test_load_manifest_bad_yaml()`) actually correct?**
  _`load_manifest()` has 5 INFERRED edges - model-reasoned connections that need verification._