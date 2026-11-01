UV ?= uv
SAMPLES ?=
AUDIO_MODE ?= speech

.PHONY: setup run check test smoke docker-build docker-up docker-smoke docker-down

setup:
	$(UV) sync --frozen --all-groups

run:
	$(UV) run --frozen uvicorn transcription_service.main:app --reload

check:
	$(UV) lock --check
	$(UV) run --frozen ruff format --check .
	$(UV) run --frozen ruff check .
	$(UV) run --frozen mypy src

test:
	$(UV) run --frozen pytest --cov=transcription_service --cov-report=term-missing

smoke:
	@test -n "$(SAMPLES)" || (echo 'Set SAMPLES, e.g. SAMPLES="he=/path/to/sample.wav"' >&2; exit 2)
	$(UV) run --frozen python scripts/smoke_media.py --audio-mode "$(AUDIO_MODE)" $(SAMPLES)

docker-build:
	docker compose build

docker-up:
	docker compose up --build -d

docker-smoke: docker-up
	@attempt=0; \
	until docker compose exec -T app python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=2)" >/dev/null 2>&1; do \
		attempt=$$((attempt + 1)); \
		if [ $$attempt -ge 180 ]; then docker compose logs app; exit 1; fi; \
		sleep 5; \
	done
	docker compose exec -T app python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/').status)"
	docker compose exec -T app python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/health/ready').read().decode())"

docker-down:
	docker compose down
