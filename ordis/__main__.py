"""Allow ``python -m ordis`` to use the same CLI as ``ordis``."""

from .ordisd import main


if __name__ == "__main__":
    raise SystemExit(main())
