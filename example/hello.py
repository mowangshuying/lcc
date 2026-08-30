"""Minimal greeting example.

Builds a greeting for a name and prints it. Kept intentionally small so it can
be used as a starter sample and as a smoke test for the tooling around this
repository.

Run it directly::

    python example/hello.py
"""


def greet(name: str) -> str:
    """Return a greeting for ``name``.

    Args:
        name: The name of the person being greeted.

    Returns:
        The greeting text, e.g. ``"Hello, Claude"``.
    """
    return f"Hello, {name}"


def main() -> None:
    """Print the greeting for the person being greeted.

    Returns:
        None. The greeting is written to standard output.
    """
    print(greet("Claude"))


if __name__ == "__main__":
    main()
