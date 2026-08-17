"""Compile the sparse Zen evaluation-axis CSV into a stable taxonomy.

The source sheet is intentionally treated as data, not as a conventional CSV
with a header.  Axis rows populate columns 1 and 2; subaxis rows populate
columns 2--4; blank rows are separators.  The first row (``Axis 1, ...``) is
therefore the first taxonomy record even though its fourth cell says
``Variants``.

Only the Python standard library is used so this compiler can run in ingestion
jobs before optional application dependencies are installed.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from typing import Any, Iterator, Mapping, Sequence, TypeAlias


SCHEMA_VERSION = "1.0"

_AXIS_RE = re.compile(r"^Axis\s+(?P<number>\d+)\s*$", re.IGNORECASE)
_STRICT_VARIANT_RE = re.compile(
    r"^\s*(?P<number>\d+)\.(?P<space>\s+)(?P<body>\S(?:.*\S)?)\s*$"
)
_LENIENT_VARIANT_RE = re.compile(
    r"^\s*(?P<number>\d+)(?P<delimiter>\.\.|\.|\)|)(?P<space>\s+)"
    r"(?P<body>\S(?:.*\S)?)\s*$"
)


class TaxonomyCompileError(ValueError):
    """Raised when the source cannot be compiled without guessing structure."""


@dataclass(frozen=True, slots=True)
class CompileWarning:
    """A recoverable source defect retained in the compiled manifest."""

    code: str
    message: str
    source_row: int
    source_column: int
    raw_value: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source_row": self.source_row,
            "source_column": self.source_column,
            "raw_value": self.raw_value,
        }


def _row_checksum(fields: Sequence[str]) -> str:
    """Checksum decoded fields unambiguously while preserving their contents."""

    payload = json.dumps(
        list(fields), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _source_manifest(
    source_row: int,
    raw_fields: Sequence[str],
    row_checksum: str,
    *,
    variant_line: int | None = None,
    raw_text: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "row": source_row,
        "raw_fields": list(raw_fields),
        "row_sha256": row_checksum,
    }
    if variant_line is not None:
        result["variant_line"] = variant_line
    if raw_text is not None:
        result["raw_text"] = raw_text
    return result


@dataclass(frozen=True, slots=True)
class Variant:
    id: str
    axis_id: str
    subaxis_id: str
    ordinal: int
    source_number: int
    name: str
    description: str
    raw_text: str
    number_delimiter: str
    source_row: int
    source_variant_line: int
    raw_fields: tuple[str, str, str, str]
    row_checksum: str
    status: str = "active"
    enabled: bool = True

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "axis_id": self.axis_id,
            "subaxis_id": self.subaxis_id,
            "ordinal": self.ordinal,
            "source_number": self.source_number,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "enabled": self.enabled,
            "source": _source_manifest(
                self.source_row,
                self.raw_fields,
                self.row_checksum,
                variant_line=self.source_variant_line,
                raw_text=self.raw_text,
            ),
            "number_delimiter": self.number_delimiter,
        }


@dataclass(frozen=True, slots=True)
class Subaxis:
    id: str
    axis_id: str
    ordinal: int
    name: str
    description: str
    variants: tuple[Variant, ...]
    source_row: int
    raw_fields: tuple[str, str, str, str]
    row_checksum: str
    status: str = "active"
    enabled: bool = True

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "axis_id": self.axis_id,
            "ordinal": self.ordinal,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "enabled": self.enabled,
            "source": _source_manifest(
                self.source_row, self.raw_fields, self.row_checksum
            ),
            "variants": [variant.to_manifest() for variant in self.variants],
        }


@dataclass(frozen=True, slots=True)
class Axis:
    id: str
    number: int
    name: str
    description: str
    subaxes: tuple[Subaxis, ...]
    source_row: int
    raw_fields: tuple[str, str, str, str]
    row_checksum: str
    status: str = "active"
    enabled: bool = True

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "enabled": self.enabled,
            "source": _source_manifest(
                self.source_row, self.raw_fields, self.row_checksum
            ),
            "subaxes": [subaxis.to_manifest() for subaxis in self.subaxes],
        }


TaxonomyNode: TypeAlias = Axis | Subaxis | Variant


@dataclass(frozen=True, slots=True)
class Taxonomy:
    axes: tuple[Axis, ...]
    warnings: tuple[CompileWarning, ...]
    source_path: str
    source_checksum: str
    schema_version: str = SCHEMA_VERSION

    def iter_subaxes(self) -> Iterator[Subaxis]:
        for axis in self.axes:
            yield from axis.subaxes

    def iter_variants(self) -> Iterator[Variant]:
        for subaxis in self.iter_subaxes():
            yield from subaxis.variants

    @property
    def axis_count(self) -> int:
        return len(self.axes)

    @property
    def subaxis_count(self) -> int:
        return sum(1 for _ in self.iter_subaxes())

    @property
    def variant_count(self) -> int:
        return sum(1 for _ in self.iter_variants())

    def lookup(self, identifier: str) -> TaxonomyNode:
        """Return an axis, subaxis, or variant by its stable ID.

        IDs are case-insensitive at the API boundary.  Names are deliberately
        not accepted: names are editable source content and are not identifiers.
        """

        normalized = str(identifier).strip().upper()
        for axis in self.axes:
            if axis.id == normalized:
                return axis
            for subaxis in axis.subaxes:
                if subaxis.id == normalized:
                    return subaxis
                for variant in subaxis.variants:
                    if variant.id == normalized:
                        return variant
        raise KeyError(f"unknown taxonomy ID: {identifier!r}")

    def validate_path(
        self,
        axis_id: str,
        subaxis_id: str | None = None,
        variant_id: str | None = None,
        *,
        require_enabled: bool = False,
    ) -> bool:
        """Return whether IDs exist, form a parent-child path, and are usable.

        ``variant_id`` without ``subaxis_id`` is invalid.  When
        ``require_enabled`` is true, draft/disabled nodes are rejected as well.
        Unknown IDs return ``False`` rather than raising, making this suitable
        for validating incoming annotation records.  Use :meth:`lookup` when a
        missing identifier should be exceptional.
        """

        if variant_id is not None and subaxis_id is None:
            return False
        try:
            axis = self.lookup(axis_id)
        except KeyError:
            return False
        if not isinstance(axis, Axis):
            return False
        if require_enabled and not axis.enabled:
            return False
        if subaxis_id is None:
            return variant_id is None

        try:
            subaxis = self.lookup(subaxis_id)
        except KeyError:
            return False
        if not isinstance(subaxis, Subaxis) or subaxis.axis_id != axis.id:
            return False
        if require_enabled and not subaxis.enabled:
            return False
        if variant_id is None:
            return True

        try:
            variant = self.lookup(variant_id)
        except KeyError:
            return False
        if not isinstance(variant, Variant) or variant.subaxis_id != subaxis.id:
            return False
        return not require_enabled or variant.enabled

    def to_manifest(self) -> dict[str, Any]:
        """Return a deterministic, JSON-serializable taxonomy manifest."""

        return {
            "schema_version": self.schema_version,
            "source": {
                "path": self.source_path,
                "sha256": self.source_checksum,
            },
            "counts": {
                "axes": self.axis_count,
                "subaxes": self.subaxis_count,
                "variants": self.variant_count,
                "warnings": len(self.warnings),
            },
            "warnings": [warning.to_manifest() for warning in self.warnings],
            "axes": [axis.to_manifest() for axis in self.axes],
        }

    def to_manifest_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_manifest(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )


@dataclass(slots=True)
class _AxisBuilder:
    id: str
    number: int
    name: str
    description: str
    source_row: int
    raw_fields: tuple[str, str, str, str]
    row_checksum: str
    status: str
    enabled: bool
    subaxes: list[Subaxis]

    def freeze(self) -> Axis:
        return Axis(
            id=self.id,
            number=self.number,
            name=self.name,
            description=self.description,
            subaxes=tuple(self.subaxes),
            source_row=self.source_row,
            raw_fields=self.raw_fields,
            row_checksum=self.row_checksum,
            status=self.status,
            enabled=self.enabled,
        )


def _split_variant_body(body: str) -> tuple[str, str]:
    """Split the display label from explanatory prose without losing raw text."""

    opening = body.find("(")
    if opening > 0:
        name = body[:opening].strip()
        remainder = body[opening:].strip()
        # Remove parentheses only when one outer pair encloses the entire
        # description.  Security labels such as ``Name(LLM01) prose`` retain
        # the classification marker as part of the description.
        if remainder.startswith("(") and remainder.endswith(")"):
            depth = 0
            closes_at_end = False
            for position, character in enumerate(remainder):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        closes_at_end = position == len(remainder) - 1
                        break
            if closes_at_end:
                remainder = remainder[1:-1].strip()
        return name or body.strip(), remainder

    spaced = re.split(r"\s{2,}", body, maxsplit=1)
    if len(spaced) == 2:
        return spaced[0].strip(), spaced[1].strip()
    return body.strip(), ""


def _parse_variants(
    raw_value: str,
    *,
    axis_id: str,
    subaxis_id: str,
    source_row: int,
    raw_fields: tuple[str, str, str, str],
    row_checksum: str,
    status: str,
    enabled: bool,
    warnings: list[CompileWarning],
    strict: bool,
) -> tuple[Variant, ...]:
    parsed: list[dict[str, Any]] = []

    for line_number, raw_line in enumerate(raw_value.splitlines(), start=1):
        if not raw_line.strip():
            continue

        strict_match = _STRICT_VARIANT_RE.match(raw_line)
        match = strict_match or _LENIENT_VARIANT_RE.match(raw_line)
        if match is None:
            # A non-numbered physical line following an item is treated as a
            # continuation.  This supports spreadsheet-wrapped descriptions
            # without inventing another variant.
            if parsed and not re.match(r"^\s*\d", raw_line):
                parsed[-1]["body"] += "\n" + raw_line.strip()
                parsed[-1]["raw_text"] += "\n" + raw_line
                continue
            raise TaxonomyCompileError(
                f"row {source_row}, variant line {line_number}: cannot parse "
                f"numbered variant {raw_line!r}"
            )

        number = int(match.group("number"))
        delimiter = "." if strict_match else match.group("delimiter")
        if strict_match is None:
            message = (
                f"variant {number} uses non-canonical delimiter "
                f"{delimiter!r}; expected '.'"
            )
            if strict:
                raise TaxonomyCompileError(f"row {source_row}: {message}")
            warnings.append(
                CompileWarning(
                    code="noncanonical_variant_numbering",
                    message=message,
                    source_row=source_row,
                    source_column=4,
                    raw_value=raw_line,
                )
            )

        expected = len(parsed) + 1
        if number != expected:
            message = f"variant number {number} is out of sequence; expected {expected}"
            if strict:
                raise TaxonomyCompileError(f"row {source_row}: {message}")
            warnings.append(
                CompileWarning(
                    code="nonsequential_variant_number",
                    message=message,
                    source_row=source_row,
                    source_column=4,
                    raw_value=raw_line,
                )
            )

        parsed.append(
            {
                "number": number,
                "delimiter": delimiter,
                "body": match.group("body").strip(),
                "raw_text": raw_line,
                "line": line_number,
            }
        )

    variants: list[Variant] = []
    for ordinal, item in enumerate(parsed, start=1):
        name, description = _split_variant_body(item["body"])
        variants.append(
            Variant(
                id=f"{subaxis_id}-V{ordinal:03d}",
                axis_id=axis_id,
                subaxis_id=subaxis_id,
                ordinal=ordinal,
                source_number=item["number"],
                name=name,
                description=description,
                raw_text=item["raw_text"],
                number_delimiter=item["delimiter"],
                source_row=source_row,
                source_variant_line=item["line"],
                raw_fields=raw_fields,
                row_checksum=row_checksum,
                status=status,
                enabled=enabled,
            )
        )
    return tuple(variants)


def compile_taxonomy(
    source: str | Path,
    *,
    strict: bool = False,
) -> Taxonomy:
    """Compile a four-column sparse CSV into an immutable :class:`Taxonomy`.

    By default, malformed but unambiguous numbered-list delimiters are accepted
    and recorded as warnings.  ``strict=True`` rejects those defects.
    Structural ambiguity, duplicate axes, malformed rows, and subaxes before an
    axis are always errors.
    """

    path = Path(source)
    raw_bytes = path.read_bytes()
    source_checksum = sha256(raw_bytes).hexdigest()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TaxonomyCompileError(f"{path}: source is not valid UTF-8") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    builders: list[_AxisBuilder] = []
    warnings: list[CompileWarning] = []
    current: _AxisBuilder | None = None
    seen_axis_numbers: set[int] = set()

    for source_row, fields_list in enumerate(reader, start=1):
        if len(fields_list) != 4:
            raise TaxonomyCompileError(
                f"row {source_row}: expected exactly 4 columns, got {len(fields_list)}"
            )
        raw_fields = tuple(fields_list)
        # The length check above makes this cast safe and keeps public source
        # metadata precisely typed as a four-cell row.
        fields = raw_fields  # type: ignore[assignment]
        assert len(fields) == 4
        if not any(value.strip() for value in fields):
            continue

        axis_cell, name_cell, description_cell, variants_cell = fields
        if axis_cell.strip():
            match = _AXIS_RE.fullmatch(axis_cell.strip())
            if match is None:
                raise TaxonomyCompileError(
                    f"row {source_row}: invalid axis marker {axis_cell!r}"
                )
            number = int(match.group("number"))
            if number in seen_axis_numbers:
                raise TaxonomyCompileError(
                    f"row {source_row}: duplicate Axis {number}"
                )
            if not name_cell.strip():
                raise TaxonomyCompileError(
                    f"row {source_row}: Axis {number} has no name"
                )
            expected = len(builders) + 1
            if number != expected:
                message = f"axis number {number} is out of sequence; expected {expected}"
                if strict:
                    raise TaxonomyCompileError(f"row {source_row}: {message}")
                warnings.append(
                    CompileWarning(
                        code="nonsequential_axis_number",
                        message=message,
                        source_row=source_row,
                        source_column=1,
                        raw_value=axis_cell,
                    )
                )

            # Creativity is explicitly an unpopulated TBD section in the source,
            # not an active evaluation family.
            is_draft = number == 10 or name_cell.strip().casefold() == "creativity"
            current = _AxisBuilder(
                id=f"AX{number:03d}",
                number=number,
                name=name_cell.strip(),
                description=description_cell.strip(),
                source_row=source_row,
                raw_fields=fields,  # type: ignore[arg-type]
                row_checksum=_row_checksum(fields),
                status="draft" if is_draft else "active",
                enabled=not is_draft,
                subaxes=[],
            )
            builders.append(current)
            seen_axis_numbers.add(number)
            continue

        if not name_cell.strip():
            raise TaxonomyCompileError(
                f"row {source_row}: nonblank row is neither an axis nor a subaxis"
            )
        if current is None:
            raise TaxonomyCompileError(
                f"row {source_row}: subaxis appears before the first axis"
            )

        ordinal = len(current.subaxes) + 1
        subaxis_id = f"{current.id}-SA{ordinal:03d}"
        is_draft = not current.enabled or name_cell.strip().casefold() == "tbd"
        status = "draft" if is_draft else "active"
        enabled = not is_draft
        row_checksum = _row_checksum(fields)
        variants = _parse_variants(
            variants_cell,
            axis_id=current.id,
            subaxis_id=subaxis_id,
            source_row=source_row,
            raw_fields=fields,  # type: ignore[arg-type]
            row_checksum=row_checksum,
            status=status,
            enabled=enabled,
            warnings=warnings,
            strict=strict,
        )
        if enabled and not variants:
            message = f"active subaxis {name_cell.strip()!r} has no variants"
            if strict:
                raise TaxonomyCompileError(f"row {source_row}: {message}")
            warnings.append(
                CompileWarning(
                    code="missing_variants",
                    message=message,
                    source_row=source_row,
                    source_column=4,
                    raw_value=variants_cell,
                )
            )
        current.subaxes.append(
            Subaxis(
                id=subaxis_id,
                axis_id=current.id,
                ordinal=ordinal,
                name=name_cell.strip(),
                description=description_cell.strip(),
                variants=variants,
                source_row=source_row,
                raw_fields=fields,  # type: ignore[arg-type]
                row_checksum=row_checksum,
                status=status,
                enabled=enabled,
            )
        )

    if not builders:
        raise TaxonomyCompileError(f"{path}: no axes found")

    axes = tuple(builder.freeze() for builder in builders)
    taxonomy = Taxonomy(
        axes=axes,
        warnings=tuple(warnings),
        source_path=str(path),
        source_checksum=source_checksum,
    )

    # Defend against an accidental ID collision if a future source grows beyond
    # the assumptions encoded above.
    identifiers = [axis.id for axis in taxonomy.axes]
    identifiers.extend(subaxis.id for subaxis in taxonomy.iter_subaxes())
    identifiers.extend(variant.id for variant in taxonomy.iter_variants())
    if len(identifiers) != len(set(identifiers)):
        raise TaxonomyCompileError("compiled taxonomy contains duplicate stable IDs")
    return taxonomy


def load_taxonomy(source: str | Path, *, strict: bool = False) -> Taxonomy:
    """Alias emphasizing the normal load-and-compile application operation."""

    return compile_taxonomy(source, strict=strict)


def lookup(taxonomy: Taxonomy, identifier: str) -> TaxonomyNode:
    """Functional wrapper for :meth:`Taxonomy.lookup`."""

    return taxonomy.lookup(identifier)


def validate_path(
    taxonomy: Taxonomy,
    axis_id: str,
    subaxis_id: str | None = None,
    variant_id: str | None = None,
    *,
    require_enabled: bool = False,
) -> bool:
    """Functional wrapper for :meth:`Taxonomy.validate_path`."""

    return taxonomy.validate_path(
        axis_id,
        subaxis_id,
        variant_id,
        require_enabled=require_enabled,
    )


__all__ = [
    "Axis",
    "CompileWarning",
    "SCHEMA_VERSION",
    "Subaxis",
    "Taxonomy",
    "TaxonomyCompileError",
    "TaxonomyNode",
    "Variant",
    "compile_taxonomy",
    "load_taxonomy",
    "lookup",
    "validate_path",
]
