"""Virtual dist-info support for Fang-bundled Python applications."""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Iterator, Mapping

DISTRIBUTIONS_MANIFEST = "meta/distributions.json"
_NORMALIZE_RE = re.compile(r"[-_.]+")
_REQUIREMENT_NAME_RE = re.compile(r"\s*([A-Za-z0-9_.-]+)")


def normalize_name(name: str) -> str:
    """Normalize a Python distribution name using PEP 503 spelling rules."""

    return _NORMALIZE_RE.sub("-", name).lower()


def install(
    *, manifest: Mapping[str, Any] | bytes | str | None = None, archive: Any = None
) -> "MetadataStore":
    """Install Fang metadata compatibility hooks.

    Exactly one of ``manifest`` or ``archive`` may be provided. Archives are
    expected to expose one of ``read_text(path)``, ``read(path)``, or
    ``get(path)`` for ``meta/distributions.json`` and optional dist-info files.
    """

    store = MetadataStore.from_source(manifest=manifest, archive=archive)
    finder = FangDistributionFinder(store)

    sys.meta_path[:] = [
        item for item in sys.meta_path if not isinstance(item, FangDistributionFinder)
    ]
    insert_at = _metadata_finder_insert_index(sys.meta_path)
    sys.meta_path.insert(insert_at, finder)
    _install_pkg_resources(store)
    return store


def uninstall() -> None:
    """Remove Fang compatibility hooks from the current interpreter."""

    sys.meta_path[:] = [
        item for item in sys.meta_path if not isinstance(item, FangDistributionFinder)
    ]
    module = sys.modules.get("pkg_resources")
    if getattr(module, "__fang_compat__", False):
        del sys.modules["pkg_resources"]


@dataclass(frozen=True)
class DistributionRecord:
    name: str
    version: str
    dist_info: str
    summary: str | None = None
    top_level: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    metadata_fields: Mapping[str, str | list[str] | tuple[str, ...]] = field(
        default_factory=dict
    )
    metadata_files: Mapping[str, str] = field(default_factory=dict)
    entry_points: tuple["EntryPointRecord", ...] = ()

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)


@dataclass(frozen=True)
class EntryPointRecord:
    group: str
    name: str
    value: str


class MetadataStore:
    def __init__(self, records: Iterable[DistributionRecord], archive: Any = None):
        self._records = tuple(records)
        self._archive = archive
        self._by_name = {record.normalized_name: record for record in self._records}

    @classmethod
    def from_source(
        cls,
        *,
        manifest: Mapping[str, Any] | bytes | str | None = None,
        archive: Any = None,
    ) -> "MetadataStore":
        if manifest is not None and archive is not None:
            raise ValueError("pass either manifest or archive, not both")
        if archive is not None:
            manifest = _load_manifest_from_archive(archive)
        if manifest is None:
            manifest = {"distributions": []}
        if isinstance(manifest, bytes):
            manifest = manifest.decode("utf-8")
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        return cls(_parse_records(manifest), archive=archive)

    def all(self) -> tuple[DistributionRecord, ...]:
        return self._records

    def get(self, name: str) -> DistributionRecord | None:
        return self._by_name.get(normalize_name(name))

    def distributions(self, name: str | None = None) -> Iterator["FangDistribution"]:
        if name is not None:
            record = self.get(name)
            if record is not None:
                yield FangDistribution(record, self)
            return
        for record in self._records:
            yield FangDistribution(record, self)

    def read_text(self, record: DistributionRecord, filename: str) -> str | None:
        filename = filename.lstrip("/")
        if filename in record.metadata_files:
            return record.metadata_files[filename]

        archive_text = self._read_archive_text(f"{record.dist_info}/{filename}")
        if archive_text is not None:
            return archive_text

        if filename in ("METADATA", "PKG-INFO", ""):
            return _synthesize_metadata(record)
        if filename == "entry_points.txt":
            return _synthesize_entry_points(record)
        if filename == "top_level.txt":
            return "\n".join(record.top_level) + ("\n" if record.top_level else "")
        if filename == "RECORD":
            return _synthesize_record(record)
        return None

    def _read_archive_text(self, path: str) -> str | None:
        if self._archive is None:
            return None
        try:
            data = _archive_read(self._archive, path)
        except KeyError:
            return None
        if data is None:
            return None
        if isinstance(data, str):
            return data
        return bytes(data).decode("utf-8")


