#!/usr/bin/env python3
"""
ACSDD Profile Discovery Tool
Implements PROFILE-001: Generate Repository Engineering Profile

Usage:
    python acsdd-profile-discovery.py /path/to/repo --profile-id my-project

Outputs:
    - profiles/{profile-id}-draft.yaml
    - profiles/{profile-id}-discovery-report.md
    - profiles/{profile-id}-recommendations.md
"""

import os
import sys
import json
import yaml
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import Dict, List, Optional, Tuple

from acsdd.paths import DEFAULT_PROFILES_DIR

try:
    import tomllib  # stdlib, Python 3.11+
except ImportError:  # pragma: no cover - fallback for older interpreters
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None


# Directories that are never part of "the repo's own code" for detection
# purposes. Vendored/installed dependencies routinely ship their own
# .eslintrc, phpunit.xml.dist, phpstan.neon.dist, etc. Walking into them
# produces false-positive convention detections and slows large repos down
# for no benefit, so every walk in this tool shares this exclusion list.
EXCLUDED_DIRS = {
    "node_modules", "vendor", ".git", "target", "build", "dist",
    ".next", ".nuxt", "__pycache__", ".venv", "venv", ".tox",
    "coverage", ".pytest_cache", ".mypy_cache", "bower_components",
    # Output/cache directories left behind by AI-tooling runs (not
    # dot-prefixed, so they'd otherwise be walked as application code).
    "graphify-out",
}

# Common monorepo layout hints. When a single stack scan at the repo root
# can't find both a backend and a frontend, we probe these subdirectories
# too before giving up on the missing role.
MONOREPO_SUBDIR_CANDIDATES = [
    "backend", "server", "api", "app/backend",
    "frontend", "client", "web", "ui", "app/frontend",
]


def _is_excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIRS or (name.startswith(".") and name != ".github")


def prune_excluded_dirs(dirs: List[str]) -> None:
    """In-place filter for os.walk's dirs list, skipping vendored/build dirs."""
    dirs[:] = [d for d in dirs if not _is_excluded_dir(d)]


class StackDetector:
    """Detects technology stack(s) from repository files.

    A repo is not assumed to be single-stack: a Symfony backend with a
    React frontend in the same repository is a common, valid layout, so
    detection scores every signature and keeps the best match per *role*
    (backend / frontend) rather than a single global winner.
    """

    STACK_SIGNATURES = {
        "php-symfony": {
            "files": ["composer.json", "symfony.lock", "config/packages"],
            "deps": ["symfony/framework-bundle", "symfony/symfony"],
            "language": "php",
            "role": "backend",
        },
        "php-laravel": {
            "files": ["composer.json", "artisan", "bootstrap/app.php"],
            "deps": ["laravel/framework"],
            "language": "php",
            "role": "backend",
        },
        "node-express": {
            "files": ["package.json"],
            "deps": ["express"],
            "language": "javascript",
            "role": "backend",
        },
        "node-nestjs": {
            "files": ["package.json", "nest-cli.json"],
            "deps": ["@nestjs/core"],
            "language": "typescript",
            "role": "backend",
        },
        "node-react": {
            "files": ["package.json"],
            "deps": ["react", "react-dom"],
            "language": "javascript",
            "role": "frontend",
        },
        "node-vue": {
            "files": ["package.json"],
            "deps": ["vue"],
            "language": "javascript",
            "role": "frontend",
        },
        "python-django": {
            "files": ["requirements.txt", "manage.py", "settings.py"],
            "deps": ["django"],
            "language": "python",
            "role": "backend",
        },
        "python-fastapi": {
            "files": ["requirements.txt", "pyproject.toml"],
            "deps": ["fastapi"],
            "language": "python",
            "role": "backend",
        },
        "go-standard": {
            "files": ["go.mod", "go.sum"],
            "deps": [],
            "language": "go",
            "role": "backend",
        },
        "rust-actix": {
            "files": ["Cargo.toml", "Cargo.lock"],
            "deps": ["actix-web"],
            "language": "rust",
            "role": "backend",
        },
        "java-spring": {
            "files": ["pom.xml", "build.gradle"],
            "deps": ["spring-boot"],
            "language": "java",
            "role": "backend",
        },
    }

    # A signature file alone (score 2) is weak evidence; a confirmed
    # dependency hit (score 3) is strong. This ceiling is used to turn raw
    # scores into a 0-100 confidence figure instead of an unbounded number.
    MAX_EXPECTED_SCORE = 5

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.manifest_data = {}

    def score_all(self) -> Dict[str, int]:
        """Returns raw match score per stack_id for this detector's path."""
        scores = {}
        for stack_id, sig in self.STACK_SIGNATURES.items():
            score = 0
            for sig_file in sig["files"]:
                if (self.repo_path / sig_file).exists():
                    score += 2
            manifest = self._read_manifest(sig["language"])
            if manifest and sig["deps"]:
                deps = self._extract_deps(manifest, sig["language"])
                for dep in sig["deps"]:
                    if any(dep in d for d in deps):
                        score += 3
            scores[stack_id] = score
        return scores

    def detect(self) -> Tuple[str, Dict]:
        """Backward-compatible single-winner detection.
        Returns (stack_id, stack_info) or ('unknown', {})."""
        scores = self.score_all()
        if not scores or max(scores.values()) == 0:
            return "unknown", {}
        best = max(scores, key=scores.get)
        return best, self.STACK_SIGNATURES[best]

    def detect_by_role(self) -> Dict[str, Tuple[str, Dict, float]]:
        """Returns the best-scoring stack per role, e.g.
        {"backend": ("php-symfony", {...}, 0.8), "frontend": ("node-react", {...}, 0.6)}
        Roles with no positive score are omitted."""
        scores = self.score_all()
        by_role: Dict[str, Tuple[str, Dict, int]] = {}
        for stack_id, score in scores.items():
            if score <= 0:
                continue
            role = self.STACK_SIGNATURES[stack_id]["role"]
            if role not in by_role or score > by_role[role][2]:
                by_role[role] = (stack_id, self.STACK_SIGNATURES[stack_id], score)

        result = {}
        for role, (stack_id, info, score) in by_role.items():
            confidence = round(min(score / self.MAX_EXPECTED_SCORE, 1.0), 2)
            result[role] = (stack_id, info, confidence)
        return result

    def _read_manifest(self, language: str) -> Optional[Dict]:
        if language in ("php",):
            path = self.repo_path / "composer.json"
        elif language in ("javascript", "typescript"):
            path = self.repo_path / "package.json"
        elif language == "python":
            path = self.repo_path / "requirements.txt"
            if not path.exists():
                path = self.repo_path / "pyproject.toml"
        elif language == "go":
            path = self.repo_path / "go.mod"
        elif language == "rust":
            path = self.repo_path / "Cargo.toml"
        elif language == "java":
            path = self.repo_path / "pom.xml"
        else:
            return None

        if not path.exists():
            return None

        try:
            if path.suffix == ".json":
                return json.loads(path.read_text())
            elif path.suffix == ".toml":
                if tomllib is not None:
                    try:
                        return {"parsed": tomllib.loads(path.read_text())}
                    except Exception:
                        pass
                # No TOML parser available — fall back to raw text so
                # substring matching still works, just less precisely.
                return {"raw": path.read_text()}
            elif path.suffix == ".txt":
                return {"raw": path.read_text()}
            elif path.suffix == ".xml":
                return {"raw": path.read_text()}
        except Exception:
            return None
        return None

    def _extract_deps(self, manifest: Dict, language: str) -> List[str]:
        if language == "php" and "require" in manifest:
            return list(manifest["require"].keys())
        elif language in ("javascript", "typescript"):
            deps = []
            for key in ("dependencies", "devDependencies"):
                if key in manifest:
                    deps.extend(manifest[key].keys())
            return deps
        elif language == "python":
            if "parsed" in manifest:
                data = manifest["parsed"]
                deps = list(data.get("project", {}).get("dependencies", []))
                poetry_deps = (
                    data.get("tool", {}).get("poetry", {}).get("dependencies", {})
                )
                deps.extend(poetry_deps.keys())
                return deps
            if "raw" in manifest:
                return [line.strip() for line in manifest["raw"].split("\n") if line.strip()]
        return []


