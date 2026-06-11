import sys

from sandboxed_jupyter import wrapper


def main() -> int:
    return wrapper.main()


if __name__ == "__main__":
    sys.exit(main())