class FangDistribution(importlib_metadata.Distribution):
    def __init__(self, record: DistributionRecord, store: MetadataStore):
        self._record = record
        self._store = store

    @property
    def _normalized_name(self) -> str:
        return self._record.normalized_name

    def read_text(self, filename: str) -> str | None:
        return self._store.read_text(self._record, filename)

    def locate_file(self, path: str) -> str:
        return f"fang://{PurePosixPath(str(path))}"


class FangDistributionFinder:
    def __init__(self, store: MetadataStore):
        self.store = store

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        return None

    def find_distributions(
        self,
        context: importlib_metadata.DistributionFinder.Context = (
            importlib_metadata.DistributionFinder.Context()
        ),
    ) -> Iterable[FangDistribution]:
        return self.store.distributions(getattr(context, "name", None))


def _metadata_finder_insert_index(meta_path: list[Any]) -> int:
    for index, finder in enumerate(meta_path):
        if getattr(finder, "find_distributions", None) is not None:
            return index
    return len(meta_path)


def _parse_records(manifest: Mapping[str, Any]) -> tuple[DistributionRecord, ...]:
    raw_records = manifest.get("distributions", [])
    if not isinstance(raw_records, list):
        raise ValueError("distributions manifest must contain a list")
    return tuple(_parse_record(item) for item in raw_records)


def _parse_record(raw: Mapping[str, Any]) -> DistributionRecord:
    name = _required_str(raw, "name")
    version = _required_str(raw, "version")
    dist_info = str(
        raw.get("dist_info")
        or raw.get("dist_info_path")
        or f"site-packages/{normalize_name(name).replace('-', '_')}-{version}.dist-info"
    ).rstrip("/")

    metadata_fields = raw.get("metadata") or {}
    if not isinstance(metadata_fields, Mapping):
        raise ValueError(f"metadata for {name!r} must be an object")

    metadata_files = raw.get("metadata_files") or raw.get("files_text") or {}
    if not isinstance(metadata_files, Mapping):
        raise ValueError(f"metadata_files for {name!r} must be an object")

    return DistributionRecord(
        name=name,
        version=version,
        dist_info=dist_info,
        summary=_optional_str(raw.get("summary")),
        top_level=_string_tuple(raw.get("top_level") or raw.get("packages") or []),
        files=_string_tuple(raw.get("files") or []),
        requires=_string_tuple(raw.get("requires") or raw.get("requires_dist") or []),
        metadata_fields=metadata_fields,
        metadata_files={str(key): str(value) for key, value in metadata_files.items()},
        entry_points=_parse_entry_points(raw.get("entry_points") or {}),
    )


def _parse_entry_points(raw: Any) -> tuple[EntryPointRecord, ...]:
    records: list[EntryPointRecord] = []
    if isinstance(raw, Mapping):
        for group, entries in raw.items():
            if isinstance(entries, Mapping):
                for name, value in entries.items():
                    records.append(EntryPointRecord(str(group), str(name), str(value)))
            elif isinstance(entries, list):
                for entry in entries:
                    records.append(_entry_point_from_mapping(str(group), entry))
            else:
                raise ValueError(f"entry point group {group!r} must be an object or list")
        return tuple(records)
    if isinstance(raw, list):
        return tuple(_entry_point_from_mapping(None, entry) for entry in raw)
    raise ValueError("entry_points must be an object or list")


def _entry_point_from_mapping(group: str | None, raw: Mapping[str, Any]) -> EntryPointRecord:
    if not isinstance(raw, Mapping):
        raise ValueError("entry point entries must be objects")
    return EntryPointRecord(
        str(group or raw["group"]),
        _required_str(raw, "name"),
        _required_str(raw, "value"),
    )


def _load_manifest_from_archive(archive: Any) -> Mapping[str, Any]:
    data = _archive_read(archive, DISTRIBUTIONS_MANIFEST)
    if isinstance(data, str):
        return json.loads(data)
    return json.loads(bytes(data).decode("utf-8"))


