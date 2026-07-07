#!/usr/bin/env python3
"""Consumer contract-pin verification (#1319, dev-contracts plugin).

This is the *only* bespoke logic in dev-contracts. Given a consumer repo's
`.contracts-lock.yaml` (which IF contracts it targets, at which version / range) and the
*actual* published versions of those contracts (supplied by the control repo, e.g. via a
committed `contracts-versions.json`), decide whether every pinned entry is satisfied.

Everything else in the contract fleet is delegated to OSS CLIs invoked from CI:
  - breaking-change detection  -> oasdiff  (`oasdiff breaking base rev --fail-on ERR`)
  - contract lint              -> Spectral (`spectral lint --ruleset .spectral.yaml`)
There is no OSS standard for "consumer declares a target contract version, verify it
matches the published contract", so that gate lives here — pure stdlib + PyYAML, no
dependency on any other plugin (cherry-pick independence).

Version model (MVP): plain 3-part semver `MAJOR.MINOR.PATCH` of non-negative integers.
Prerelease / build metadata (`-rc1`, `+build`) is intentionally out of scope and reported
as a diagnostic rather than silently accepted.

Exit codes (CLI):
  0  all pins satisfied
  1  one or more pin violations (a consumer targets a version the control repo no longer
     publishes / a range is not satisfied)
  2  diagnostic / usage error (missing or malformed lock, unknown contract name,
     unsupported version string, missing PyYAML)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import List, Tuple

# Longest comparators first so ">=" is matched before ">".
_COMPARATORS = (">=", "<=", "==", ">", "<")


def parse_version(v: str) -> Tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH`` into a tuple. Reject prerelease/build metadata.

    Raises ValueError with a human-readable (Japanese) message on anything we do not
    support in the MVP so the caller can surface it as a diagnostic.
    """
    if not isinstance(v, str):
        raise ValueError(f"バージョンは文字列である必要があります: {v!r}")
    s = v.strip()
    if "-" in s or "+" in s:
        raise ValueError(
            f"prerelease/ビルドメタ付きバージョンは MVP 対象外です: '{s}'"
        )
    parts = s.split(".")
    if len(parts) != 3:
        raise ValueError(f"MAJOR.MINOR.PATCH 形式ではありません: '{s}'")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError(f"バージョンの各要素は整数である必要があります: '{s}'")
    if any(n < 0 for n in nums):
        raise ValueError(f"バージョンに負の要素は使えません: '{s}'")
    return nums  # type: ignore[return-value]


def parse_range(spec: str) -> List[Tuple[str, Tuple[int, int, int]]]:
    """Parse a whitespace-separated comparator range like ``>=1.4.0 <2.0.0``.

    Returns a list of ``(operator, version_tuple)``. An empty/whitespace spec yields an
    empty list. Raises ValueError on an unknown operator or unparseable version.
    """
    if spec is None:
        return []
    tokens = spec.split()
    parsed: List[Tuple[str, Tuple[int, int, int]]] = []
    for tok in tokens:
        op = next((c for c in _COMPARATORS if tok.startswith(c)), None)
        if op is None:
            raise ValueError(
                f"range の比較演算子が不正です（{', '.join(_COMPARATORS)} のいずれか）: '{tok}'"
            )
        parsed.append((op, parse_version(tok[len(op):])))
    return parsed


def _satisfies_comparator(actual: Tuple[int, int, int], op: str, want: Tuple[int, int, int]) -> bool:
    if op == "==":
        return actual == want
    if op == ">=":
        return actual >= want
    if op == "<=":
        return actual <= want
    if op == ">":
        return actual > want
    if op == "<":
        return actual < want
    raise ValueError(f"未知の比較演算子: '{op}'")  # defensive; parse_range guards this


def evaluate_entry(entry: dict, actual: str) -> Tuple[bool, str]:
    """Evaluate one lock entry against the actual published version string.

    Range wins over exact ``version`` when present. Returns ``(ok, reason)`` where reason
    is empty on success and a human-readable explanation on failure. Raises ValueError for
    unsupported/invalid version data (surfaced by the caller as a diagnostic).
    """
    actual_t = parse_version(actual)
    # range が present（明示 null 以外）なら range mode。空文字/空白/比較子ゼロ件は「malformed range」
    # として診断エラーにする（無条件 pass で gate を静かに落とさない — #1319 Codex Blocker）。
    if "range" in entry and entry["range"] is not None:
        range_spec = entry["range"]
        if not isinstance(range_spec, str):
            raise ValueError(f"range は文字列である必要があります: {range_spec!r}")
        comparators = parse_range(range_spec)
        if not comparators:
            raise ValueError(f"range に比較子がありません（空/空白は不正）: '{range_spec}'")
        for op, want in comparators:
            if not _satisfies_comparator(actual_t, op, want):
                return False, f"実バージョン {actual} が range '{range_spec}' を満たしません"
        return True, ""
    pinned = entry.get("version")
    if pinned is None:
        raise ValueError("lock エントリに version も range もありません")
    if not isinstance(pinned, str):
        raise ValueError(f"version は文字列である必要があります: {pinned!r}")
    if parse_version(pinned) != actual_t:
        return False, f"実バージョン {actual} が pin '{pinned}' と一致しません"
    return True, ""


