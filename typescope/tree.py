from __future__ import annotations

from libcst._nodes import base

base.CSTNode.__str__ = lambda self: self.__class__.__name__
base.CSTNode.__repr__ = lambda self: self.__class__.__name__

import libcst

libcst._nodes.base.CSTNode.__str__ = lambda self: self.__class__.__name__
libcst._nodes.base.CSTNode.__repr__ = lambda self: self.__class__.__name__

import time
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from functools import total_ordering, lru_cache
from hashlib import md5
from pathlib import Path
from typing import Iterator, Mapping, cast

from libcst import MetadataWrapper
from libcst.metadata import ProviderT, QualifiedName
from rich.console import Console
import intervaltree.interval
import libcst as cst
import libcst.metadata as meta
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
    def from_cst_range(cls, code_range: CodeRange) -> tuple[CodePos, CodePos]:
        start = code_range.start.line, code_range.start.column
        end = code_range.end.line, code_range.end.column
        return cls(*start), cls(*end)

    def as_tuple(self) -> tuple[int, int]:
        return self.line, self.column

    def __str__(self):
        return f"{self.line}:{self.column}"

    def __repr__(self):
        return f"<CodePos {self.line}:{self.column}>"


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
        if self.dirty:
            self._disk_cache.clear()

        self._providers = providers
        # todo - handle cache invalidation differently for providers which depend on
        #  other files, e.g. FullyQualifiedNameProvider, and those which don't, e.g.
        #  ParentNodeProvider

    def calculate_md5(self) -> int:
        return hash(frozenset(md5(Path(path).read_bytes()) for path in self._paths))

    @property
    def dirty(self) -> bool:
        cache_md5 = self._disk_cache.get("md5")
        current_md5 = self.calculate_md5()
        return current_md5 != cache_md5

    def get_cache_for_path(self, path: str) -> Mapping["ProviderT", object]:
        self.resolve_cache()
        # path = str(Path(path).relative_to(self._repo_root_dir))
        return {provider: self._disk_cache[provider][path] for provider in self._providers}

    def resolve_cache(self) -> None:
        """
        Resolve cache for all providers that require it. Normally this is called by
        :meth:`~FullRepoManager.get_cache_for_path` so you do not need to call it
        manually. However, if you intend to do a single cache resolution pass before
        forking, it is a good idea to call this explicitly to control when cache
        resolution happens.
        """
        for provider in self._providers:
            if provider in self._disk_cache:
                continue
            t = time.perf_counter()
            handler = provider.gen_cache
            if handler:
                self._disk_cache[provider] = handler(self.root_path, self._paths, self._timeout)
            t = time.perf_counter() - t
            print(f"Resolved cache for {provider} in {t:.3f}s")
        self._disk_cache["md5"] = self.calculate_md5()

    def get_metadata_wrapper_for_path(self, path: str) -> MetadataWrapper:
        """
        Create a :class:`~libcst.metadata.MetadataWrapper` given a source file path.
        The path needs to be a path relative to project root directory.
        The source code is read and parsed as :class:`~libcst.Module` for
        :class:`~libcst.metadata.MetadataWrapper`.

        .. code-block:: python

            manager = FullRepoManager(".", {"a.py", "b.py"}, {TypeInferenceProvider})
            wrapper = manager.get_metadata_wrapper_for_path("a.py")
        """

        print(f"Parsing module for {path}")
        file_contents = Path(path).read_text()
        module = cst.parse_module(file_contents)
        cache = self.get_cache_for_path(path)

        # todo - more granular, per-file cache:
        # file_md5 = md5(file_contents := Path(path).read_bytes()).hexdigest()
        # if (cached_module := self._module_cache.get(path, {})).get("md5") == file_md5:
        #     print(f"Using cached module for {path}")
        #     module = cached_module["module"]
        # else:
        #     print(f"Parsing module for {path}")
        #     module = cst.parse_module(file_contents)
        #     self._module_cache[path] = {"md5": file_md5, "module": module}
        # cache = self.get_cache_for_path(path)
        return MetadataWrapper(module, True, cache)


