"""
Code browser example.

Run with:

    python code_browser.py PATH

"""
import asyncio
import time
from itertools import chain
from pathlib import Path

from libcst.metadata import QualifiedNameSource
from rich.console import RenderableType, Group
from rich.padding import Padding
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from rich.tree import Tree
from textual import events
from textual._types import MessageTarget
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.reactive import var, reactive
from textual.widgets import DirectoryTree, Footer, Header, Static, TreeNode
from textual.widgets._directory_tree import DirEntry

from typescope.tree import SourceFileInfo, CodePos, RepoInfo, NodeInfo


class CodeMouseOverEvent(events.Event):
    node: NodeInfo
    bubble = True

    def __init__(self, node: NodeInfo, sender: MessageTarget):
        # print("CodeClickEvent", node)
        super().__init__(sender)
        self.node = node


class CustomDirectoryTree(DirectoryTree):
    """A custom subclass of DirectoryTree which:

    - Doesn't render hidden files
    - Highlights Python files differently
    - Doesn't render __pycache__ directories
    """

    def load_directory(self, node: TreeNode[DirEntry]) -> None:
        assert node.data is not None
        dir_path = Path(node.data.path)
        node.data.loaded = True
        directory = sorted(
            list(dir_path.iterdir()),
            key=lambda path: (not path.is_dir(), path.name.lower()),
        )
        for path in directory:
            if path.name.startswith("."):
                continue
            if path.name == "__pycache__":
                continue

            label = Text(path.name)
            if path.is_dir() and path.rglob("*.py"):
                label.stylize(Style(color="green"))
            elif path.suffix.lower() == ".py":
                pass
            else:
                label.stylize(Style(dim=True))

            node.add(
                label,
                data=DirEntry(str(path), path.is_dir()),
                allow_expand=path.is_dir(),
            )
        node.expand()


class CodeView(Static, can_focus=True):
    """A widget for displaying code."""

    active_file = reactive[Path | None](None, layout=True)
    file_contents = reactive[str | None](None, layout=True)
    highlighted_nodes = reactive[tuple[NodeInfo, ...]]((), layout=True)

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

        self.root_path = root_path
        self._repo_manager = RepoInfo(self.root_path)
        self._code_info: SourceFileInfo | None = None

        self._rel_file: Path | None = None

    async def watch_active_file(self, file: Path | None) -> None:
        if file is None:
            return
        file = self.root_path / file

        if file.is_dir() or file.suffix.lower() not in (".py", ".pyi", ".pyx"):
            return

        self.log.info("watch_active_file", file)
        self._rel_file = file.relative_to(self.root_path.absolute())
        self.log.info("watch_active_file", self._rel_file)
        self.file_contents = file.read_text()

        def _set_code_info():
            self._code_info = self._repo_manager.get_src_info(file.absolute())

        # Offload the processing of source files to the default
        # executor, so we can load the file contents in the main
        # thread with less latency.
        asyncio.create_task(asyncio.to_thread(_set_code_info))

    def on_click(self, event: events.Click) -> None:
        self.log.info("click", event)
        line = event.y + 1
        column = event.x - 6

        nodes = self._code_info.nodes_at(CodePos(line, column))
        for node in nodes:
            if node.fqn is not None:
                self.highlighted_nodes = (node,)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        # self.log.error("mouse move", event)
        self._trigger_type_display_update(event)

    def on_mouse_scroll_down(self, event) -> None:
        # self.log.info("scroll down", event)
        self._trigger_type_display_update(event)

    def on_mouse_scroll_up(self, event) -> None:
        # self.log.info("scroll up", event)
        self._trigger_type_display_update(event)

    def _trigger_type_display_update(
        self, event: events.MouseMove | events.MouseScrollUp | events.MouseScrollDown
    ) -> None:
        line = event.y + 1
        column = event.x - 6

        if self._code_info is None:
            return

        nodes_at_pos = self._code_info.nodes_at(CodePos(line, column))
        self.emit_no_wait(
            CodeMouseOverEvent(
                node=self._code_info.minimal_node_from_set(nodes_at_pos), sender=self
            )
        )

    def render(self) -> RenderableType:
        if self.file_contents is None:
            return Text("No file selected", Style(color="red"))

        syntax = Syntax(self.file_contents, "python", line_numbers=True)
        self.log("highlighted_nodes", self.highlighted_nodes)
        if self.highlighted_nodes:
            for node in self.highlighted_nodes:
                for ch in (node, *node.children):
                    self.log("highlighted_node", ch)
                    occurences = self._code_info.occurences_of_name(ch)
                    for occurence in occurences:
                        syntax.stylize_range(
                            Style(underline=True),
                            occurence.code_start.as_tuple(),
                            occurence.code_end.as_tuple(),
                        )
                        self.log.info("render occurence", occurence)
                        # todo - fix this, doesn't seem to do anything

        return syntax