class SymfonyStructureDetector:
    """Symfony-specific enrichment, invoked only when the backend stack is
    php-symfony. General StackDetector signatures only tell us "this is
    Symfony" — this fills in the details ACSDD's profile actually needs:
    which version, which directory-structure generation, and whether the
    repo still carries pre-Flex/bundle-wrapper patterns that Symfony's own
    Best Practices guide recommends moving away from.

    Reference: https://symfony.com/doc/current/configuration.html,
    https://symfony.com/doc/current/best_practices.html
    """

    # Doctrine-family packages whose exact resolved version is useful enough
    # to surface in technology_stack.additional (schema field otherwise never
    # populated) — deliberately not every symfony.lock entry, which would be
    # mostly noise (minor symfony/* micro-components).
    _ADDITIONAL_PACKAGES = [
        "doctrine/orm",
        "doctrine/dbal",
        "doctrine/doctrine-bundle",
        "doctrine/doctrine-migrations-bundle",
    ]

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def detect(self) -> Dict:
        result = {
            "version": None,
            "version_constraint": None,
            "structure": "unknown",   # "flex" | "legacy"
            "uses_bundle_wrapper": False,
            "orm": None,
            "public_dir": None,
            "uses_dotenv": False,
            "additional_packages": [],
        }

        composer_path = self.repo_path / "composer.json"
        if composer_path.exists():
            try:
                composer = json.loads(composer_path.read_text())
                require = composer.get("require", {})
                constraint = require.get("symfony/framework-bundle") or require.get("symfony/symfony")
                if constraint:
                    result["version_constraint"] = constraint
                    result["version"] = self._normalize_version(constraint)
                if "doctrine/orm" in require or "doctrine/doctrine-bundle" in require:
                    result["orm"] = "doctrine"
            except Exception:
                pass

        # composer.lock records what Composer actually resolved, not just the
        # declared range in composer.json — e.g. a "^6.0" constraint could
        # resolve anywhere from 6.0 to 6.4.x. Prefer the exact resolved
        # version when available; fall back to the constraint-derived value
        # above if the lock file is missing, unparseable, or doesn't list the
        # package (shouldn't happen for a valid lock, but degrade quietly).
        resolved = self._read_composer_lock()
        resolved_symfony = resolved.get("symfony/framework-bundle") or resolved.get("symfony/symfony")
        if resolved_symfony:
            result["version"] = resolved_symfony
        result["additional_packages"] = [
            {"name": pkg, "version": resolved[pkg]}
            for pkg in self._ADDITIONAL_PACKAGES
            if pkg in resolved
        ]

        # Flex (Symfony 3.4+/4.x+) markers vs. legacy pre-Flex markers.
        # See https://symfony.com/doc/current/configuration.html (config/packages,
        # config/bundles.php) vs the Symfony 2.x/3.x app/ layout. symfony.lock is
        # the most authoritative signal — Flex always writes it — so it's included
        # alongside the directory-structure markers rather than replacing them.
        flex_markers = [
            self.repo_path / "config" / "packages",
            self.repo_path / "config" / "bundles.php",
            self.repo_path / "public" / "index.php",
            self.repo_path / "symfony.lock",
        ]
        legacy_markers = [
            self.repo_path / "app" / "AppKernel.php",
            self.repo_path / "app" / "config" / "config.yml",
            self.repo_path / "web" / "app.php",
        ]

        if any(m.exists() for m in flex_markers):
            result["structure"] = "flex"
        elif any(m.exists() for m in legacy_markers):
            result["structure"] = "legacy"

        if (self.repo_path / "public").is_dir():
            result["public_dir"] = "public"
        elif (self.repo_path / "web").is_dir():
            result["public_dir"] = "web"

        result["uses_dotenv"] = (self.repo_path / ".env").exists()

        # Best Practices (4.x) recommends a flat src/ (src/Entity,
        # src/Controller, ...) instead of wrapping everything in a single
        # AppBundle, a pattern left over from the pre-Symfony-4 bundle
        # convention.
        src_dir = self.repo_path / "src"
        if src_dir.is_dir():
            for child in src_dir.iterdir():
                if child.is_dir() and child.name.endswith("Bundle"):
                    result["uses_bundle_wrapper"] = True
                    break

        return result

    @staticmethod
    def _normalize_version(constraint: str) -> Optional[str]:
        import re
        match = re.search(r"(\d+)\.(\d+)", constraint)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
        return None

    def _read_composer_lock(self) -> Dict[str, str]:
        """package name -> resolved version (leading 'v' stripped), read
        from both packages and packages-dev. Empty dict if composer.lock is
        missing or unparseable — callers must fall back to composer.json's
        declared constraints in that case."""
        lock_path = self.repo_path / "composer.lock"
        if not lock_path.exists():
            return {}
        try:
            lock = json.loads(lock_path.read_text())
            versions = {}
            for section in ("packages", "packages-dev"):
                for pkg in lock.get(section, []):
                    name = pkg.get("name")
                    version = pkg.get("version")
                    if name and version:
                        versions[name] = version.lstrip("v")
            return versions
        except Exception:
            return {}


