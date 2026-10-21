"""Import-heavy work: each loader imports a different set of large standard modules."""

import importlib

GROUPS = {
    "network": ["asyncio", "http.client", "urllib.request", "email.parser", "ssl"],
    "data": ["decimal", "fractions", "statistics", "csv", "sqlite3", "json"],
    "text": ["xml.etree.ElementTree", "html.parser", "difflib", "textwrap", "string"],
    "tools": ["unittest.mock", "argparse", "logging.handlers", "tarfile", "zipfile"],
}


def load(group):
    return sorted(importlib.import_module(name).__name__ for name in GROUPS[group])