class InferredTypeNode:
    def __init__(self, inferred_type_str: str):
        self.inferred_type_str = inferred_type_str  # neat

    @property
    def node(self) -> cst.CSTNode:

        # Prevent:
        # libcst._exceptions.ParserSyntaxError: Syntax Error @ 1:30.
        # Incomplete input. Unexpectedly encountered ']'.
        # Example:
        # functools._lru_cache_wrapper[]
        code = self.inferred_type_str.replace("[]", "[NoContent]")

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
            node = cst.Name("InferredTypeParserSyntaxError")
        return node

    def __str__(self):
        return str(self.inferred_type_str)

    def __repr__(self):
        return repr(self.inferred_type_str)


class RepoInfo:
    def __init__(self, repo_root: Path | str) -> None:
        self._cache = diskcache.Cache(".typescope_full_cache")

        repo_root = Path(repo_root).absolute()
        # paths = [str(p.relative_to(repo_root)) for p in Path(repo_root.absolute()).rglob("*.py")]
        paths = [str(p.relative_to(repo_root)) for p in repo_root.rglob("*.py")]
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
            providers=(
                meta.TypeInferenceProvider,
                meta.FullyQualifiedNameProvider,
            ),
        )
        self.repo_root = repo_root

    @lru_cache(maxsize=512)
    def get_src_info(self, path: Path | str) -> SourceFileInfo:
        if isinstance(path, str):
            path = Path(path)
        if path.is_absolute():
            path = path.relative_to(self.repo_root)

        wrapper = self.repo_manager.get_metadata_wrapper_for_path(str(path))

        metadata = wrapper.resolve_many(
            (
                meta.PositionProvider,
                meta.ParentNodeProvider,
                meta.TypeInferenceProvider,
                meta.FullyQualifiedNameProvider,
            )
        )

        meta_positions = cast(Mapping[cst.CSTNode, CodeRange], metadata[meta.PositionProvider])
        meta_inferred_types = cast(Mapping[cst.CSTNode, str], metadata[meta.TypeInferenceProvider])
        meta_parents = cast(Mapping[cst.CSTNode, cst.CSTNode], metadata[meta.ParentNodeProvider])
        meta_fully_qualified_names = cast(
            Mapping[cst.CSTNode, set[QualifiedName]], metadata[meta.FullyQualifiedNameProvider]
        )

        # Collect nodes in the file and build:
        #   - node_data: a dictionary mapping code positions to sets of NodeInfo instances
        #   - nodes_to_info: a dictionary CSTNode to their corresponding NodeInfo instances
        node_data = defaultdict[tuple[CodePos, CodePos], set[NodeInfo]](set)
        nodes_to_info: dict[cst.CSTNode, NodeInfo] = {}

        for node, pos in meta_positions.items():
            type_info = meta_inferred_types.get(node)
            parent = meta_parents.get(node)
            fqn_set = meta_fully_qualified_names.get(node)
            fqn = fqn_set.pop() if fqn_set else None

            pos_start = CodePos(pos.start.line, pos.start.column)
            pos_end = CodePos(pos.end.line, pos.end.column)

            node_data[(pos_start, pos_end)].add(
                new_node := NodeInfo(
                    node=node,
                    code_start=pos_start,
                    code_end=pos_end,
                    inferred_type=InferredTypeNode(type_info) if type_info else None,
                    parent=parent,
                    fqn=fqn,
                    file_path=path,
                )
            )
            nodes_to_info[new_node.node] = new_node

        # Add node parent data to NodeInfo instances from the now-filled
        # node_data dictionary
        node_infos = defaultdict[tuple[CodePos, CodePos], set[NodeInfo]](set)
        for pos, node_data_items in node_data.items():

            # Don't keep null nodes (e.g. empty whitespace)
            if pos[0] == pos[1]:
                continue

            node_infos[pos].update(node_data_items)
            for node in node_data_items:
                node.parent = nodes_to_info.get(node.parent)
                if node.parent is not None:
                    node.parent.children.add(node)

        # Turn
        tups = ((*k, frozenset(v)) for k, v in node_infos.items())
        tree = IntervalTree.from_tuples(tups)

        return SourceFileInfo(path, tree)