class DatabaseDetector:
    """Stack-agnostic detection of the database engine actually in use.

    ORM/framework signatures (e.g. SymfonyStructureDetector's ``orm:
    doctrine``) only say *how* the app talks to a database, not *which*
    one — Doctrine, SQLAlchemy, and ActiveRecord all support multiple
    engines, and the tool's own bundled example capabilities assume MySQL
    while plenty of real Symfony/Doctrine repos run PostgreSQL. This checks
    the artifacts that actually pin down the choice: a DATABASE_URL-style
    connection string in an env file, or a docker-compose service image.
    """

    _ENV_FILES = [".env", ".env.dist", ".env.example", ".env.test"]
    _COMPOSE_FILES = [
        "docker-compose.yml", "docker-compose.yaml",
        "compose.yml", "compose.yaml",
        "docker-compose.override.yml", "compose.override.yaml",
    ]
    _SCHEME_TO_ENGINE = {
        "mysql": "mysql",
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "sqlite": "sqlite",
        "sqlsrv": "sqlserver",
        "mssql": "sqlserver",
    }
    _IMAGE_TO_ENGINE = {
        "postgres": "postgresql",
        "mysql": "mysql",
        "mariadb": "mariadb",
        "mongo": "mongodb",
        "redis": "redis",
    }

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def detect(self) -> Optional[str]:
        return self._from_env_files() or self._from_compose_files()

    def _from_env_files(self) -> Optional[str]:
        import re
        for name in self._ENV_FILES:
            path = self.repo_path / name
            if not path.exists():
                continue
            try:
                content = path.read_text(errors="ignore")
            except Exception:
                continue
            match = re.search(r"DATABASE_URL\s*=\s*\"?([a-zA-Z][a-zA-Z0-9+]*)://", content)
            if match:
                scheme = match.group(1).split("+")[0].lower()
                if scheme in self._SCHEME_TO_ENGINE:
                    return self._SCHEME_TO_ENGINE[scheme]
        return None

    def _from_compose_files(self) -> Optional[str]:
        import re
        for name in self._COMPOSE_FILES:
            path = self.repo_path / name
            if not path.exists():
                continue
            try:
                content = path.read_text(errors="ignore")
            except Exception:
                continue
            for line in content.splitlines():
                match = re.search(r"image:\s*[\"']?([a-zA-Z0-9._/-]+)", line)
                if not match:
                    continue
                image = match.group(1).lower()
                for keyword, engine in self._IMAGE_TO_ENGINE.items():
                    if keyword in image:
                        return engine
        return None


class ConventionDetector:
    """Detects code conventions and quality gates."""

    CONFIG_MAP = {
        # Linters & Formatters
        ".eslintrc": ("eslint", "linter"),
        ".eslintrc.js": ("eslint", "linter"),
        ".eslintrc.json": ("eslint", "linter"),
        "phpstan.neon": ("phpstan", "static-analysis"),
        "phpstan.neon.dist": ("phpstan", "static-analysis"),
        "phpstan.dist.neon": ("phpstan", "static-analysis"),
        ".php_cs.dist": ("php-cs-fixer", "formatter"),
        ".php-cs-fixer.dist.php": ("php-cs-fixer", "formatter"),
        "phpcs.xml": ("phpcs", "linter"),
        "phpcs.xml.dist": ("phpcs", "linter"),
        ".pylintrc": ("pylint", "linter"),
        "pyproject.toml": ("black/ruff", "formatter"),
        "clippy.toml": ("clippy", "linter"),
        "checkstyle.xml": ("checkstyle", "linter"),

        # Test Configs
        "phpunit.xml": ("phpunit", "test-framework"),
        "phpunit.xml.dist": ("phpunit", "test-framework"),
        "jest.config.js": ("jest", "test-framework"),
        "vitest.config.ts": ("vitest", "test-framework"),
        "pytest.ini": ("pytest", "test-framework"),
        "go.mod": ("go test", "test-framework"),

        # CI/CD
        ".github/workflows": ("github-actions", "ci-cd"),
        ".gitlab-ci.yml": ("gitlab-ci", "ci-cd"),
        "azure-pipelines.yml": ("azure-pipelines", "ci-cd"),
        "Jenkinsfile": ("jenkins", "ci-cd"),

        # Security
        ".snyk": ("snyk", "security-scan"),
        ".trivyignore": ("trivy", "security-scan"),
        ".gitleaks.toml": ("gitleaks", "security-scan"),

        # General
        ".editorconfig": ("editorconfig", "convention"),
        ".pre-commit-config.yaml": ("pre-commit", "git-hook"),
        "Makefile": ("make", "build-system"),
        "Taskfile.yml": ("task", "build-system"),
    }

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.findings = []

    def detect(self) -> Dict:
        """Returns dict of detected tools by category."""
        results = {
            "linters": [],
            "formatters": [],
            "static_analysis": [],
            "test_frameworks": [],
            "ci_cd": [],
            "security_scans": [],
            "build_systems": [],
            "git_hooks": [],
            "conventions": [],
        }

        file_patterns = {k: v for k, v in self.CONFIG_MAP.items() if "/" not in k}
        dir_patterns = {k: v for k, v in self.CONFIG_MAP.items() if "/" in k}

        for root, dirs, files in os.walk(self.repo_path):
            prune_excluded_dirs(dirs)

            rel_root = Path(root).relative_to(self.repo_path)

            for filename in files:
                rel_path = rel_root / filename

                if filename in file_patterns:
                    tool, category = file_patterns[filename]
                    self._add_finding(results, category, tool, str(rel_path))

        # Directory-based patterns (e.g. ".github/workflows") are checked
        # once against the filesystem directly rather than matched inside
        # the file walk above — matching per-file there was comparing a
        # directory path against arbitrary filenames and could never
        # succeed, silently missing CI/CD detection entirely.
        for pattern, (tool, category) in dir_patterns.items():
            target_dir = self.repo_path / pattern
            if target_dir.is_dir() and any(target_dir.iterdir()):
                self._add_finding(results, category, tool, pattern)

        return results

    def _add_finding(self, results: Dict, category: str, tool: str, path: str):
        mapping = {
            "linter": "linters",
            "formatter": "formatters",
            "static-analysis": "static_analysis",
            "test-framework": "test_frameworks",
            "ci-cd": "ci_cd",
            "security-scan": "security_scans",
            "build-system": "build_systems",
            "git-hook": "git_hooks",
            "convention": "conventions",
        }
        key = mapping.get(category)
        if key:
            results[key].append({"tool": tool, "config": path})


