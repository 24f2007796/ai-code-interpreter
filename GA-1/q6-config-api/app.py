
import os
from pathlib import Path

import yaml
from dotenv import dotenv_values
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}


def normalize_key(key):
    key = str(key).lower()

    if key == "num_workers":
        return "workers"

    if key.startswith("app_"):
        key = key[4:]

    return key


def coerce(key, value):
    if key in ("port", "workers"):
        return int(value)

    if key == "debug":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in (
            "true", "1", "yes", "on"
        )

    return str(value)


def merge_layer(config, values):
    if not isinstance(values, dict):
        return

    for raw_key, value in values.items():
        if value is None:
            continue

        key = normalize_key(raw_key)

        if key in DEFAULTS:
            config[key] = coerce(key, value)


@app.get("/effective-config")
def effective_config(
    set: list[str] = Query(default=[])
):
    config = DEFAULTS.copy()

    # Layer 2: Environment-specific YAML
    env_name = os.getenv("APP_ENV", "development")
    yaml_path = BASE_DIR / f"config.{env_name}.yaml"

    if yaml_path.exists():
        with yaml_path.open(encoding="utf-8") as file:
            yaml_config = yaml.safe_load(file) or {}
        merge_layer(config, yaml_config)

    # Layer 3: .env file
    env_file = dotenv_values(BASE_DIR / ".env")
    merge_layer(config, env_file)

    # Layer 4: OS environment variables
    os_config = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("APP_") or key == "NUM_WORKERS"
    }
    merge_layer(config, os_config)

    # Layer 5: Query-string CLI overrides
    for item in set:
        if "=" not in item:
            continue

        raw_key, value = item.split("=", 1)
        key = normalize_key(raw_key)

        if key in DEFAULTS:
            config[key] = coerce(key, value)

    # Never reveal the actual API key
    config["api_key"] = "****"

    return config
