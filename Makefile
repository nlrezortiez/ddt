PYTHON ?= python3
BUILD_DIR ?= build

.PHONY: check build replay
check:
	$(PYTHON) -m unittest discover -s tests -v
	sh -n deploy/openrc/sweep
	sh -n deploy/ddt
	sh -n scripts/diagnostics.sh
	sh -n scripts/manifest.sh

build:
	cmake -S . -B $(BUILD_DIR) -DCMAKE_BUILD_TYPE=Release -DENABLE_SHARED_LIB=OFF
	cmake --build $(BUILD_DIR) --parallel 2

replay:
	$(PYTHON) -m ddt --headless --replay tests/fixtures/sweep.csv --status-file $(BUILD_DIR)/replay-status.json