class ArchitectureDetector:
    """Detects architecture patterns from directory structure."""

    PATTERNS = {
        "layered": ["Controller", "Service", "Repository", "Entity", "Model"],
        "hexagonal": ["Domain", "Application", "Infrastructure", "Ports", "Adapters"],
        "feature-based": ["features/", "modules/", "domains/"],
        "mvc": ["Models", "Views", "Controllers"],
    }

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def detect(self) -> Tuple[str, float]:
        """Returns (pattern, confidence). When two or more patterns tie for
        the top score, ``pattern`` is a "+"-joined label (e.g.
        "layered+hexagonal") naming all of them rather than silently picking
        one by dict-insertion order — a real layout can legitimately match
        more than one convention (e.g. Controller/Entity/Repository alongside
        Domain/Application/Infrastructure), and reporting a single winner at
        high confidence in that case is misleading."""
        src_dirs = self._find_source_dirs()
        if not src_dirs:
            return "unknown", 0.0

        scores = {p: 0 for p in self.PATTERNS}
        total_dirs = 0

        for src_dir in src_dirs:
            for item in src_dir.iterdir():
                if item.is_dir() and not _is_excluded_dir(item.name):
                    total_dirs += 1
                    name = self._normalize(item.name)
                    for pattern, keywords in self.PATTERNS.items():
                        for kw in keywords:
                            if self._normalize(kw) == name:
                                scores[pattern] += 1

        if total_dirs == 0:
            return "unknown", 0.0

        max_score = max(scores.values())
        if max_score == 0:
            return "unknown", 0.0

        best = "+".join(p for p in self.PATTERNS if scores[p] == max_score)
        confidence = min(max_score / max(total_dirs * 0.3, 1), 1.0)
        return best, round(confidence, 2)

    @staticmethod
    def _normalize(token: str) -> str:
        """Lowercase, strip a trailing slash and a trailing plural 's' so
        singular/plural keyword variants (Controller/Controllers,
        domain/domains) compare equal without resorting to an unanchored,
        bidirectional substring match (which would let e.g. a directory
        named "M" match several unrelated keywords)."""
        token = token.lower().rstrip("/")
        if len(token) > 1 and token.endswith("s"):
            token = token[:-1]
        return token

    def _find_source_dirs(self) -> List[Path]:
        candidates = [
            self.repo_path / "src",
            self.repo_path / "app",
            self.repo_path / "lib",
            self.repo_path / "packages",
        ]
        return [c for c in candidates if c.exists() and c.is_dir()]


