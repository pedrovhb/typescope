from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from functools import total_ordering, lru_cache
from hashlib import md5
from pathlib import Path
from typing import Iterator, Mapping

import diskcache as diskcache
from diskcache import JSONDisk
from libcst.metadata import ProviderT, QualifiedName
from rich.console import Console
import intervaltree.interval
import libcst as cst
import libcst.codemod
import libcst.metadata as meta
import rich
from intervaltree import IntervalTree
from libcst._position import CodePosition as CstCodePosition, CodeRange
from rich.style import Style
from rich.syntax import Syntax
from rich.tree import Tree
from textual import log
import diskcache

cst.CSTNode.__str__ = lambda self: self.__class__.__name__
cst.CSTNode.__repr__ = lambda self: self.__class__.__name__
NoValueSentinel = object()


_disk_cache = diskcache.Cache(".typescope_cache")


class DefaultIntervalTree(IntervalTree):
    def __init__(self, default=NoValueSentinel, default_factory=None):
        super().__init__()

        if default is not NoValueSentinel and default_factory is not None:
            raise ValueError("Cannot specify both default and default_factory")

        if default is NoValueSentinel and default_factory is None:
            raise ValueError("Must specify either default or default_factory")

        self.default = default
        self.default_factory = default_factory

    def __getitem__(self, index):
        try:
            start, stop = index.start, index.stop
            if start is None:
                start = self.begin()
                if stop is None:
                    return set(self)
            if stop is None:
                stop = self.end()
            result = self.overlap(start, stop)
            if result is None:
                self[index] = self.default_factory()
                return self[index]
            if result == set():
                self[index] = result
                return result
        except AttributeError:
            return self.at(index)


@total_ordering
@dataclass(frozen=True)
class CodePos(CstCodePosition):
    def __lt__(self, other):
        if self.line < other.line:
            return True
        elif self.line == other.line:
            return self.column < other.column
        else:
            return False

    def __eq__(self, other: CodePos):
        return self.line == other.line and self.column == other.column

    @classmethod
    def from_range(cls, code_r: CodeRange) -> tuple[CodePos, CodePos]:
        return cls(code_r.start.line, code_r.start.column), cls(code_r.end.line, code_r.end.column)


def node_to_rich_tree(node: cst.CSTNode) -> Tree | None:
    skipped_nodes = (cst.Module, cst.BaseParenthesizableWhitespace, cst.Newline)
    if isinstance(node, skipped_nodes):
        return None

    label = Syntax(
        cst.Module([]).code_for_node(node).strip() + f"  # {node.__class__.__name__}", "python"
    )
    tree = Tree(label)
    for child in node.children:
        if isinstance(child, cst.CSTNode) and not isinstance(child, skipped_nodes):
            try:
                tree.add(node_to_rich_tree(child))
            except Exception as e:
                print(e)
                print(child)
    return tree


class DiskCacheRepoManager(meta.FullRepoManager):
    def __init__(
        self, repo_root_dir: str, paths: Collection[str], providers: Collection["ProviderT"]
    ):
        super().__init__(repo_root_dir, paths, providers)
        self._disk_cache = diskcache.Cache(".typescope_cache")
        self._providers = providers
        # todo - handle cache invalidation differently for providers which depend on
        #  other files, e.g. FullyQualifiedNameProvider, and those which don't, e.g.
        #  ParentNodeProvider

    @property
    def _md5s(self):
        return hash(frozenset(md5(Path(path).read_text().encode("utf-8")) for path in self._paths))

    @property
    def dirty(self) -> bool:
        return self._md5s != self._disk_cache.get("md5s", None)

    def get_cache_for_path(self, path: str) -> Mapping["ProviderT", object]:
        self.resolve_cache()
        return {provider: self._disk_cache[provider][path] for provider in self._providers}

    def resolve_cache(self) -> None:
        """
        Resolve cache for all providers that require it. Normally this is called by
        :meth:`~FullRepoManager.get_cache_for_path` so you do not need to call it
        manually. However, if you intend to do a single cache resolution pass before
        forking, it is a good idea to call this explicitly to control when cache
        resolution happens.
        """
        if self.dirty:
            for provider in self._providers:
                if provider in self._disk_cache:
                    continue
                handler = provider.gen_cache
                if handler:
                    self._disk_cache[provider] = handler(self.root_path, self._paths, self._timeout)
            self._disk_cache["md5s"] = self._md5s
            # self._cache = self._disk_cache


