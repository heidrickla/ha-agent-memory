"""The add-on image carries every module. Tests import from the source tree, so a module the
Dockerfile leaves out passes them and fails at start with ModuleNotFoundError."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "agent_memory"


def test_the_image_copies_and_compiles_every_module():
    modules = {p.name for p in SRC.glob("*.py")}
    lines = (SRC / "Dockerfile").read_text(encoding="utf-8").splitlines()
    copied = next(line for line in lines if line.startswith("COPY ") and "server.py" in line).split()[1:-1]
    compiled = next(line for line in lines if "py_compile" in line).split("py_compile", 1)[1].split()
    assert set(copied) == modules and set(compiled) == modules