class HealthMetrics:
    """Extracts repository health metrics."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def analyze(self) -> Dict:
        metrics = {
            "total_files": 0,
            "code_files": 0,
            "test_files": 0,
            "doc_files": 0,
            "languages": Counter(),
            "has_readme": False,
            "has_contributing": False,
            "has_changelog": False,
            "coverage": None,
            "tech_debt_score": "unknown",
        }

        test_patterns = ("test", "spec", "__tests__", "tests/")
        doc_patterns = (".md", ".rst", ".txt")
        code_extensions = {
            ".php": "php", ".js": "javascript", ".ts": "typescript",
            ".py": "python", ".go": "go", ".rs": "rust",
            ".java": "java", ".kt": "kotlin", ".scala": "scala",
            ".rb": "ruby", ".ex": "elixir", ".swift": "swift",
        }

        for root, dirs, files in os.walk(self.repo_path):
            prune_excluded_dirs(dirs)

            for f in files:
                metrics["total_files"] += 1
                fpath = Path(root) / f

                # README check
                if f.lower().startswith("readme"):
                    metrics["has_readme"] = True
                if f.lower() == "contributing.md":
                    metrics["has_contributing"] = True
                if f.lower().startswith("changelog"):
                    metrics["has_changelog"] = True

                # Language detection
                ext = fpath.suffix.lower()
                if ext in code_extensions:
                    metrics["code_files"] += 1
                    metrics["languages"][code_extensions[ext]] += 1

                # Test file detection
                rel_path = str(fpath.relative_to(self.repo_path))
                if any(p in rel_path.lower() for p in test_patterns):
                    metrics["test_files"] += 1

                # Doc files
                if ext in doc_patterns:
                    metrics["doc_files"] += 1

        # Coverage detection
        coverage_files = [
            self.repo_path / "coverage" / "index.html",
            self.repo_path / ".coverage",
            self.repo_path / "coverage.xml",
            self.repo_path / "clover.xml",
        ]
        for cf in coverage_files:
            if cf.exists():
                metrics["coverage"] = self._parse_coverage(cf)
                break

        # Tech debt heuristic
        metrics["tech_debt_score"] = self._assess_tech_debt(metrics)

        return metrics

    def _parse_coverage(self, path: Path) -> Optional[float]:
        try:
            if path.name == "index.html":
                content = path.read_text()
                # Very naive extraction
                for line in content.split("\n"):
                    if "%" in line and any(c.isdigit() for c in line):
                        import re
                        matches = re.findall(r'(\d+(?:\.\d+)?)%', line)
                        if matches:
                            return float(matches[0])
                return None
            if path.name == "coverage.xml":
                # Cobertura: <coverage line-rate="0.87" ...>
                root = ET.parse(path).getroot()
                line_rate = root.get("line-rate")
                if line_rate is not None:
                    return float(line_rate) * 100
                return None
            if path.name == "clover.xml":
                # Clover: <project><metrics statements="N" coveredstatements="M" .../></project>
                root = ET.parse(path).getroot()
                metrics = root.find(".//project/metrics")
                if metrics is not None:
                    statements = int(metrics.get("statements", 0))
                    covered = int(metrics.get("coveredstatements", 0))
                    if statements > 0:
                        return covered / statements * 100
                return None
            return None
        except Exception:
            return None

    def _assess_tech_debt(self, metrics: Dict) -> str:
        score = 0
        if not metrics["has_readme"]:
            score += 2
        if not metrics["has_contributing"]:
            score += 1
        if metrics["test_files"] == 0:
            score += 3
        elif metrics["test_files"] / max(metrics["code_files"], 1) < 0.1:
            score += 2
        if metrics["coverage"] is not None and metrics["coverage"] < 50:
            score += 2

        if score >= 5:
            return "high"
        elif score >= 3:
            return "medium"
        elif score >= 1:
            return "low"
        return "low"


class ProfileGenerator:
    """Generates the ACSDD Engineering Profile draft."""

    # Backend/full-stack capability adapter per detected stack.
    ADAPTER_MAP = {
        "php-symfony": "php-symfony-doctrine",
        "php-laravel": "php-laravel-eloquent",
        "node-express": "node-express",
        "node-nestjs": "node-nestjs-typeorm",
        "python-django": "python-django",
        "python-fastapi": "python-fastapi-sqlalchemy",
        "go-standard": "go-standard",
        "rust-actix": "rust-actix-diesel",
        "java-spring": "java-spring-jpa",
    }

    # Frontend capability adapter per detected frontend stack.
    FRONTEND_ADAPTER_MAP = {
        "node-react": "react",
        "node-vue": "vue",
    }

    def __init__(self, profile_id: str, stack: str, stack_info: Dict,
                 conventions: Dict, architecture: Tuple[str, float],
                 health: Dict, acsdd_version: str = "0.2.0",
                 frontend_stack: Optional[str] = None,
                 frontend_confidence: float = 0.0,
                 symfony_info: Optional[Dict] = None,
                 database: Optional[str] = None):
        self.profile_id = profile_id
        self.stack = stack
        self.stack_info = stack_info
        self.conventions = conventions
        self.architecture = architecture
        self.health = health
        self.acsdd_version = acsdd_version
        # A repo can legitimately have a backend stack (self.stack) *and* a
        # separately-detected frontend stack (e.g. Symfony + React in the
        # same repository) — these are tracked independently rather than
        # forcing a single winner.
        self.frontend_stack = frontend_stack
        self.frontend_confidence = frontend_confidence
        # Populated only when self.stack == "php-symfony"; see
        # SymfonyStructureDetector. Lets us auto-fill version/orm instead
        # of leaving them as [REVIEW REQUIRED] when confidently detected.
        self.symfony_info = symfony_info or {}
        # From DatabaseDetector — independent of ORM/framework, since e.g.
        # Doctrine/SQLAlchemy/ActiveRecord all support multiple engines.
        self.database = database

    def generate(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")

        # Extract tools
        linter = self._first_tool("linters")
        formatter = self._first_tool("formatters")
        static_analysis = self._first_tool("static_analysis")
        test_fw = self._first_tool("test_frameworks")
        security = self._first_tool("security_scans")

        # Coverage
        coverage = self.health.get("coverage") or 0

        if self.frontend_stack:
            frontend_value = self.frontend_stack.split("-")[-1]
            if self.frontend_confidence < 0.6:
                # The placeholder has to be a *prefix*, not a suffix — every
                # consumer (profile validate --strict, profile create's gate,
                # profile review) finds these via find_unresolved_fields(),
                # which does a str.startswith() against "[REVIEW REQUIRED".
                # Appending it left this field invisible to all three. The
                # detected value is kept inside the placeholder so the reviewer
                # still sees what was guessed.
                frontend_value = f"[REVIEW REQUIRED — low confidence, verify: {frontend_value}]"
        elif self.stack_info.get("language") in ("javascript", "typescript"):
            # The winning "backend" stack IS a JS/TS frontend framework
            # (e.g. repo only contains node-react, no separate backend).
            frontend_value = "[REVIEW REQUIRED — detect from package.json]"
        else:
            frontend_value = None

        # Symfony-specific fields, when available, replace the generic
        # placeholders — PHP version constraints are still left to human
        # review since composer.json's "php" constraint and the actual
        # deployed runtime often diverge.
        version_value = "[REVIEW REQUIRED — detect from manifest]"
        orm_value = "[REVIEW REQUIRED — infer from dependencies]"
        if self.symfony_info.get("version"):
            version_value = f"{self.symfony_info['version']} (symfony/framework-bundle: {self.symfony_info.get('version_constraint')})"
        if self.symfony_info.get("orm"):
            orm_value = self.symfony_info["orm"]

        profile = {
            "profile": {
                "meta": {
                    "id": self.profile_id,
                    "version": "0.1.0",
                    "status": "draft",
                    "last_updated": today,
                    "generated_by": "PROFILE-001 v1.0.0",
                    "acsdd_version": self.acsdd_version,
                    "confidence_score": self._calculate_confidence(),
                },
                "technology_stack": {
                    "language": self.stack_info.get("language", "unknown"),
                    "version": version_value,
                    "framework": self.stack.split("-")[1] if "-" in self.stack else "unknown",
                    "orm": orm_value,
                    "database": self.database or "[REVIEW REQUIRED — detect database engine]",
                    **({"frontend": frontend_value} if frontend_value is not None else {}),
                    **({"additional": self.symfony_info["additional_packages"]}
                       if self.symfony_info.get("additional_packages") else {}),
                },
                "engineering_standards": {
                    "code_style": formatter or linter or "[REVIEW REQUIRED]",
                    "static_analysis": static_analysis or "[REVIEW REQUIRED]",
                    "test_framework": test_fw or "[REVIEW REQUIRED]",
                    "min_coverage": coverage if coverage else 0,
                },
                "capability_configuration": {
                    "default_adapter": self.ADAPTER_MAP.get(self.stack, "[REVIEW REQUIRED]"),
                    "frontend_adapter": (
                        self.FRONTEND_ADAPTER_MAP.get(self.frontend_stack, "[REVIEW REQUIRED]")
                        if self.frontend_stack else None
                    ),
                    "overrides": {},
                },
                "ai_execution_rules": {
                    "max_files_per_session": 3,
                    "max_loc_per_session": 200,
                    "require_human_approval": [
                        "database-schema-change",
                        "api-contract-change",
                        "security-related-change",
                    ],
                    "context_priority": [
                        "active-specification",
                        "relevant-adr",
                        "similar-past-sessions",
                        "profile-conventions",
                    ],
                },
                "context_priority": {
                    "tiers": [
                        {"tier": 1, "content": "Active specification + locked version", "max_tokens": 4000},
                        {"tier": 2, "content": "Profile + relevant ADRs", "max_tokens": 2000},
                        {"tier": 3, "content": "Capability manifest + adapter", "max_tokens": 2000},
                        {"tier": 4, "content": "Similar past sessions (RAG retrieval)", "max_tokens": 1500},
                        {"tier": 5, "content": "Repository health / current file context", "max_tokens": 1500},
                    ],
                },
                "quality_gates": {
                    "automatic": self._build_gate_list(),
                    "manual": ["architecture-review", "security-review"],
                },
                "security_profile": {
                    "cross_cutting_constraints": [
                        {
                            "id": "no-hardcoded-secrets",
                            "name": "No hardcoded secrets",
                            "enforcement": "automatic" if security else "manual",
                            "tool": security or "[REVIEW REQUIRED]",
                            "rule": "No credentials, API keys, or tokens committed to source.",
                        },
                        {
                            "id": "all-inputs-validated",
                            "name": "All inputs validated",
                            "enforcement": "manual",
                            "tool": "[REVIEW REQUIRED]",
                            "rule": "User-supplied input is validated before use.",
                        },
                        {
                            "id": "sql-injection-prevention",
                            "name": "SQL injection prevention",
                            "enforcement": "manual",
                            "tool": "[REVIEW REQUIRED]",
                            "rule": "No raw SQL string concatenation with user input.",
                        },
                        {
                            "id": "xss-prevention",
                            "name": "XSS prevention",
                            "enforcement": "manual",
                            "tool": "[REVIEW REQUIRED]",
                            "rule": "Output is escaped/encoded appropriately for its context.",
                        },
                        {
                            "id": "dependency-vulnerability-scan",
                            "name": "Dependency vulnerability scan",
                            "enforcement": "automatic" if security else "manual",
                            "tool": security or "[REVIEW REQUIRED]",
                            "rule": "Dependencies are scanned for known vulnerabilities.",
                        },
                    ],
                    "mandatory_human_reviews": [
                        "auth-changes",
                        "authorization-rule-changes",
                        "payment-processing-changes",
                        "pii-handling-changes",
                        "cryptographic-implementation-changes",
                    ],
                },
                "repository_health": {
                    "current_coverage": coverage if coverage else 0,
                    "tech_debt_score": self.health.get("tech_debt_score", "unknown"),
                    "last_audit": today,
                },
                "token_budgets": {
                    "planning_phase": 4000,
                    "implementation_phase": 8000,
                    "validation_phase": 2000,
                    "max_total_per_session": 15000,
                },
                **self._symfony_structure_notes(),
            },
        }

        return yaml.dump(profile, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def _symfony_structure_notes(self) -> Dict:
        """Only present when the backend stack is php-symfony. Surfaces
        directory-structure findings that affect which capability adapter
        behavior and quality gates make sense (Flex/4.x-style vs a
        pre-Flex legacy layout), per
        https://symfony.com/doc/current/configuration.html and
        https://symfony.com/doc/current/best_practices.html."""
        if self.stack != "php-symfony" or not self.symfony_info:
            return {}
        return {
            "structure_notes": {
                "directory_structure": self.symfony_info.get("structure", "unknown"),
                "public_dir": self.symfony_info.get("public_dir"),
                "uses_dotenv": self.symfony_info.get("uses_dotenv", False),
                "uses_bundle_wrapper": self.symfony_info.get("uses_bundle_wrapper", False),
                "reference": "https://symfony.com/doc/current/best_practices.html",
            }
        }

    def _first_tool(self, category: str) -> Optional[str]:
        tools = self.conventions.get(category, [])
        return tools[0]["tool"] if tools else None

    def _build_gate_list(self) -> List[str]:
        gates = []
        if self.conventions.get("linters"):
            gates.append(f"lint:{self.conventions['linters'][0]['tool']}")
        else:
            gates.append("lint")
        if self.conventions.get("static_analysis"):
            gates.append(f"static-analysis:{self.conventions['static_analysis'][0]['tool']}")
        else:
            gates.append("type-check")
        if self.conventions.get("test_frameworks"):
            gates.append(f"test:unit-passing")
        else:
            gates.append("test:unit-passing")
        if self.conventions.get("security_scans"):
            gates.append(f"security-scan:{self.conventions['security_scans'][0]['tool']}")
        else:
            gates.append("security-scan:secrets")
        return gates

    def _calculate_confidence(self) -> int:
        # Weights sum to 100. Security scanning is included explicitly —
        # the framework treats security as a mandatory cross-cutting
        # concern (see ACSDD §7), so a repo with no secret/dependency
        # scanning at all should not be able to reach a high confidence
        # score just because it has lint + CI + tests + a README.
        score = 0
        if self.stack != "unknown":
            score += 20
        if self.conventions.get("linters") or self.conventions.get("formatters"):
            score += 20
        if self.conventions.get("ci_cd"):
            score += 15
        if self.health.get("test_files", 0) > 0:
            score += 15
        if self.health.get("has_readme"):
            score += 10
        if self.conventions.get("security_scans"):
            score += 20
        return min(score, 100)


