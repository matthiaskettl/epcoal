CPU_MODEL ?= Intel Xeon E3-1230 v5 @ 3.40 GHz

# install requirements txt to lib/pip
lib/pip:
	pip3 install -r requirements.txt --target=lib/pip --upgrade

lib/cpachecker:
	rm -rf lib/cpachecker
	git -C lib clone https://gitlab.com/sosy-lab/software/cpachecker
	git -C lib/cpachecker checkout tarjan-st-bridges
	cd lib/cpachecker && ant dist-unix-zip
	mv lib/cpachecker/CPAchecker-*.zip lib/cpachecker.zip
	rm -rf lib/cpachecker
	cd lib && unzip cpachecker.zip
	mv lib/CPAchecker-* lib/cpachecker
	rm lib/cpachecker.zip

setup: lib/pip lib/cpachecker

prepare-benchexec:
	rm -rf benchmark/benchexec
	git -C benchmark clone https://github.com/sosy-lab/benchexec
	cp benchmark/epcoal.py benchmark/benchexec/benchexec/tools/epcoal.py

prepare-bench-defs:
	@if [ ! -d benchmark/sv-benchmarks ]; then echo "Error: benchmark/sv-benchmarks not found"; exit 1; fi
	find benchmark/ -maxdepth 1 -name "*_equivalent_mutants.csv" -exec ./benchmark/generate.py {} --cpu-model="${CPU_MODEL}" --output {}.xml \;

prepare-benchmarks: prepare-benchexec prepare-bench-defs

analysis:
	./benchmark/create_table.py --latest
	./benchmark/analyze_tables.py --logfiles benchmark/results/*logfiles*zip
