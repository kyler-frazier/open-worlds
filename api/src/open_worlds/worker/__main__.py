"""Console entrypoint for the worker process."""

from __future__ import annotations

from .main import run_forever


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
