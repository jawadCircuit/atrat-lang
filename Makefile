PY ?= python

.PHONY: test demo bench install check clean

test:
	$(PY) tests/run_tests.py
	$(PY) tests/interfaces_test.py

demo:
	$(PY) atrc run examples/demo.atr

bench:
	$(PY) bench/bench.py

install:
	$(PY) -m pip install .

check:
	$(PY) atrc check examples/demo.atr

clean:
	rm -rf __pycache__ atrat/__pycache__ tests/__pycache__ build dist *.egg-info