@dataclass
class Violation:
    name: str
    declared: str
    actual: str
    reason: str


@dataclass
class Diagnostic:
    name: str
    error: str


@dataclass
class Result:
    violations: List[Violation] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.diagnostics

    def exit_code(self) -> int:
        if self.diagnostics:
            return 2
        if self.violations:
            return 1
        return 0


def _declared_str(entry: dict) -> str:
    return str(entry.get("range") or entry.get("version") or "?")


def verify(lock: dict, actual_versions: dict) -> Result:
    """Compare every locked contract entry against the actual published versions.

    - unknown contract name (in lock, absent from ``actual_versions``) -> diagnostic
    - unsupported/invalid version string (lock or actual)              -> diagnostic
    - range/exact not satisfied                                        -> violation
    A missing/empty ``contracts`` list verifies vacuously (Result.ok == True).
    """
    result = Result()
    contracts = lock.get("contracts") or []
    for entry in contracts:
        if not isinstance(entry, dict) or "name" not in entry:
            result.diagnostics.append(
                Diagnostic(name="<unknown>", error=f"lock エントリの形式が不正です: {entry!r}")
            )
            continue
        name = entry["name"]
        if name not in actual_versions:
            result.diagnostics.append(
                Diagnostic(name=name, error=f"契約 '{name}' が control 側の公開バージョンに存在しません")
            )
            continue
        try:
            ok, reason = evaluate_entry(entry, str(actual_versions[name]))
        except (ValueError, TypeError) as exc:
            # 不正な version/range 型・未サポート版などは違反ではなく診断エラー（run を信頼できない）
            result.diagnostics.append(Diagnostic(name=name, error=str(exc)))
            continue
        if not ok:
            result.violations.append(
                Violation(
                    name=name,
                    declared=_declared_str(entry),
                    actual=str(actual_versions[name]),
                    reason=reason,
                )
            )
    return result


def load_lock(path: str) -> dict:
    """Load and shape-validate a ``.contracts-lock.yaml``. Raises ValueError on any
    problem (surfaced as a diagnostic / exit 2). Requires PyYAML — its absence is a hard
    error, never a silent pass."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ValueError(
            "PyYAML が必要です（pip install pyyaml）。契約 pin 検証を skip しません"
        ) from exc
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ValueError(f"lock ファイルが見つかりません: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"lock ファイルの YAML が不正です: {path}: {exc}") from exc
    if data is None:
        raise ValueError(f"lock ファイルが空です: {path}")
    if not isinstance(data, dict) or "contracts" not in data:
        raise ValueError("lock は最上位に 'contracts' リストを持つ必要があります")
    if not isinstance(data["contracts"], list):
        raise ValueError("'contracts' はリストである必要があります")
    return data


def load_versions(path: str) -> dict:
    """Load the control repo's published version map (JSON: ``{name: version}``)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ValueError(f"versions ファイルが見つかりません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"versions ファイルの JSON が不正です: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("versions は {name: version} の JSON オブジェクトである必要があります")
    return data


def render_report(result: Result) -> str:
    if result.ok:
        return "✓ 契約 pin 突合: 全エントリが公開バージョンを満たしています"
    lines: List[str] = []
    if result.violations:
        lines.append("✗ 契約 pin 違反:")
        for v in result.violations:
            lines.append(f"  - {v.name}: 宣言 '{v.declared}' / 実 '{v.actual}' — {v.reason}")
    if result.diagnostics:
        lines.append("⚠ 診断エラー（違反とは区別）:")
        for d in result.diagnostics:
            lines.append(f"  - {d.name}: {d.error}")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a consumer repo's .contracts-lock.yaml against published contract versions."
    )
    parser.add_argument("--lock", default=".contracts-lock.yaml", help="path to .contracts-lock.yaml")
    parser.add_argument(
        "--versions",
        required=True,
        help="path to a JSON file mapping contract name -> published version",
    )
    args = parser.parse_args(argv)
    try:
        lock = load_lock(args.lock)
        actual = load_versions(args.versions)
    except ValueError as exc:
        print(f"⚠ 診断エラー: {exc}", file=sys.stderr)
        return 2
    result = verify(lock, actual)
    print(render_report(result))
    return result.exit_code()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
