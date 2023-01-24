"""
Code browser example.

Run with:

    python code_browser.py PATH

"""


import libcst._nodes.base

libcst._nodes.base.CSTNode.__str__ = lambda self: self.__class__.__name__
libcst._nodes.base.CSTNode.__repr__ = lambda self: self.__class__.__name__


import sys
from functools import lru_cache
from pathlib import Path

import rich
from rich.console import RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.traceback import Traceback
from rich.tree import Tree

from textual import events, log as default_log, Logger
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, Content, Horizontal
from textual.reactive import var, reactive
from textual.scroll_view import ScrollView
from textual.widgets import DirectoryTree, Footer, Header, Static, Placeholder
from textual.widgets._data_table import Column
import libcst as cst

from tree import SourceFileInfo, CodePos, RepoInfo


cst.CSTNode.__str__ = lambda self: self.__class__.__name__
cst.CSTNode.__repr__ = lambda self: self.__class__.__name__


def custom_log(*args, **kwargs):
    args = [arg if not isinstance(arg, cst.CSTNode) else arg.__class__.__name__ for arg in args]
    kwargs = {
        key: value if not isinstance(value, cst.CSTNode) else value.__class__.__name__
        for key, value in kwargs.items()
    }
    default_log(*args, **kwargs)


# class MyLogger(Logger):
#     def _log(self, *args, **kwargs):
#         print(f"args, kwargs: {args, kwargs}")
#         args = [arg if not isinstance(arg, cst.CSTNode) else arg.__class__.__name__ for arg in args]
#         kwargs = {
#             key: value if not isinstance(value, cst.CSTNode) else value.__class__.__name__
#             for key, value in kwargs.items()
#         }
#
#         super().log(*args, **kwargs)


log = default_log

# class MyTree(cst.CSTVisitor):
#     def __init__(self):
#         self.tree = Tree("root")
#         self.current = self.tree
#         self.stack = []


@lru_cache
def code_for_file(path: Path) -> SourceFileInfo:
    return SourceFileInfo.from_path(path)


def node_to_rich_tree(node: cst.CSTNode) -> Tree | None:
    module = cst.Module([])

    skipped_nodes = (cst.Module, cst.BaseParenthesizableWhitespace, cst.Newline)
    if isinstance(node, skipped_nodes):
        return None

    label = Syntax(module.code_for_node(node).strip() + f"  # {node.__class__.__name__}", "python")
    tree = Tree(label)
    for child in node.children:
        if isinstance(child, cst.CSTNode) and not isinstance(child, skipped_nodes):
            try:
                tree.add(node_to_rich_tree(child))
            except Exception as e:
                print(e)
                print(child)
    return tree


class _CodeView(Static):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.file: SourceFileInfo = code_for_file(path)

    def render(self) -> RenderableType:
        return Syntax(self.file.code, "python", line_numbers=True)


class CodeView(Static, can_focus=True):
    """A widget for displaying code."""

    def _default_file(self) -> Path:
        default = self.root_path.glob("**/*.py").__next__().relative_to(self.root_path)
        print("default", default)
        return default

    file: reactive[Path] = reactive[Path](_default_file, layout=True)

    def __init__(
        self,
        root_path: Path,
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            renderable=":)",
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
        )
        self._code_views = {}
        self._code_view: _CodeView

        self.root_path = root_path
        self._repo_manager = RepoInfo(self.root_path)

        self.watch_file(self.file)

    def on_mount(self) -> None:
        self.watch_file(self.file)

    def watch_file(self, file: Path):
        file = Path(file).absolute().relative_to(self.root_path.absolute())
        self._code_info = self._repo_manager.get_src_info(file)
        self.update()

    async def on_click(self, event) -> None:
        log.error("on_click", event)
        line = event.y + 1
        column = event.x - 6

        intervals = self._code_info.intervals_at(CodePos(line, column))
        codes = []
        code_types = {}
        minimal_node = None
        seen_parents = set()
        minimal_chain = []
        for interval in intervals:
            for node in interval.data:

                if node not in seen_parents:
                    minimal_node = node
                for parent in self._code_info.iter_parents(node):
                    seen_parents.add(parent)

            # code = self._code_info.code_at(interval.begin, interval.end)

            # codes.append(code)
            # code_types[code] = interval
        minimal_chain = [minimal_node, *self._code_info.iter_parents(minimal_node)]
        log("min", minimal_node)
        # log("fqn", self._code_info.fqn)
        module = cst.Module([])
        for node in minimal_chain[::-1]:
            code = module.code_for_node(node).strip() + "\n\n# " + node.__class__.__name__
            # type_info = self._code_info.types.get(node, None)
            fqns = self._code_info.fqn.get(node, None)

            # if type_info:
            #     code += f"\n# type: {module.code_for_node(type_info.node)}"
            if fqns:
                for fqn in fqns:
                    code += f"\n# fqn: {fqn.name}\n# fqn (source): {fqn.source}\n"

            log(Syntax(code, "python"))

        codes.sort(key=len)
        for code in codes:
            log("====")
            code += f"\n# {code_types[code].data}"
            log(Syntax(code, "python", line_numbers=True))
            log("min", minimal_node, minimal_chain)

    def render(self) -> RenderableType:
        return Syntax(self._code_info.code, "python", line_numbers=True)


class TypeView(Static):
    """A widget for displaying type information."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    def on_mount(self) -> None:
        self.update(self._get_renderable())

    def _get_renderable(self) -> RenderableType:
        tree = Tree("fuck that")
        tree.add("and fuck this")

        panel = Panel(tree)
        return panel


class CodeBrowser(App):
    """Textual code browser app."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2;
        grid-gutter: 1;
    }
    Vertical {
        width: 100%;
        height: 100%;
        row-span: 2;
        text-style: bold;
    }

    """

    BINDINGS = [
        ("f", "toggle_files", "Toggle Files"),
        ("q", "quit", "Quit"),
    ]

    show_tree = var(True)

    def __init__(self):

        super().__init__()
        self.path = Path("/home/pedro/projs/asynkets/")
        self._code_view = CodeView(
            self.path,
            id="codeview",
            classes="box",
        )
        self._type_view = TypeView(id="typeview", classes="box")

    def watch_show_tree(self, show_tree: bool) -> None:
        """Called when show_tree is modified."""
        self.set_class(show_tree, "-show-tree")

    def compose(self) -> ComposeResult:
        """Compose our UI."""
        yield Header()
        # path = "./" if len(sys.argv) < 2 else sys.argv[1]

        yield DirectoryTree(str(self.path), classes="box")
        yield Vertical(self._code_view, id="codeview")
        yield self._type_view
        yield Footer()

    def on_mount(self, event: events.Mount) -> None:
        self.query_one(DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Called when the user click a file in the directory tree."""
        event.stop()
        self._code_view.file = event.path

    def action_toggle_files(self) -> None:
        """Called in response to key binding."""
        self.show_tree = not self.show_tree


if __name__ == "__main__":
    CodeBrowser().run()
