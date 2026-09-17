from __future__ import annotations

import argparse
import json
from pathlib import Path

from cascadesignal.config import load_config


def main -> None:
 parser = argparse.ArgumentParser(
 description="Write deterministic setup-stage metadata."
 )
 parser.add_argument("stage")
 parser.add_argument("output", type=Path)
 args = parser.parse_args

 config = load_config
 args.output.parent.mkdir(parents=True, exist_ok=True)
 args.output.write_text(
 json.dumps(
 {
 "stage": args.stage,
 "project": config.name,
 "data_root": str(config.data.root),
 },
 indent=2,
 sort_keys=True,
 )
 + "\n"
 )


if __name__ == "__main__":
 main
