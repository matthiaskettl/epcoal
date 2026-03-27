# install requirements txt to lib/pip
lib/pip:
	pip3 install -r requirements.txt --target=lib/pip

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
	cp lib/epcoal.py benchmark/benchexec/benchexec/tools/epcoal.py

prepare-bench-defs:
	@if [ ! -d benchmark/sv-benchmarks ]; then echo "Error: benchmark/sv-benchmarks not found"; exit 1; fi
	find benchmark/ -maxdepth 1 -name "*.csv" -exec ./benchmark/generate.py {} --output {}.xml \;

prepare-benchmarks: prepare-benchexec prepare-bench-defs