class SourceFileInfo:
    def __init__(
        self,
        path: Path,
        tree: IntervalTree,
    ) -> None:
        self.path = path
        self.code = self.path.read_text()
        self.tree = tree
        self.node_infos = {ni for ni_set in tree.items() for ni in ni_set.data}
        self.qualified_names_to_node_infos: dict[QualifiedName, set[NodeInfo]] = defaultdict(set)
        for node_info in self.node_infos:
            if node_info.fqn is not None:
                self.qualified_names_to_node_infos[node_info.fqn].add(node_info)

        self._lines = ["", *self.code.splitlines()]

    # def __getitem__(self, index: slice | CodePos) -> set[NodeInfo]:
    #     if isinstance(index, slice):
    #         start = CodePos(index.start.line, index.start.column)
    #         stop = CodePos(index.stop.line, index.stop.column)
    #         return set(interval.data for interval in self.tree.overlap(start, stop))
    #     else:
    #         return set(interval.data for interval in self.tree.at(index))

    @lru_cache(maxsize=64)
    def code_at(self, start: CodePos, end: CodePos) -> str:
        log("code_at", f"{start.line}:{start.column}-{end.line}:{end.column}")
        # todo - column always 0?

        if start.line == end.line:
            return self._lines[start.line][start.column : end.column]
        else:
            lines = self._lines[start.line : end.line]
            return "\n".join(lines)

    @lru_cache(maxsize=64)
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

    @lru_cache(maxsize=64)
    def nodes_at(self, start: CodePos, end: CodePos | None = None) -> set[NodeInfo]:
        return {node for interval in self.intervals_at(start, end) for node in interval.data}

    def iter_parents(self, node: cst.CSTNode | NodeInfo) -> Iterator[cst.CSTNode]:
        while node.parent is not None:
            node = node.parent
            yield node

    def occurences_of_name(self, name: str | NodeInfo) -> set[NodeInfo]:
        if isinstance(name, str):
            node_fqn = next(ni for ni in self.qualified_names_to_node_infos if ni.name == name)
        else:
            node_fqn = name

        return self.qualified_names_to_node_infos[node_fqn]

    def minimal_node_from_set(self, nodes: set[NodeInfo]) -> NodeInfo:
        """Return the smallest (in terms of tree position) node from the given set.

        Args:
            nodes: A set of nodes.

        Returns:
            The smallest node.
        """
        seen_parents = set()
        min_node = None
        for node in nodes:
            if node not in seen_parents:
                min_node = node
            for parent in node.iter_parents():
                seen_parents.add(parent)
        return min_node


@dataclass
class NodeInfo:
    node: cst.CSTNode
    code_start: CodePos
    code_end: CodePos
    inferred_type: InferredTypeNode | None
    fqn: QualifiedName | None
    parent: cst.CSTNode | None
    children: set[NodeInfo] = field(default_factory=set)
    file_path: Path | None = None

    def iter_parents(self) -> Iterator[NodeInfo]:
        node = self
        while node.parent is not None:
            node = node.parent
            yield node

    def __hash__(self) -> int:
        return hash(self.node)

    def __str__(self) -> str:
        if isinstance(self.node, cst.Name):
            name = f"{self.node.__class__.__name__}({self.node.value})"
        elif isinstance(self.node, (cst.FunctionDef, cst.ClassDef)):
            name = f"{self.node.__class__.__name__}({self.node.name.value})"
        else:
            name = self.node.__class__.__name__

        # fname = self.file_path.name if self.file_path is not None else "<unknown>"
        return name

    __repr__ = __str__


def _demo():

    # if len(sys.argv) != 2:
    #     print("usage: python3 test.py <path to repo>")
    #     sys.exit(1)

    # repo_root = Path(sys.argv[1])
    console = Console(style=Style(bgcolor="#272822", color="#f8f8f2"))

    print("Loading repo info...")
    repo_info = RepoInfo(Path("/home/pedro/projs/typescope"))

    print("Loading source file...")
    src_info = repo_info.get_src_info(Path("typescope/main.py"))

    print([type(node) for node in src_info[CodePos(126, 18)]])
    print([type(node) for node in src_info[CodePos(123, 7) : CodePos(123, 30)]])
    ns = src_info.nodes_at(CodePos(126, 18))
    print([node for node in ns])
    for node in ns:
        print(node)
        i = 1
        for np in node.iter_parents():
            if np.fqn is not None:
                print("  " * i, np.fqn.name)
                i += 1
        print("===")

    # for interval in my_tree:
    #     print(interval.begin, interval.end, [type(node) for node in interval])
    # print(my_tree.at(OrderedCodePosition(110, 26)))


if __name__ == "__main__":
    _demo()
