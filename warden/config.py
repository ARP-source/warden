"""Typed configuration loading.

Config is read once from config/warden.toml and exposed as frozen dataclasses.
A small number of operational values may be overridden by environment variables
so an overnight run can be retuned without editing the file.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "warden.toml"
ENV_FILE = PROJECT_ROOT / ".env"


def load_dotenv(path: Path | None = None, override: bool = False) -> int:
    """Read .env into the environment. No dependency, no surprises.

    Values already set in the real environment win by default, so a shell export
    can always override the file without editing it.
    """
    path = path or ENV_FILE
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or (not override and os.environ.get(key)):
            continue
        if value:
            os.environ[key] = value
            loaded += 1
    return loaded


# Load credentials before any config value is read, so mode and backend
# resolution see the same environment the agents will run with.
load_dotenv()


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


@dataclass(frozen=True)
class BudgetConfig:
    ceiling_usd: float
    reserve_floor_usd: float
    max_call_usd: float


@dataclass(frozen=True)
class LimitsConfig:
    max_model_calls_per_round: int
    max_model_calls_per_hour: int
    max_rounds_per_hour: int
    round_cooldown_s: float
    max_consecutive_round_errors: int


@dataclass(frozen=True)
class ModelsConfig:
    target: str
    attacker: str
    defender: str
    analysis: str
    judge: str


@dataclass(frozen=True)
class InferenceConfig:
    provider: str
    base_url: str
    api_key_envs: tuple[str, ...]

    def api_key(self) -> str | None:
        for name in self.api_key_envs:
            value = os.environ.get(name)
            if value:
                return value
        return None


@dataclass(frozen=True)
class TargetConfig:
    host: str
    port: int
    max_tool_iterations: int
    max_output_tokens: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class PolicyConfig:
    refund_limit_usd: float
    session_refund_cap_usd: float
    allowed_tiers: tuple[int, ...]


@dataclass(frozen=True)
class DefenderConfig:
    max_patch_attempts: int
    benign_regression_tolerance: float


@dataclass(frozen=True)
class AnalysisConfig:
    every_n_rounds: int
    report_dir: str


@dataclass(frozen=True)
class EvalConfig:
    every_n_rounds: int
    min_acceptable_score: float


@dataclass(frozen=True)
class WeaveConfig:
    project: str
    enabled: bool


@dataclass(frozen=True)
class Config:
    mode: str
    seed: int
    budget: BudgetConfig
    limits: LimitsConfig
    models: ModelsConfig
    models_openai: ModelsConfig
    inference: InferenceConfig
    pricing: dict[str, ModelPrice]
    target: TargetConfig
    policy: PolicyConfig
    defender: DefenderConfig
    analysis: AnalysisConfig
    eval: EvalConfig
    weave: WeaveConfig
    paths: dict[str, str] = field(default_factory=dict)

    def path(self, key: str) -> Path:
        p = Path(self.paths[key])
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def ledger_db(self) -> Path:
        return self.path("ledger_db")

    @property
    def ledger_jsonl(self) -> Path:
        return self.path("ledger_jsonl")

    @property
    def versions_dir(self) -> Path:
        return self.path("versions_dir")

    @property
    def halt_file(self) -> Path:
        return self.path("halt_file")

    @property
    def report_dir(self) -> Path:
        p = Path(self.analysis.report_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def price_for(self, model: str) -> ModelPrice:
        if model in self.pricing:
            return self.pricing[model]
        # Unknown model: price it pessimistically so the budget governor can
        # never under-count spend for something we did not anticipate.
        return ModelPrice(input_per_mtok=15.0, output_per_mtok=75.0)

    def model_for(self, role: str, provider: str | None = None) -> str:
        """The model id for a role on the provider that will actually serve it."""
        provider = provider or resolved_provider(self)
        table = self.models_openai if provider == "openai" else self.models
        return getattr(table, role)


def env_str(name: str, default: str = "") -> str:
    """Read an environment variable, treating an empty value as unset.

    Hosting dashboards routinely store a declared-but-blank variable, and
    os.environ.get then returns "" rather than the default. That blank went on
    to fail mode validation and would have crashed the deployed service on
    boot. Absent and blank mean the same thing here.
    """
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else Path(os.environ.get("WARDEN_CONFIG", DEFAULT_CONFIG_PATH))
    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)

    pricing = {
        name: ModelPrice(input_per_mtok=float(v["input"]), output_per_mtok=float(v["output"]))
        for name, v in raw.get("pricing", {}).items()
    }

    mode = env_str("WARDEN_MODE", raw["run"]["mode"]).strip().lower()
    if mode not in {"auto", "live", "simulated"}:
        raise ValueError("invalid run mode " + repr(mode) + "; expected auto|live|simulated")

    weave_enabled = bool(raw["weave"]["enabled"]) and env_str(
        "WARDEN_WEAVE_DISABLED", ""
    ).lower() not in {"1", "true", "yes"}

    return Config(
        mode=mode,
        seed=_env_int("WARDEN_SEED", int(raw["run"]["seed"])),
        budget=BudgetConfig(
            ceiling_usd=_env_float(
                "WARDEN_BUDGET_CEILING_USD", float(raw["budget"]["ceiling_usd"])
            ),
            reserve_floor_usd=float(raw["budget"]["reserve_floor_usd"]),
            max_call_usd=float(raw["budget"]["max_call_usd"]),
        ),
        limits=LimitsConfig(
            max_model_calls_per_round=int(raw["limits"]["max_model_calls_per_round"]),
            max_model_calls_per_hour=_env_int(
                "WARDEN_MAX_CALLS_PER_HOUR", int(raw["limits"]["max_model_calls_per_hour"])
            ),
            max_rounds_per_hour=_env_int(
                "WARDEN_MAX_ROUNDS_PER_HOUR", int(raw["limits"]["max_rounds_per_hour"])
            ),
            round_cooldown_s=_env_float(
                "WARDEN_ROUND_COOLDOWN_S", float(raw["limits"]["round_cooldown_s"])
            ),
            max_consecutive_round_errors=int(raw["limits"]["max_consecutive_round_errors"]),
        ),
        models=ModelsConfig(**raw["models"]),
        models_openai=ModelsConfig(**raw.get("models_openai", raw["models"])),
        inference=InferenceConfig(
            provider=env_str(
                "WARDEN_INFERENCE", raw.get("inference", {}).get("provider", "auto")
            ).lower(),
            base_url=env_str("WARDEN_OPENAI_BASE_URL",
                             raw.get("inference", {}).get("base_url", "")),
            api_key_envs=tuple(raw.get("inference", {}).get(
                "api_key_envs",
                ["WARDEN_OPENAI_API_KEY", "WANDB_API_KEY", "OPENAI_API_KEY"])),
        ),
        pricing=pricing,
        target=TargetConfig(
            host=env_str("WARDEN_TARGET_HOST", raw["target"]["host"]),
            port=_env_int("WARDEN_TARGET_PORT", int(raw["target"]["port"])),
            max_tool_iterations=int(raw["target"]["max_tool_iterations"]),
            max_output_tokens=int(raw["target"]["max_output_tokens"]),
        ),
        policy=PolicyConfig(
            refund_limit_usd=float(raw["policy"]["refund_limit_usd"]),
            session_refund_cap_usd=float(raw["policy"]["session_refund_cap_usd"]),
            allowed_tiers=tuple(int(t) for t in raw["policy"]["allowed_tiers"]),
        ),
        defender=DefenderConfig(
            max_patch_attempts=int(raw["defender"]["max_patch_attempts"]),
            benign_regression_tolerance=float(raw["defender"]["benign_regression_tolerance"]),
        ),
        analysis=AnalysisConfig(
            every_n_rounds=int(raw["analysis"]["every_n_rounds"]),
            report_dir=raw["analysis"]["report_dir"],
        ),
        eval=EvalConfig(
            every_n_rounds=int(raw["eval"]["every_n_rounds"]),
            min_acceptable_score=float(raw["eval"]["min_acceptable_score"]),
        ),
        weave=WeaveConfig(
            project=env_str("WARDEN_WEAVE_PROJECT", raw["weave"]["project"]),
            enabled=weave_enabled,
        ),
        paths=dict(raw["paths"]),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def resolved_provider(cfg: Config | None = None) -> str:
    """Which provider will actually serve model calls: anthropic, openai or simulated.

    Resolution order matters for a project with no API budget: an explicit
    setting wins, then a paid Anthropic key if one exists, then any
    OpenAI-compatible endpoint (W&B Inference, CoreWeave, a local Ollama), and
    only then the deterministic doubles.
    """
    cfg = cfg or get_config()
    choice = cfg.inference.provider
    if choice in {"anthropic", "openai", "simulated"}:
        return choice
    if choice not in {"auto", ""}:
        raise ValueError(
            "invalid inference.provider " + repr(choice)
            + "; expected auto|anthropic|openai|simulated"
        )
    if env_str("ANTHROPIC_API_KEY"):
        return "anthropic"
    if cfg.inference.base_url:
        # A local endpoint such as Ollama needs no key.
        if cfg.inference.api_key() or "localhost" in cfg.inference.base_url                 or "127.0.0.1" in cfg.inference.base_url:
            return "openai"
    return "simulated"


def resolved_mode(cfg: Config | None = None) -> str:
    """Collapse mode auto into the concrete mode this process will use.

    "live" means a real model served the call, whichever provider did it.
    """
    cfg = cfg or get_config()
    if cfg.mode != "auto":
        return cfg.mode
    return "simulated" if resolved_provider(cfg) == "simulated" else "live"


def resolved_store() -> str:
    """Which persistence backend this process will use: supabase or sqlite.

    auto picks Supabase when both its URL and service role key are present, and
    falls back to SQLite otherwise. The fallback is a labelled degradation path,
    not a silent one: the choice appears in the run summary, on the health
    endpoint, and in the posture report.
    """
    choice = env_str("WARDEN_STORE", "auto").lower()
    if choice not in {"auto", "supabase", "sqlite"}:
        choice = "auto"
    have_supabase = bool(env_str("SUPABASE_URL") and env_str("SUPABASE_SERVICE_ROLE_KEY"))
    if choice == "supabase":
        if not have_supabase:
            raise ValueError(
                "WARDEN_STORE=supabase but SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
                "is missing; see .env.example"
            )
        return "supabase"
    if choice == "sqlite":
        return "sqlite"
    return "supabase" if have_supabase else "sqlite"
