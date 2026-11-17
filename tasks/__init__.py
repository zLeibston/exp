"""Load an independent domain adapter from tasks/<domain>/ without pipeline branches."""
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re
import sys
from tasks.base import TaskSuite, validate_cases


def load_domain(domain, *, split="train", limit=None, dataset=None):
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[0-9]+)*", domain):
        raise ValueError("Domain must be a lowercase package name, optionally followed by a numeric version")
    module_name = f"tasks.{domain.replace('.', '_')}"
    if '.' in domain and module_name not in sys.modules:
        entry = Path(__file__).parent / domain / '__init__.py'
        if not entry.is_file():
            raise ValueError(f"Unknown task domain: {domain}")
        spec = spec_from_file_location(module_name, entry)
        module = module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    try:
        adapter = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise ValueError(f"Unknown task domain: {domain}") from exc
        raise
    factory = getattr(adapter, 'load', None)
    if not callable(factory):
        raise ValueError(f"Domain {domain} must export load(split=..., dataset=...)")
    suite = factory(split=split, dataset=dataset)
    if not isinstance(suite, TaskSuite) or suite.name != domain or suite.split != split:
        raise ValueError("Domain adapter returned an incompatible TaskSuite")
    validate_cases(suite.cases)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be positive")
        suite.cases = suite.cases[:limit]
    return suite