def generate_discovery_report(repo_path: Path, profile_id: str, stack: str,
                              stack_info: Dict, conventions: Dict,
                              architecture: Tuple[str, float],
                              health: Dict, stack_confidence: float = 0.0,
                              frontend_stack: Optional[str] = None,
                              frontend_confidence: float = 0.0) -> str:
    """Generate the discovery report markdown."""
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# Discovery Report: {profile_id}",
        f"**Generated:** {today}  ",
        f"**Repository:** `{repo_path}`  ",
        f"**Analyzer:** PROFILE-001 v1.0.0  ",
        "",
        "---",
        "",
        "## 1. Stack Detection",
        "",
        f"| Property | Value |",
        f"|----------|-------|",
        f"| Detected Backend Stack | `{stack}` |",
        f"| Language | {stack_info.get('language', 'unknown')} |",
        # This is the stack detector's own confidence (signature file +
        # dependency match strength), not the unrelated architecture-
        # pattern confidence computed below.
        f"| Backend Confidence | {stack_confidence * 100:.0f}% |",
        f"| Detected Frontend Stack | `{frontend_stack or 'none detected'}` |",
    ]
    if frontend_stack:
        lines.append(f"| Frontend Confidence | {frontend_confidence * 100:.0f}% |")
    lines.extend([
        "",
        "## 2. Detected Conventions & Tools",
        "",
    ])

    for category, tools in conventions.items():
        if tools:
            lines.append(f"### {category.replace('_', ' ').title()}")
            lines.append("")
            lines.append("| Tool | Config File |")
            lines.append("|------|-------------|")
            for tool in tools:
                lines.append(f"| {tool['tool']} | `{tool['config']}` |")
            lines.append("")

    lines.extend([
        "## 3. Architecture Detection",
        "",
        f"| Property | Value |",
        f"|----------|-------|",
        f"| Detected Pattern | `{architecture[0]}` |",
        f"| Confidence | {architecture[1] * 100:.0f}% |",
        "",
        "## 4. Repository Health Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Files | {health['total_files']:,} |",
        f"| Code Files | {health['code_files']:,} |",
        f"| Test Files | {health['test_files']:,} |",
        f"| Documentation Files | {health['doc_files']:,} |",
        f"| Has README | {'✅' if health['has_readme'] else '❌'} |",
        f"| Has CONTRIBUTING | {'✅' if health['has_contributing'] else '❌'} |",
        f"| Has CHANGELOG | {'✅' if health['has_changelog'] else '❌'} |",
        f"| Coverage (detected) | {health['coverage'] if health['coverage'] else 'Not found'} |",
        f"| Tech Debt Score | `{health['tech_debt_score']}` |",
        "",
        "### Language Distribution",
        "",
        "| Language | Files |",
        "|----------|-------|",
    ])

    for lang, count in health["languages"].most_common():
        lines.append(f"| {lang} | {count} |")

    lines.extend([
        "",
        "---",
        "",
        "*This report was generated automatically. All findings require human validation.*",
    ])

    return "\n".join(lines)