class TypeView(Static):
    """A widget for displaying type information."""

    selected_node = reactive[NodeInfo | None](None, layout=True)

    def render(self) -> RenderableType:
        if self.selected_node is None:
            return Text("No node selected", Style(color="red"))

        nodes_hierarchy = [self.selected_node, *self.selected_node.iter_parents()][::-1]
        root = Tree(Text(str(nodes_hierarchy[0]), Style(color="green", bold=True)))
        node = root
        for other_node in nodes_hierarchy:
            if other_node.fqn:
                node = node.add(Text(other_node.fqn.name, Style(color="green")))

        most_specific_typed_parent: NodeInfo | None = (
            self.selected_node
            if self.selected_node.inferred_type
            else next(
                (parent for parent in self.selected_node.iter_parents() if parent.inferred_type),
                None,
            )
        )

        if self.selected_node.inferred_type:
            type_text = Text.from_markup(f"Type: [bold yellow]{self.selected_node.inferred_type}")
        else:
            if most_specific_typed_parent is None:
                type_text = Text("No type information", Style(dim=True))
            else:
                type_text = Text.from_markup(
                    f"Type: [bold yellow]{most_specific_typed_parent.inferred_type}[/]\n"
                    f"\t[dim](from {most_specific_typed_parent.fqn.name})[/dim]",
                )

        if self.selected_node.fqn:
            to_show_fqn = self.selected_node.fqn
        elif most_specific_typed_parent is not None:
            to_show_fqn = most_specific_typed_parent.fqn
        else:
            to_show_fqn = None

        if to_show_fqn:
            src = {
                QualifiedNameSource.IMPORT: "import",
                QualifiedNameSource.BUILTIN: "builtin",
                QualifiedNameSource.LOCAL: "local",
            }[to_show_fqn.source]
            fqn_text = Text.from_markup(
                f"Full name: [bold green]{to_show_fqn.name}[/]\nSource: [blue]{src}[/]"
            )
        else:
            fqn_text = Text("(no name information)\n", Style(dim=True))

        renderable = Group(
            Text.from_markup(f"Node: [bold green]{self.selected_node}[/]"),
            fqn_text,
            type_text,
            Padding(root, (2, 2)),
        )
        return renderable


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

    def __init__(self, project_path: Path = Path.cwd()) -> None:

        super().__init__()
        self.path = project_path
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

        yield CustomDirectoryTree(str(self.path), classes="box")
        yield Vertical(self._code_view, id="codeview")
        yield self._type_view
        yield Footer()

    def on_mount(self, event: events.Mount) -> None:
        self.query_one(DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Called when the user click a file in the directory tree."""
        event.stop()
        self._code_view.active_file = event.path

    def on_code_mouse_over_event(self, event: CodeMouseOverEvent) -> None:
        self._type_view.selected_node = event.node
        self._type_view.render()

    def action_toggle_files(self) -> None:
        """Called in response to key binding."""
        self.show_tree = not self.show_tree


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, default=Path.cwd())
    args = parser.parse_args()
    CodeBrowser(args.path).run()


if __name__ == "__main__":
    main()
