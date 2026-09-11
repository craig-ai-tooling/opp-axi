# opp-axi — build/test/lint/install. The distributable is a single-file zipapp
# (dist/opp-axi.pyz): stdlib-only, runs on any Python 3.10+, no install step.
.PHONY: build install test lint clean doctor

DIST := dist
BIN  := $(HOME)/.local/bin/opp-axi

build:
	@rm -rf build/stage $(DIST)
	@mkdir -p build/stage $(DIST)
	@cp -r opp_axi build/stage/
	python3 -m zipapp build/stage -m "opp_axi.cli:run" -o $(DIST)/opp-axi.pyz -p "/usr/bin/env python3"
	@rm -rf build/stage
	@echo "built $(DIST)/opp-axi.pyz"

install: build
	@mkdir -p $(dir $(BIN))
	@cp $(DIST)/opp-axi.pyz $(BIN)
	@chmod +x $(BIN)
	@echo "installed $(BIN)"
	@echo "next: $(BIN) doctor"

test:
	python3 -m unittest discover -s tests

lint:
	ruff check .

doctor: build
	./$(DIST)/opp-axi.pyz doctor

clean:
	rm -rf $(DIST) build
