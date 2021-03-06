import os
import re
from tests.unit import utils

environ_before = os.environ.copy()


class TestCommands(utils.TestBase):
    def test_repr(self):
        utils.init()
        utils.flake_cmd(prop=False, glob=False)
        utils.mypy_cmd(prop=False, glob=False)
        e = utils.env()
        assert re.match(
            (
                r"# Variables\n"
                r"root: Field = PosixPath\('.*'\)\n"
                r"stage: Field = 'test'\n"
                r"envo_stage: Field = 'test'\n"
                r"pythonpath: Field = .*\n"
                r"# context\n"
                r"# command\n"
                r"""flake\(test_arg: str = ""\) -> str   {glob=False, prop=False}\n"""
                r"""mypy\(test_arg: str = ""\) -> None   {glob=False, prop=False}\n"""
                r"# precmd\n"
                r"# onstdout\n"
                r"# onstderr\n"
                r"# postcmd\n"
                r"# onload\n"
                r"# oncreate\n"
                r"# ondestroy\n"
                r"# onunload"
            ),
            repr(e),
        )

        utils.init()
        utils.flake_cmd(prop=True, glob=False)
        utils.mypy_cmd(prop=True, glob=False)
        e = utils.env()
        assert re.match(
            (
                r"# Variables\n"
                r"root: Field = PosixPath\('.*'\)\n"
                r"stage: Field = 'test'\n"
                r"envo_stage: Field = 'test'\n"
                r"pythonpath: Field = .*\n"
                r"# context\n"
                r"# command\n"
                r"""flake\(test_arg: str = ""\) -> str   {glob=False, prop=True}\n"""
                r"""mypy\(test_arg: str = ""\) -> None   {glob=False, prop=True}\n"""
                r"# precmd\n"
                r"# onstdout\n"
                r"# onstderr\n"
                r"# postcmd\n"
                r"# onload\n"
                r"# oncreate\n"
                r"# ondestroy\n"
                r"# onunload"
            ),
            repr(e),
        )

        utils.init()
        utils.mypy_cmd(prop=True, glob=False)

    def test_property_cmd(self, capsys):
        utils.init()
        utils.flake_cmd(prop=True, glob=False)

        e = utils.env()
        assert repr(e.flake) == "Flake return value"
        assert capsys.readouterr().out == "Flake all good\n"

    def test_call_cmd(self, capsys):
        utils.init()
        utils.flake_cmd(prop=False, glob=False)

        e = utils.env()
        assert e.flake() == "Flake return value"
        assert capsys.readouterr().out == "Flake all good\n"

    def test_property_cmd_no_ret(self, capsys):
        utils.init()
        utils.mypy_cmd(prop=True, glob=False)

        e = utils.env()
        assert repr(e.mypy) == "\b"
        assert capsys.readouterr().out == "Mypy all good\n"
