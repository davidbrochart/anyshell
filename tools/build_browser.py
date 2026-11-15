"""Build a static browser demo using this checkout's wheel."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path
from shutil import copy2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".cache/browser-site"))
    parser.add_argument(
        "--anytext-source", type=Path, help="Build and bundle a local Anytext checkout."
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = args.output.resolve()
    wheels = output / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=root, check=True
    )
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = f"anyshell-{version}-py3-none-any.whl"
    assert (wheels / wheel).is_file()
    manifest = {"wheel": f"wheels/{wheel}"}
    if args.anytext_source is not None:
        source = args.anytext_source.resolve()
        project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
        if project["name"] != "anytext":
            parser.error("--anytext-source must point to an Anytext checkout")
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=source, check=True
        )
        anytext_wheel = f"anytext-{project['version']}-py3-none-any.whl"
        assert (wheels / anytext_wheel).is_file()
        manifest["anytext"] = f"wheels/{anytext_wheel}"
    for name in ("index.html", "browser.js", "app.py"):
        copy2(root / "examples/browser" / name, output / name)
    (output / "wheel.json").write_text(json.dumps(manifest) + "\n")
    (output / ".nojekyll").touch()
    print(f"Built browser demo in {output}")


if __name__ == "__main__":
    main()
