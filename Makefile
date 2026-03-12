.PHONY: install dev clean build publish publish-test check

install:
	pip install -e .

dev:
	pip install -e ".[dev]" 2>/dev/null || pip install -e .
	pip install build twine

clean:
	rm -rf dist/ build/ *.egg-info __pycache__

build: clean
	python -m build

check: build
	twine check dist/*

publish-test: build
	twine upload --repository testpypi dist/*

publish: build
	twine upload dist/*