class InferredTypeNode:
    def __init__(self, node: cst.CSTNode):
        self.node = node

    @classmethod
    def parse(cls, code: str) -> InferredTypeNode:

        # Prevent:
        # libcst._exceptions.ParserSyntaxError: Syntax Error @ 1:30.
        # Incomplete input. Unexpectedly encountered ']'.
        # Example:
        # functools._lru_cache_wrapper[]
        code = code.replace("[]", "[NoContent]")

        # Prevent:
        # libcst._exceptions.ParserSyntaxError: Syntax Error @ 1:130.
        # Incomplete input. Encountered 'to', but expected ')'.
        # Example:
        # ...
        # typing.List[Variable[_typeshed.SupportsRichComparisonT
        # (bound to typing.Union[_typeshed.SupportsDunderGT[typing.Any], _
        # typeshed.SupportsDunderLT[typing.Any]])]]),
        # ...
        code = code.replace(" (bound to ", "(bound=")

        # Prevent:
        # ParserSyntaxError: Syntax Error @ 1:130.
        # '$' is not a valid token.
        # Example:
        # BoundMethod[typing.Callable(typing.GenericMeta.__getitem__)[[Named(self, unknown),
        # typing.Tuple[typing.Any, typing.Type[Variable[$synthetic_attribute_resolution_variable]]]]
        # , typing.Type[typing.Callable[..., Variable[$synthetic_attribute_resolution_variable]]]],
        # typing.Type[typing.Callable]]
        code = code.replace("$", "__dollar_sign__")

        try:
            node = cst.parse_expression(code)
        except cst.ParserSyntaxError as e:
            node = cst.Name("ParserSyntaxError")
        result = cls(node)
        # console.print(node_to_rich_tree(node))
        return result

    def __str__(self):
        return str(self.node)

    def __repr__(self):
        return repr(self.node)


