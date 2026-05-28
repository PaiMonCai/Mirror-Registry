$ErrorActionPreference = "Stop"

python scripts\verify.py
python -m py_compile scripts\verify.py sync\sync.py panel\main.py tests\test_panel.py tests\test_sync.py

if (Get-Command pytest -ErrorAction SilentlyContinue) {
    python -m pytest
} else {
    Write-Warning "pytest is not installed. Install development dependencies with: python -m pip install -r requirements-dev.txt"
}

if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose config
    docker compose -f docker-compose.dev.yml config
} else {
    Write-Warning "Docker CLI is not available. Skipping docker compose config."
}
