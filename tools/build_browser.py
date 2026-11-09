"""Build a static browser demo using this checkout's wheel."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from shutil import copy2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".cache/browser-site"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = args.output.resolve()
    wheels = output / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=root, check=True
    )
    import tomllib

    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    wheel = f"anyshell-{version}-py3-none-any.whl"
    assert (wheels / wheel).is_file()
    for name in ("index.html", "browser.js", "app.py"):
        copy2(root / "examples/browser" / name, output / name)
    (output / "wheel.json").write_text(json.dumps({"wheel": f"wheels/{wheel}"}) + "\n")
    (output / ".nojekyll").touch()
    print(f"Built browser demo in {output}")


if __name__ == "__main__":
    main()
