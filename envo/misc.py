import importlib.machinery
import importlib.util
from fnmatch import fnmatch
import inspect
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "dir_name_to_class_name",
    "setup_logger",
    "render_py_file",
    "render_file",
    "import_from_file",
    "EnvoError",
]


class EnvoError(Exception):
    pass


class Inotify:
    def __init__(self, root: Path):
        import inotify.adapters

        self.device = inotify.adapters.InotifyTree(str(root))
        self.include: List[Path] = []
        self.exclude: List[Path] = []
        self._tmp_watches: Dict[str, Any] = {}
        self._tmp_watches_r: Dict[str, Any] = {}
        self._pause = False
        self._pause_exemption: Optional[Path] = None

    def event_gen(self) -> Any:
        for event in self.device.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event
            full_path = Path(path) / Path(filename)

            if self._pause and full_path != self._pause_exemption:
                continue

            if any([fnmatch(str(full_path), i) for i in self.exclude]):
                continue

            if any([fnmatch(str(full_path), i) for i in self.include]):
                yield event

    def pause(self, exempt: Optional[Path] = None) -> None:
        self._pause = True
        # self.device.
        self._pause_exemption = exempt

    def resume(self) -> None:
        self._pause = False


def dir_name_to_class_name(dir_name: str) -> str:
    class_name = dir_name.replace("_", " ")
    class_name = class_name.replace("-", " ")
    class_name = class_name.replace(".", " ")
    s: str
    class_name = "".join([s.strip().capitalize() for s in class_name.split()])

    return class_name


def dir_name_to_pkg_name(dir_name: str) -> str:
    pkg_name = dir_name.replace("_", " ")
    class_name = pkg_name.replace("-", " ")
    class_name = class_name.replace(".", " ")
    s: str
    class_name = "_".join([s.strip() for s in class_name.split()])

    return class_name


def is_valid_module_name(module: str) -> bool:
    from keyword import iskeyword

    return module.isidentifier() and not iskeyword(module)


def setup_logger() -> None:
    from loguru import logger

    logger.remove()

    logger.add(
        sys.stdout,
        format="<blue>{message}</blue>",
        level="DEBUG",
        filter=lambda x: x["level"].name == "DEBUG",
    )
    logger.add(
        sys.stdout,
        format="<bold>{message}</bold>",
        level="INFO",
        filter=lambda x: x["level"].name == "INFO",
    )
    logger.add(
        sys.stderr,
        format="<bold><yellow>{message}</yellow></bold>",
        level="WARNING",
        filter=lambda x: x["level"].name == "WARNING",
    )
    logger.add(
        sys.stderr,
        format="<bold><red>{message}</red></bold>",
        level="ERROR",
        filter=lambda x: x["level"].name == "ERROR",
    )


def render_file(template_path: Path, output: Path, context: Dict[str, Any]) -> None:
    from jinja2 import StrictUndefined, Template

    template = Template(template_path.read_text(), undefined=StrictUndefined)
    output.write_text(template.render(**context))


def render_py_file(template_path: Path, output: Path, context: Dict[str, Any]) -> None:
    import black

    render_file(template_path, output, context)
    try:
        black.main([str(output), "-q"])
    except SystemExit:
        pass


def import_from_file(path: Path) -> Any:
    if not path.is_absolute():
        frame = inspect.stack()[1]
        caller_path_dir = Path(frame[1]).parent
        path = caller_path_dir / path

    loader = importlib.machinery.SourceFileLoader(str(path), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    return module
