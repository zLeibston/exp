"""Load an independent domain adapter from tasks/<domain>/ without pipeline branches."""
from importlib import import_module
import re
from tasks.base import TaskSuite, validate_cases


def load_domain(domain, *, split="train", limit=None, dataset=None):
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        raise ValueError("Domain must be a lowercase package name")
    module_name = f"tasks.{domain}"
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
