PY ?= python3
VENV ?= $(HOME)/.venvs/ytpipe

.PHONY: help test test-verbose lint install dev clean smoke

help:
	@echo "make test     - offline test suite (stubbed yt-dlp/ffmpeg, no network)"
	@echo "make lint     - syntax/compile check of the package"
	@echo "make install  - install into $(VENV) and link to ~/.local/bin"
	@echo "make dev      - editable install into $(VENV)"
	@echo "make smoke    - live download smoke test (hits YouTube)"
	@echo "make clean    - drop build/test artifacts"

test:
	$(PY) -m unittest discover -s tests -v

lint:
	$(PY) -m compileall -q ytpipe && echo "compile ok"

install:
	./install.sh

dev:
	python3 -m venv $(VENV) && $(VENV)/bin/pip install -q -U pip -e . && \
	ln -sf $(VENV)/bin/ytpipe $(HOME)/.local/bin/ytpipe && echo "linked $(HOME)/.local/bin/ytpipe"

smoke:
	./scripts/smoke.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
