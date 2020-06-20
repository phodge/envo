#!/usr/bin/env python3
import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from time import sleep
from typing import Dict, List, Literal, Optional

from ilock import ILock
from loguru import logger

from envo import Env, misc, shell
from envo.misc import import_from_file, EnvoError, Inotify

__all__ = ["stage_emoji_mapping"]

package_root = Path(os.path.realpath(__file__)).parent
templates_dir = package_root / "templates"

stage_emoji_mapping: Dict[str, str] = {
    "comm": "",
    "test": "🛠",
    "ci": "🧪",
    "local": "🐣",
    "stage": "🤖",
    "prod": "🔥",
}


class Envo:
    @dataclass
    class Sets:
        stage: str
        init: bool

    environ_before = Dict[str, str]
    files_watchdog_thread: Thread
    shell: shell.Shell
    inotify: Inotify
    env_dirs: List[Path]
    quit: bool
    env: Env

    def __init__(self, sets: Sets) -> None:
        self.se = sets
        self.inotify: Inotify = None

        self.env_dirs = self._get_env_dirs()
        if not self.env_dirs:
            raise EnvoError(
                "Couldn't find any env!\n" 'Forgot to run envo --init" first?'
            )
        sys.path.insert(0, str(self.env_dirs[0]))

        self.quit: bool = False

        self.environ_before = os.environ.copy()  # type: ignore

        self._set_context_thread: Optional[Thread] = None

        self.lock_dir = Path("/tmp/envo")
        if not self.lock_dir.exists():
            self.lock_dir.mkdir()

        self.global_lock = ILock("envo_lock")
        self.global_lock._filepath = str(self.env_dirs[0] / "__envo_lock__")

    def spawn_shell(self, type: Literal["fancy", "simple", "headless"]) -> None:
        """
        :param type: shell type
        """
        self.shell = shell.shells[type].create()
        self.restart()
        self.shell.start()
        self._stop_files_watchdog()
        self._on_unload()
        self._on_destroy()

    def restart(self) -> None:
        try:
            self._stop_files_watchdog()

            os.environ = self.environ_before.copy()  # type: ignore

            if not hasattr(self, "env"):
                self.env = self.create_env()
                self._on_create()
            else:
                self._on_unload()
                self.env = self.create_env()

            self.env.validate()
            self.env.activate()

            self._on_load()
            self.shell.reset()
            self.shell.set_variable("env", self.env)
            self.shell.set_variable("environ", self.shell.environ)

            self._set_context_thread = Thread(target=self._set_context)
            self._set_context_thread.start()

            glob_cmds = [
                c for c in self.env.get_magic_functions()["command"] if c.kwargs["glob"]
            ]
            for c in glob_cmds:
                self.shell.set_variable(c.name, c)

            self.shell.pre_cmd = self._on_precmd
            self.shell.on_stdout = self._on_stdout
            self.shell.on_stderr = self._on_stderr
            self.shell.post_cmd = self._on_postcmd

            self.shell.environ.update(self.env.get_env_vars())
            self.shell.set_prompt_prefix(
                self._get_prompt_prefix(loading=self._set_context_thread.is_alive())
            )

        except EnvoError as exc:
            logger.error(exc)
            self.shell.set_prompt_prefix("❌")
            self._start_emergency_files_watchdog()
        except Exception:
            from traceback import print_exc
            print_exc()
            self.shell.set_prompt_prefix("❌")
            self._start_emergency_files_watchdog()
        else:
            self._start_files_watchdog()

    def _get_prompt_prefix(self, loading: bool = False) -> str:
        env_prefix = f"{self.env.meta.emoji}({self.env.get_full_name()})"

        if loading:
            env_prefix = "⏳" + env_prefix

        return env_prefix

    def _set_context(self) -> None:
        for c in self.env.get_magic_functions()["context"]:
            context = c()
            self.shell.update_context(context)
        self.shell.set_prompt_prefix(self._get_prompt_prefix(loading=False))

    def _on_create(self) -> None:
        for h in self.env.get_magic_functions()["oncreate"]:
            h()

    def _on_destroy(self) -> None:
        for h in self.env.get_magic_functions()["ondestroy"]:
            h()

    def _on_load(self) -> None:
        for h in self.env.get_magic_functions()["onload"]:
            h()

    def _on_unload(self) -> None:
        for h in self.env.get_magic_functions()["onunload"]:
            h()

    def _on_precmd(self, command: str) -> str:
        for h in self.env.get_magic_functions()["precmd"]:
            if re.match(h.kwargs["cmd_regex"], command):
                ret = h(command=command)  # type: ignore
                if ret:
                    command = ret
        return command

    def _on_stdout(self, command: str, out: str) -> str:
        for h in self.env.get_magic_functions()["onstdout"]:
            if re.match(h.kwargs["cmd_regex"], command):
                ret = h(command=command, out=out)  # type: ignore
                if ret:
                    out = ret
        return out

    def _on_stderr(self, command: str, out: str) -> str:
        for h in self.env.get_magic_functions()["onstderr"]:
            if re.match(h.kwargs["cmd_regex"], command):
                ret = h(command=command, out=out)  # type: ignore
                if ret:
                    out = ret
        return out

    def _on_postcmd(self, command: str, stdout: List[str], stderr: List[str]) -> None:
        for h in self.env.get_magic_functions()["postcmd"]:
            if re.match(h.kwargs["cmd_regex"], command):
                h(command=command, stdout=stdout, stderr=stderr)  # type: ignore

    def _files_watchdog(self) -> None:
        for event in self.inotify.event_gen():
            if self.quit:
                return

            # check if locked
            # locked means that other envo instance is creating temp __init__.py files
            # we don't want to handle this so we skip
            (_, type_names, path, filename) = event
            full_path = Path(path) / Path(filename)
            # print(event)

            # Disable events on global lock
            if full_path == Path(self.global_lock._filepath):
                if "IN_CREATE" in type_names:
                    self.inotify.pause(exempt=Path(self.global_lock._filepath))
                    # Enable events for lock file so inotify can be resumed on lock end

                if "IN_DELETE" in type_names:
                    self.inotify.resume()
                continue

            if "IN_CLOSE_WRITE" in type_names and Path(full_path).is_file():
                logger.info(f'\nDetected changes in "{str(full_path)}".')
                logger.info("Reloading...")
                self.restart()
                print("\r" + self.shell.prompt, end="")
                return

    def _start_emergency_files_watchdog(self) -> None:
        self.inotify = Inotify(self.env_dirs[-1])
        self.quit = False
        self.inotify.include = ["**/env_*.py"]
        self.files_watchdog_thread = Thread(target=self._files_watchdog)
        self.files_watchdog_thread.start()

    def _start_files_watchdog(self) -> None:
        self.inotify = Inotify(self.env.get_root_env().root)
        self.quit = False
        self.inotify.include = self.env.meta.watch_files
        self.inotify.exclude = self.env.meta.ignore_files
        self.files_watchdog_thread = Thread(target=self._files_watchdog)
        self.files_watchdog_thread.start()

    def _stop_files_watchdog(self) -> None:
        self.quit = True
        env_comm = self.env_dirs[0] / "env_comm.py"
        # Save the same content to trigger inotify event
        env_comm.read_text()

    def _get_env_dirs(self) -> List[Path]:
        ret = []
        path = Path(".").absolute()
        while True:
            env_file = path / f"env_{self.se.stage}.py"
            if env_file.exists():
                ret.append(path)
            else:
                if path == Path("/"):
                    break
            path = path.parent

        return ret

    def _create_init_files(self) -> None:
        """
        Create __init__.py files if not exist.

        If exist save them to __init__.py.tmp to recover later.
        This step is needed because there might be some content in existing that might crash envo.
        """

        for d in self.env_dirs:
            init_file = d / "__init__.py"

            if init_file.exists():
                init_file_tmp = d / Path("__init__.py.tmp")
                init_file_tmp.touch()
                init_file_tmp.write_text(init_file.read_text())

            if not init_file.exists():
                init_file.touch()

            init_file.write_text("# __envo_delete__")

    def _delete_init_files(self) -> None:
        """
        Delete __init__.py files if crated otherwise recover.
        :return:
        """
        for d in self.env_dirs:
            init_file = d / Path("__init__.py")
            init_file_tmp = d / Path("__init__.py.tmp")

            if init_file.read_text() == "# __envo_delete__":
                init_file.unlink()

            if init_file_tmp.exists():
                init_file.touch()
                init_file.write_text(init_file_tmp.read_text())
                init_file_tmp.unlink()

    def create_env(self) -> Env:
        env_dir = self.env_dirs[0]
        package = env_dir.name
        env_name = f"env_{self.se.stage}"
        env_file = env_dir / f"{env_name}.py"

        module_name = f"{package}.{env_name}"

        # We have to lock this part in case there's other shells concurrently executing this code
        with self.global_lock:
            self._create_init_files()

            # unload modules
            for m in list(sys.modules.keys())[:]:
                if m.startswith("env_"):
                    sys.modules.pop(m)
            try:
                module = import_from_file(env_file)
                env: Env
                env = module.Env()
                return env
            except ImportError as exc:
                raise EnvoError(f"""Couldn't import "{module_name}" ({exc}).""")
            finally:
                self._delete_init_files()

    def handle_command(self, args: argparse.Namespace) -> None:
        try:
            if args.save:
                self.create_env().dump_dot_env()
                return

            if args.command:
                self.spawn_shell("headless")
                try:
                    self.shell.default(args.command)
                except SystemExit as e:
                    sys.exit(e.code)
                else:
                    sys.exit(self.shell.history[-1].rtn)

            if args.dry_run:
                content = "\n".join(
                    [
                        f'export {k}="{v}"'
                        for k, v in self.create_env().get_env_vars().items()
                    ]
                )
                print(content)
            else:
                self.spawn_shell(args.shell)
        except EnvoError as e:
            logger.error(e)
            exit(1)


