import sys

from . import wrapper


def main() -> int:
    return wrapper.main()


if __name__ == "__main__":
    sys.exit(main())
