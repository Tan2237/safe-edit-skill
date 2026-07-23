"""Console entry point for the safe-edit CLI fallback."""

import sys

import safe_edit


def main():
    return safe_edit.main(sys.argv[1:])