def generate_recommendations(repo_path: Path, profile_id: str, stack: str,
                             conventions: Dict, health: Dict,
                             symfony_info: Optional[Dict] = None) -> str:
    """Generate recommendations markdown."""
    today = datetime.now().strftime("%Y-%m-%d")

    recs = []

    if not health["has_readme"]:
        recs.append("- **Add README.md** — Repository lacks a README. Critical for onboarding.")

    if not conventions.get("linters") and not conventions.get("formatters"):
        recs.append("- **Add code style tooling** — No linter or formatter detected. Consider adding one for your stack.")

    if not conventions.get("static_analysis"):
        recs.append("- **Add static analysis** — No static analysis tool detected. This is a high-value quality gate.")

    if health["test_files"] == 0:
        recs.append("- **Add tests** — No test files detected. Testing is mandatory for ACSDD sessions.")

    if not conventions.get("ci_cd"):
        recs.append("- **Add CI/CD pipeline** — No CI configuration detected. Quality gates require automated execution.")

    if not conventions.get("security_scans"):
        recs.append("- **Add security scanning** — Consider gitleaks, Snyk, or Trivy for secret and vulnerability detection.")

    if not (repo_path / ".editorconfig").exists():
        recs.append("- **Add .editorconfig** — Standardizes editor behavior across the team.")

    if stack == "php-symfony" and symfony_info:
        if symfony_info.get("structure") == "legacy":
            recs.append(
                "- **Legacy Symfony directory structure detected** (`app/AppKernel.php`, "
                "`web/app.php`) — the capability adapter defaults assume the 4.x+ Flex "
                "layout (`config/packages`, `public/index.php`). Either migrate the "
                "structure or add explicit `capability_configuration.overrides` for "
                "path-sensitive capabilities. See https://symfony.com/doc/current/best_practices.html"
            )
        if symfony_info.get("uses_bundle_wrapper"):
            recs.append(
                "- **`src/*Bundle` wrapper detected** — Symfony's 4.x Best Practices "
                "recommend a flat `src/` (`src/Entity`, `src/Controller`, ...) instead of "
                "wrapping the whole app in a single bundle, a pattern left over from "
                "pre-Symfony-4 conventions. Flag this for `PROFILE-001` review before "
                "generating new code under the old wrapper."
            )
        if not symfony_info.get("version"):
            recs.append(
                "- **Symfony version constraint not found** — `symfony/framework-bundle` "
                "or `symfony/symfony` wasn't pinned in `composer.json`'s `require` block. "
                "Confidence in the generated adapter is lower without it."
            )

    lines = [
        f"# Recommendations: {profile_id}",
        f"**Generated:** {today}  ",
        "",
        "---",
        "",
        "## Priority Recommendations",
        "",
    ]
    lines.extend(recs)
    lines.extend([
        "",
        "## Human Decisions Required",
        "",
        "The following fields in the draft profile require explicit human input:",
        "",
        "| Field | Reason |",
        "|-------|--------|",
        "| `technology_stack.version` | Cannot be reliably auto-detected from all manifest formats |",
        "| `technology_stack.orm` | Requires domain knowledge of the data layer |",
        "| `engineering_standards.min_coverage` | Should reflect realistic team goals, not current state |",
        "| `capability_configuration.overrides` | Repository-specific exceptions to defaults |",
        "| `ai_execution_rules.max_files_per_session` | Calibrate to team review capacity |",
        "",
        "## Next Steps",
        "",
        "1. Review the draft profile (`profiles/{profile_id}-draft.yaml`)",
        "2. Complete all `[REVIEW REQUIRED]` fields",
        "3. Validate quality gates are executable in your CI",
        "4. Bump version to `1.0.0` and commit",
        "5. Begin ACSDD Phase 1: Spec Retroactive",
        "",
        "---",
        "",
        "*These recommendations are AI-generated proposals. The team decides which to adopt.*",
    ])

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ACSDD Profile Discovery Tool — PROFILE-001"
    )
    parser.add_argument("repo_path", help="Path to the Git repository root")
    parser.add_argument("--profile-id", required=True, help="Profile identifier")
    parser.add_argument("--depth", default="deep", choices=["surface", "deep"])
    parser.add_argument("--known-stack", default=None, help="Skip auto-detection")
    parser.add_argument("--target-version", default="0.2.0", help="ACSDD target version")
    parser.add_argument("--output", default=DEFAULT_PROFILES_DIR, help="Output directory")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite existing draft/report/recommendations files")

    args = parser.parse_args(argv)

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.exists():
        print(f"ERROR: Repository path does not exist: {repo_path}")
        sys.exit(1)

    if not (repo_path / ".git").exists():
        print(f"WARNING: No .git directory found at {repo_path}. Proceeding anyway.")

    output_dir = Path(args.output)

    # Discovery output is meant to be hand-edited (filling in [REVIEW
    # REQUIRED] fields) before it's trusted — silently overwriting those
    # edits on a re-run would destroy that work with no way back.
    existing = [
        output_dir / f"{args.profile_id}-draft.yaml",
        output_dir / f"{args.profile_id}-discovery-report.md",
        output_dir / f"{args.profile_id}-recommendations.md",
    ]
    existing = [p for p in existing if p.exists()]
    if existing and not args.force:
        print("ERROR: Output file(s) already exist — re-run with --force to overwrite:")
        for p in existing:
            print(f"   {p}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Analyzing repository: {repo_path}")
    print(f"   Profile ID: {args.profile_id}")
    print(f"   Depth: {args.depth}")
    print()

    # Phase 1: Discovery
    print("[1/4] Detecting technology stack...")
    by_role = StackDetector(repo_path).detect_by_role()
    role_paths = {role: repo_path for role in by_role}

    # Also probe common monorepo subdirectories for whichever role wasn't
    # found at the root — e.g. root has a Symfony backend, and a separate
    # "frontend/" directory holds the React app that a root-only scan
    # would otherwise miss entirely.
    for subdir_name in MONOREPO_SUBDIR_CANDIDATES:
        if "backend" in by_role and "frontend" in by_role:
            break
        subdir = repo_path / subdir_name
        if not subdir.is_dir():
            continue
        sub_by_role = StackDetector(subdir).detect_by_role()
        for role, result in sub_by_role.items():
            if role not in by_role:
                by_role[role] = result
                role_paths[role] = subdir

    stack_confidence = 0.0
    frontend_stack = None
    frontend_confidence = 0.0
    backend_path = repo_path

    if "backend" in by_role:
        stack, stack_info, stack_confidence = by_role["backend"]
        backend_path = role_paths["backend"]
    elif "frontend" in by_role:
        # Frontend-only repo (e.g. a standalone React app) — treat it as
        # the primary stack rather than reporting "unknown".
        stack, stack_info, stack_confidence = by_role["frontend"]
        backend_path = role_paths["frontend"]
    else:
        stack, stack_info = "unknown", {}

    if "frontend" in by_role and by_role["frontend"][0] != stack:
        frontend_stack, _, frontend_confidence = by_role["frontend"]

    if args.known_stack:
        stack = args.known_stack
        stack_info = StackDetector.STACK_SIGNATURES.get(stack, {"language": "unknown"})
        stack_confidence = 1.0

    print(f"       → Detected backend: {stack}"
          + (f" (confidence: {stack_confidence:.0%})" if stack != "unknown" else ""))
    if frontend_stack:
        print(f"       → Detected frontend: {frontend_stack} (confidence: {frontend_confidence:.0%})")

    symfony_info = None
    if stack == "php-symfony":
        symfony_info = SymfonyStructureDetector(backend_path).detect()
        if symfony_info.get("version"):
            print(f"       → Symfony version: {symfony_info['version']} ({symfony_info['structure']} structure)")
        else:
            print(f"       → Symfony structure: {symfony_info['structure']} (version not pinned)")

    database = DatabaseDetector(repo_path).detect()
    if database:
        print(f"       → Detected database: {database}")

    print("[2/4] Detecting conventions and quality gates...")
    conv_detector = ConventionDetector(repo_path)
    conventions = conv_detector.detect()
    print(f"       → Found {sum(len(v) for v in conventions.values())} configuration files")

    # ArchitectureDetector and HealthMetrics both walk the full repo tree —
    # the two expensive passes. --depth surface skips them entirely rather
    # than doing the same full walk regardless of the flag (which is what
    # this used to do): the caller gets a fast stack+conventions-only pass
    # and clearly-marked placeholders for the rest.
    if args.depth == "surface":
        print("[3/4] Skipping architecture detection (--depth surface)...")
        architecture = ("[REVIEW REQUIRED — run --depth deep]", 0.0)
        print("[4/4] Skipping repository health analysis (--depth surface)...")
        health = {
            "total_files": 0, "code_files": 0, "test_files": 0, "doc_files": 0,
            "languages": Counter(), "has_readme": False, "has_contributing": False,
            "has_changelog": False, "coverage": None,
            "tech_debt_score": "[REVIEW REQUIRED — run --depth deep]",
        }
    else:
        print("[3/4] Detecting architecture patterns...")
        arch_detector = ArchitectureDetector(repo_path)
        architecture = arch_detector.detect()
        print(f"       → Detected: {architecture[0]} (confidence: {architecture[1]:.0%})")

        print("[4/4] Analyzing repository health...")
        health = HealthMetrics(repo_path).analyze()
        print(f"       → {health['code_files']:,} code files, {health['test_files']:,} test files")
        print(f"       → Tech debt: {health['tech_debt_score']}")
    print()

    # Phase 2: Generation
    print("⚙️  Generating artifacts...")

    generator = ProfileGenerator(
        profile_id=args.profile_id,
        stack=stack,
        stack_info=stack_info,
        conventions=conventions,
        architecture=architecture,
        health=health,
        acsdd_version=args.target_version,
        frontend_stack=frontend_stack,
        frontend_confidence=frontend_confidence,
        symfony_info=symfony_info,
        database=database,
    )

    profile_yaml = generator.generate()
    profile_path = output_dir / f"{args.profile_id}-draft.yaml"
    profile_path.write_text(profile_yaml, encoding="utf-8")
    print(f"       → {profile_path}")

    report_md = generate_discovery_report(
        repo_path, args.profile_id, stack, stack_info,
        conventions, architecture, health,
        stack_confidence=stack_confidence,
        frontend_stack=frontend_stack,
        frontend_confidence=frontend_confidence,
    )
    report_path = output_dir / f"{args.profile_id}-discovery-report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"       → {report_path}")

    recs_md = generate_recommendations(
        repo_path, args.profile_id, stack, conventions, health,
        symfony_info=symfony_info,
    )
    recs_path = output_dir / f"{args.profile_id}-recommendations.md"
    recs_path.write_text(recs_md, encoding="utf-8")
    print(f"       → {recs_path}")
    print()

    # Summary
    confidence = generator._calculate_confidence()
    print("=" * 60)
    print("✅ PROFILE-001 Complete")
    print(f"   Confidence Score: {confidence}/100")
    print(f"   Stack: {stack}")
    print(f"   Architecture: {architecture[0]}")
    print(f"   Tech Debt: {health['tech_debt_score']}")
    print()
    print("⚠️  HUMAN REVIEW REQUIRED")
    print("   Review the draft profile and complete all [REVIEW REQUIRED] fields.")
    print("   See the recommendations document for prioritized next steps.")
    print("=" * 60)


if __name__ == "__main__":
    main()