def _archive_read(archive: Any, path: str) -> bytes | str | None:
    for method_name in ("read_text", "read", "get"):
        method = getattr(archive, method_name, None)
        if method is None:
            continue
        result = method(path)
        if isinstance(result, tuple) and len(result) == 2:
            ok, value = result
            if not ok:
                raise KeyError(path)
            result = value
        if result is None:
            raise KeyError(path)
        return result
    if isinstance(archive, Mapping):
        try:
            return archive[path]
        except KeyError:
            raise
    raise TypeError("archive must expose read_text(path), read(path), get(path), or mapping access")


def _synthesize_metadata(record: DistributionRecord) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {record.name}",
        f"Version: {record.version}",
    ]
    if record.summary:
        lines.append(f"Summary: {record.summary}")
    for key, value in record.metadata_fields.items():
        if key in {"Name", "Version", "Summary"}:
            continue
        if isinstance(value, (list, tuple)):
            lines.extend(f"{key}: {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(f"Requires-Dist: {requirement}" for requirement in record.requires)
    return "\n".join(lines) + "\n"


def _synthesize_entry_points(record: DistributionRecord) -> str:
    by_group: dict[str, list[EntryPointRecord]] = {}
    for entry_point in record.entry_points:
        by_group.setdefault(entry_point.group, []).append(entry_point)
    lines: list[str] = []
    for group in sorted(by_group):
        lines.append(f"[{group}]")
        for entry_point in sorted(by_group[group], key=lambda item: item.name):
            lines.append(f"{entry_point.name} = {entry_point.value}")
        lines.append("")
    return "\n".join(lines)


def _synthesize_record(record: DistributionRecord) -> str:
    files = list(record.files)
    metadata_files = ["METADATA", "entry_points.txt", "top_level.txt", "RECORD"]
    files.extend(f"{record.dist_info}/{filename}" for filename in metadata_files)
    return "".join(f"{path},,\n" for path in files)


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"distribution record requires non-empty {key!r}")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_tuple(value: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(item) for item in value)


