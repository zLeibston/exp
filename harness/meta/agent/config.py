"""Role-specific YAML configuration. Credentials never enter agent transcripts."""
from dataclasses import dataclass, field
import math
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class APIConfig:
    model: str
    api_key: str = field(default="", repr=False)
    url: str = ""
    provider: str = "auto"
    max_tokens: int = 16384
    temperature: float | None = None
    timeout: float = 120
    retries: int = 2
    reasoning_effort: str | None = None
    budget_fallback: bool = True
    json_mode: bool = True

    @property
    def protocol(self):
        if self.provider != "auto":
            return "openai" if self.provider == "deepseek" else self.provider
        host = urlsplit(self.url).hostname or ""
        if host == "generativelanguage.googleapis.com":
            return "gemini"
        if host == "api.anthropic.com":
            return "anthropic"
        # Custom gateways default to the widely supported OpenAI chat protocol.
        if self.url:
            return "openai"
        name = self.model.lower()
        if name.startswith(("claude", "anthropic/")):
            return "anthropic"
        if name.startswith(("gemini", "gemini/")):
            return "gemini"
        return "openai"

    @property
    def model_name(self):
        prefixes = ("openai/", "anthropic/", "gemini/", "deepseek/")
        return self.model.split("/", 1)[1] if self.model.startswith(prefixes) else self.model

    @property
    def is_deepseek(self):
        return (self.provider == 'deepseek' or self.model_name.lower().startswith('deepseek')
                or urlsplit(self.url).hostname == 'api.deepseek.com')

    def public(self):
        return dict(model=self.model, url=self.url, provider=self.provider, protocol=self.protocol,
                    max_tokens=self.max_tokens, temperature=self.temperature,
                    timeout=self.timeout, retries=self.retries, reasoning_effort=self.reasoning_effort,
                    budget_fallback=self.budget_fallback, json_mode=self.json_mode)


def parse_api(value, role):
    if not isinstance(value, dict):
        raise ValueError(f"{role} must be a YAML mapping")
    allowed = set(APIConfig.__dataclass_fields__)
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {role} configuration fields: {', '.join(sorted(unknown))}")
    if not isinstance(value.get("model"), str) or not value["model"].strip():
        raise ValueError(f"{role}.model must be a nonempty string")
    result = APIConfig(**value)
    if result.provider not in {"auto", "openai", "deepseek", "anthropic", "gemini"}:
        raise ValueError(f"Unsupported provider for {role}")
    if not isinstance(result.api_key, str) or not isinstance(result.url, str):
        raise ValueError(f"{role}.api_key and url must be strings")
    if result.url:
        parts = urlsplit(result.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(f"{role}.url must be an HTTP(S) endpoint without credentials, query or fragment")
    if isinstance(result.max_tokens, bool) or not isinstance(result.max_tokens, int) or result.max_tokens < 1:
        raise ValueError(f"{role}.max_tokens must be a positive integer")
    if isinstance(result.timeout, bool) or not isinstance(result.timeout, (int, float)) or not math.isfinite(result.timeout) or result.timeout <= 0:
        raise ValueError(f"{role}.timeout must be positive")
    if isinstance(result.retries, bool) or not isinstance(result.retries, int) or result.retries < 0:
        raise ValueError(f"{role}.retries must be a nonnegative integer")
    if result.temperature is not None and (isinstance(result.temperature, bool) or not isinstance(result.temperature, (int, float)) or not math.isfinite(result.temperature)):
        raise ValueError(f"{role}.temperature must be numeric or null")
    if (result.reasoning_effort is not None and not isinstance(result.reasoning_effort, str)
            or result.reasoning_effort not in {None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}):
        raise ValueError(f'Unsupported {role}.reasoning_effort')
    if result.reasoning_effort is not None and result.protocol != 'openai':
        raise ValueError(f'{role}.reasoning_effort currently requires an OpenAI-compatible API')
    if type(result.budget_fallback) is not bool:
        raise ValueError(f'{role}.budget_fallback must be a boolean')
    if type(result.json_mode) is not bool:
        raise ValueError(f'{role}.json_mode must be a boolean')
    if not result.api_key.strip():
        raise ValueError(f"{role}.api_key is required")
    return result


def load_config(path, roles=("meta_agent",)):
    import yaml
    with Path(path).open() as handle:
        data = yaml.safe_load(handle)
    required = set(roles)
    if not isinstance(data, dict) or not required <= set(data) or set(data) - {"task_agent", "meta_agent", "providers", "scheduler", "evaluation", "execution"}:
        raise ValueError("YAML must define the requested API profile; optional providers, scheduler, evaluation and execution")
    presets = data.get("providers", {})
    if not isinstance(presets, dict):
        raise ValueError("providers must be a YAML mapping")
    resolved = {}
    for role in roles:
        if role not in {"task_agent", "meta_agent"} or role not in data:
            raise ValueError(f"Missing API profile: {role}")
        selected = data[role]
        if isinstance(selected, str):
            name, overrides = selected, {}
        elif isinstance(selected, dict) and "use" in selected:
            name = selected["use"]
            overrides = {key: value for key, value in selected.items() if key != "use"}
        else:
            resolved[role] = parse_api(selected, role)
            continue
        if not isinstance(name, str) or name not in presets:
            raise ValueError(f"Unknown provider preset for {role}")
        if not isinstance(presets[name], dict):
            raise ValueError(f"Provider preset {name} must be a mapping")
        resolved[role] = parse_api({**presets[name], **overrides}, role)
    return resolved