class EnvoCreator:
    @dataclass
    class Sets:
        stage: str
        addons: List[str]

    def __init__(self, se: Sets) -> None:
        self.se = se

        self.addons = ["venv"]

        unknown_addons = set(self.se.addons) - set(self.addons)
        if unknown_addons:
            raise EnvoError(f"Unknown addons {unknown_addons}")

    def _create_from_templ(
        self, templ_file: Path, output_file: Path, is_comm: bool = False
    ) -> None:
        """
        Create env file from template.

        :param templ_file:
        :param output_file:
        :param is_comm:
        :return:
        """
        from jinja2 import Environment
        Environment(keep_trailing_newline=True)

        if output_file.exists():
            raise EnvoError(f"{str(output_file)} file already exists.")

        env_dir = Path(".").absolute()
        package_name = misc.dir_name_to_pkg_name(env_dir.name)
        class_name = misc.dir_name_to_class_name(package_name) + "Env"

        if misc.is_valid_module_name(env_dir.name):
            env_comm_import = f"from env_comm import {class_name}Comm"
        else:
            env_comm_import = (
                "from pathlib import Path\n"
                f"from envo.misc import import_from_file\n\n\n"
                f'{class_name}Comm = import_from_file(Path("env_comm.py")).{class_name}Comm'
            )

        context = {
            "class_name": class_name,
            "name": env_dir.name,
            "package_name": package_name,
            "stage": self.se.stage,
            "emoji": stage_emoji_mapping.get(self.se.stage, "🙂"),
            "selected_addons": self.se.addons,
            "env_comm_import": env_comm_import,
        }

        if not is_comm:
            context["stage"] = self.se.stage

        misc.render_py_file(
            templates_dir / templ_file, output=output_file, context=context
        )

    def create(self) -> None:
        env_comm_file = Path("env_comm.py")
        if not env_comm_file.exists():
            self._create_from_templ(
                Path("env_comm.py.templ"), env_comm_file, is_comm=True
            )

        env_file = Path(f"env_{self.se.stage}.py")
        self._create_from_templ(Path("env.py.templ"), env_file)
        logger.info(f"Created {self.se.stage} environment 🍰!")


def _main() -> None:
    sys.argv[0] = "/home/kwazar/Code/opensource/envo/.venv/bin/xonsh"
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage", type=str, default="local", help="Stage to activate.", nargs="?"
    )
    parser.add_argument("--dry-run", default=False, action="store_true")
    parser.add_argument("--version", default=False, action="store_true")
    parser.add_argument("--save", default=False, action="store_true")
    parser.add_argument("--shell", default="fancy")
    parser.add_argument("-c", "--command", default=None)
    parser.add_argument("-i", "--init", nargs="?", const=True, action="store")

    args = parser.parse_args(sys.argv[1:])
    sys.argv = sys.argv[:1]

    if args.version:
        from envo.__version__ import __version__
        logger.info(__version__)
        return

    if args.init:
        if isinstance(args.init, str):
            selected_addons = args.init.split()
        else:
            selected_addons = []
        envo_creator = EnvoCreator(EnvoCreator.Sets(stage=args.stage, addons=selected_addons))
        envo_creator.create()
        return
    else:
        envo = Envo(
            Envo.Sets(stage=args.stage, init=bool(args.init))
        )
        envo.handle_command(args)


if __name__ == "__main__":
    _main()