class PkgResourcesDistribution:
    def __init__(self, record: DistributionRecord, store: MetadataStore):
        self._record = record
        self._store = store
        self.project_name = record.name
        self.key = record.normalized_name
        self.version = record.version
        self.location = "fang://site-packages"

    @property
    def parsed_version(self) -> str:
        return self.version

    def has_metadata(self, name: str) -> bool:
        return self.get_metadata(name) is not None

    def get_metadata(self, name: str) -> str | None:
        return self._store.read_text(self._record, name)

    def get_metadata_lines(self, name: str) -> Iterator[str]:
        text = self.get_metadata(name)
        if text is None:
            return iter(())
        return iter(text.splitlines())

    def requires(self, extras: Iterable[str] = ()) -> list[str]:
        return list(self._record.requires)

    def get_entry_map(self, group: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for entry_point in _pkg_entry_points_for_record(self._record, self._store):
            if group is not None and entry_point.group != group:
                continue
            if group is None:
                result.setdefault(entry_point.group, {})[entry_point.name] = entry_point
            else:
                result[entry_point.name] = entry_point
        return result

    def load_entry_point(self, group: str, name: str) -> Any:
        return self.get_entry_map(group)[name].load()

    def __repr__(self) -> str:
        return f"{self.project_name} {self.version}"


class PkgResourcesEntryPoint:
    def __init__(self, record: EntryPointRecord, dist: PkgResourcesDistribution):
        self.name = record.name
        self.group = record.group
        self.value = record.value
        self.dist = dist
        self.module_name, self.attrs = _split_entry_point_value(record.value)
        self.extras: tuple[str, ...] = ()

    def load(self, require: bool = True) -> Any:
        target = importlib.import_module(self.module_name)
        for attr in self.attrs:
            target = getattr(target, attr)
        return target

    def __repr__(self) -> str:
        return f"EntryPoint.parse({self.name!r} = {self.value!r})"


class WorkingSet:
    def __init__(self, store: MetadataStore):
        self._store = store

    @property
    def by_key(self) -> dict[str, PkgResourcesDistribution]:
        return {
            record.normalized_name: PkgResourcesDistribution(record, self._store)
            for record in self._store.all()
        }

    def __iter__(self) -> Iterator[PkgResourcesDistribution]:
        for record in self._store.all():
            yield PkgResourcesDistribution(record, self._store)

    def find(self, requirement: str) -> PkgResourcesDistribution | None:
        record = self._store.get(_requirement_name(requirement))
        if record is None:
            return None
        return PkgResourcesDistribution(record, self._store)

    def require(self, *requirements: Any) -> list[PkgResourcesDistribution]:
        return _pkg_require(self._store, *requirements)

    def iter_entry_points(
        self, group: str, name: str | None = None
    ) -> Iterator[PkgResourcesEntryPoint]:
        return _pkg_iter_entry_points(self._store, group, name)


def _install_pkg_resources(store: MetadataStore) -> types.ModuleType:
    module = types.ModuleType("pkg_resources")
    module.__fang_compat__ = True
    module.__file__ = "fang://python/fang_compat/pkg_resources.py"

    class DistributionNotFound(Exception):
        pass

    class VersionConflict(Exception):
        pass

    class UnknownExtra(Exception):
        pass

    module.DistributionNotFound = DistributionNotFound
    module.VersionConflict = VersionConflict
    module.UnknownExtra = UnknownExtra
    module.Distribution = PkgResourcesDistribution
    module.EntryPoint = PkgResourcesEntryPoint
    module.WorkingSet = WorkingSet
    module.working_set = WorkingSet(store)
    module.get_distribution = lambda requirement: _pkg_get_distribution(
        store, requirement, DistributionNotFound
    )
    module.require = lambda *requirements: _pkg_require(
        store, *requirements, missing_exc=DistributionNotFound
    )
    module.iter_entry_points = lambda group, name=None: _pkg_iter_entry_points(store, group, name)
    module.get_entry_map = lambda dist, group=None: module.get_distribution(
        dist
    ).get_entry_map(group)
    module.load_entry_point = (
        lambda dist, group, name: module.get_distribution(dist).load_entry_point(group, name)
    )
    module.resource_filename = lambda package_or_requirement, resource_name: (
        f"fang://resources/{package_or_requirement}/{resource_name}"
    )
    module.cleanup_resources = lambda force=False: None
    sys.modules["pkg_resources"] = module
    return module


def _pkg_get_distribution(
    store: MetadataStore, requirement: Any, missing_exc: type[Exception]
) -> PkgResourcesDistribution:
    if isinstance(requirement, PkgResourcesDistribution):
        return requirement
    record = store.get(_requirement_name(str(requirement)))
    if record is None:
        raise missing_exc(str(requirement))
    return PkgResourcesDistribution(record, store)


def _pkg_require(
    store: MetadataStore,
    *requirements: Any,
    missing_exc: type[Exception] | None = None,
) -> list[PkgResourcesDistribution]:
    missing_exc = missing_exc or LookupError
    flattened: list[Any] = []
    for requirement in requirements:
        if isinstance(requirement, str):
            flattened.extend(item for item in re.split(r"[\n,]+", requirement) if item.strip())
        elif isinstance(requirement, PkgResourcesDistribution):
            flattened.append(requirement)
        else:
            flattened.extend(requirement)
    return [_pkg_get_distribution(store, requirement, missing_exc) for requirement in flattened]


def _pkg_iter_entry_points(
    store: MetadataStore, group: str, name: str | None = None
) -> Iterator[PkgResourcesEntryPoint]:
    for record in store.all():
        for entry_point in _pkg_entry_points_for_record(record, store):
            if entry_point.group == group and (name is None or entry_point.name == name):
                yield entry_point


def _pkg_entry_points_for_record(
    record: DistributionRecord, store: MetadataStore | None = None
) -> Iterator[PkgResourcesEntryPoint]:
    if store is None:
        store = MetadataStore([record])
    dist = PkgResourcesDistribution(record, store)
    for entry_point in record.entry_points:
        yield PkgResourcesEntryPoint(entry_point, dist)


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME_RE.match(requirement)
    if match is None:
        raise ValueError(f"invalid requirement: {requirement!r}")
    return match.group(1)


def _split_entry_point_value(value: str) -> tuple[str, tuple[str, ...]]:
    module_name, _, attrs = value.partition(":")
    return module_name.strip(), tuple(part.strip() for part in attrs.split(".") if part.strip())
