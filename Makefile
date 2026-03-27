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