class SourceFileInfoBuilder(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (
        meta.PositionProvider,
        meta.ParentNodeProvider,
        meta.TypeInferenceProvider,
        meta.FullyQualifiedNameProvider,
    )

    # def __init__(self) -> None:
    #     super().__init__()
    #     self.node_positions = defaultdict(set)
    #     self.parents: dict[cst.CSTNode, cst.CSTNode] = {}
    #     self.types: dict[cst.CSTNode, InferredTypeNode] = {}
    #     self.fqn: dict[cst.CSTNode, QualifiedName] = {}

    # def on_visit(self, node: cst.CSTNode) -> bool:
    #     pos = self.get_metadata(cst.metadata.PositionProvider, node)
    #     pos_start = CodePos(pos.start.line, pos.start.column)
    #     pos_end = CodePos(pos.end.line, pos.end.column)
    #     if pos is not None and pos_start != pos_end and not isinstance(node, cst.Module):
    #         self.node_positions[(pos_start, pos_end)].add(node)
    #         self.parents[node] = self.get_metadata(cst.metadata.ParentNodeProvider, node)
    #     try:
    #         node_str = self.get_metadata(cst.metadata.TypeInferenceProvider, node)
    #         self.types[node] = InferredTypeNode.parse(node_str)
    #     except KeyError:
    #         pass
    #
    #     try:
    #         self.fqn[node] = self.get_metadata(cst.metadata.FullyQualifiedNameProvider, node).pop()
    #     except KeyError:
    #         pass
    #
    #     return super().on_visit(node)

    @classmethod
    def from_wrapper(
        cls,
        path: Path,
        wrapper: cst.MetadataWrapper,
    ) -> SourceFileInfo:
        metadata = wrapper.resolve_many(
            (
                meta.PositionProvider,
                meta.ParentNodeProvider,
                meta.TypeInferenceProvider,
                meta.FullyQualifiedNameProvider,
            )
        )

        positions = metadata[meta.PositionProvider]
        position_to_nodes = defaultdict(set)
        for node, pos in positions.items():
            position_to_nodes[CodePos.from_range(pos)].add(node)

        tups = ((*k, frozenset(v)) for k, v in position_to_nodes.items() if k[0] != k[1])
        tree = IntervalTree.from_tuples(tups)

        parents = metadata[meta.ParentNodeProvider]
        types = {
            k: InferredTypeNode.parse(v) for k, v in metadata[meta.TypeInferenceProvider].items()
        }
        fqn = {k: v.pop() for k, v in metadata[meta.FullyQualifiedNameProvider].items() if v}

        return SourceFileInfo(path, tree, parents, types, fqn)


class RepoInfo:
    def __init__(self, repo_root: Path | str) -> None:
        self._cache = diskcache.Cache(".typescope_full_cache")

        repo_root = Path(repo_root)
        paths = [str(p.relative_to(repo_root)) for p in Path(repo_root.absolute()).rglob("*.py")]
        self._paths = paths

        # self._position_cache = diskcache.Cache(".typescope_cache/position")
        # self._parent_cache = diskcache.Cache(".typescope_cache/parent")
        # self._types_cache = diskcache.Cache(".typescope_cache/types")
        # self._fqn_cache = diskcache.Cache(".typescope_cache/fqn")
        #
        # self._file_dependent_caches = [self._position_cache, self._parent_cache]
        # self._repo_dependent_caches = [self._types_cache, self._fqn_cache]

        self.repo_manager = DiskCacheRepoManager(
            repo_root_dir=str(repo_root),
            paths=paths,
            providers=(meta.TypeInferenceProvider, meta.FullyQualifiedNameProvider),
        )
        self.repo_root = repo_root

    def get_src_info(self, path: Path | str) -> SourceFileInfo:
        if isinstance(path, str):
            path = Path(path)

        if (
            (src_info_cache := _disk_cach["src_info"].get(path))
            and src_info_cache["repo_root"] == self.repo_root
            and src_info_cache["md5"] == md5(path.read_bytes()).hexdigest()
        ):
            return _disk_cache["src_info"][path]

        wrapper = self.repo_manager.get_metadata_wrapper_for_path(str(path))

        metadata = wrapper.resolve_many(
            (
                meta.PositionProvider,
                meta.ParentNodeProvider,
                meta.TypeInferenceProvider,
                meta.FullyQualifiedNameProvider,
            )
        )

        positions = metadata[meta.PositionProvider]
        position_to_nodes = defaultdict(set)
        for node, pos in positions.items():
            position_to_nodes[CodePos.from_range(pos)].add(node)

        tups = ((*k, frozenset(v)) for k, v in position_to_nodes.items() if k[0] != k[1])
        tree = IntervalTree.from_tuples(tups)

        parents = metadata[meta.ParentNodeProvider]
        types = {
            k: InferredTypeNode.parse(v) for k, v in metadata[meta.TypeInferenceProvider].items()
        }
        fqn = {k: v.pop() for k, v in metadata[meta.FullyQualifiedNameProvider].items() if v}

        return SourceFileInfo(path, tree, parents, types, fqn)


class SourceFileInfo:
    def __init__(
        self,
        path: Path,
        tree: IntervalTree,
        parents: dict[cst.CSTNode, cst.CSTNode],
        types: dict[cst.CSTNode, InferredTypeNode],
        fqn: dict[cst.CSTNode, QualifiedName],
    ) -> None:
        self.path = path
        self.code = self.path.read_text()
        self.tree = tree
        self.parents = parents
        self.types = types
        self.fqn = fqn

        self._lines = ["", *self.code.splitlines()]

    def __getitem__(self, index: slice | CodePos) -> set[cst.CSTNode]:
        if isinstance(index, slice):
            start = CodePos(index.start.line, index.start.column)
            stop = CodePos(index.stop.line, index.stop.column)
            return set(interval.data for interval in self.tree.overlap(start, stop))
        else:
            return set(interval.data for interval in self.tree.at(index))

    def nodes_at(self, begin: CodePos, end: CodePos | None = None) -> set[cst.CSTNode]:
        """Return all nodes at the given position.

        If end is None, return all nodes at the given position. Otherwise, return all nodes
        enveloped by the given range.

        Args:
            begin: The beginning of the range.
            end: The end of the range.

        Returns:
            A set of nodes.
        """
        if end is None:
            return set(interval.data for interval in self.tree.at(begin))
        return set(interval.data for interval in self.tree.envelop(begin, end))

    def code_at(self, start: CodePos, end: CodePos) -> str:
        log("code_at", f"{start.line}:{start.column}-{end.line}:{end.column}")
        # todo - column always 0?

        if start.line == end.line:
            return self._lines[start.line][start.column : end.column]
        else:
            lines = self._lines[start.line : end.line]
            return "\n".join(lines)

    def intervals_at(
        self,
        start: CodePos,
        end: CodePos | None = None,
    ) -> set[intervaltree.Interval]:
        end_part = f"-{end.line}:{end.column}" if end is not None else ""
        log("intervals_at", f"{start.line}:{start.column}{end_part}")
        if end is None:
            return self.tree.at(start)
        return self.tree.overlap(start, end)

    def iter_parents(self, node: cst.CSTNode) -> Iterator[cst.CSTNode]:
        while node in self.parents:
            node = self.parents[node]
            yield node


if __name__ == "__main__":
    import sys

    # if len(sys.argv) != 2:
    #     print("usage: python3 test.py <path to repo>")
    #     sys.exit(1)

    # repo_root = Path(sys.argv[1])
    console = Console(style=Style(bgcolor="#272822", color="#f8f8f2"))

    print("Loading repo info...")
    repo_info = RepoInfo(Path("/home/pedro/projs/typescope"))

    print("Loading source file...")
    src_info = repo_info.get_src_info(Path("typescope/main.py"))

    print([type(node) for node in src_info[CodePos(126, 21)]])
    print([type(node) for node in src_info[CodePos(123, 7) : CodePos(123, 30)]])
    print(src_info.code_at(CodePos(123, 7), CodePos(123, 30)))

    # for interval in my_tree:
    #     print(interval.begin, interval.end, [type(node) for node in interval])
    # print(my_tree.at(OrderedCodePosition(110, 26)))
