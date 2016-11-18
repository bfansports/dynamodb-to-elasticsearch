ENV = dev
PROFILE :=
EVENT :=
DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# Helper functions
FILTER_OUT = $(foreach v,$(2),$(if $(findstring $(1),$(v)),,$(v)))
TITLE_CASE = $(shell echo $1 | cut -c1 | tr '[[:lower:]]' '[[:upper:]]')$(shell echo $1 | cut -c2-)

.PHONY: help clean dist create/% run/% deploy deploy/% _check-desc _check-vers 
.SILENT: help

help:
	echo "SA-LAMBDA MAKEFILE FUNCTIONS"
	echo "----------------------------------------------------------"
	echo "(Add VERBOSE=1 for verbose output)"
	echo "----------------------------------------------------------"
	echo "Run all tests:         make test"
	echo "Run a specific test:   make test/TEST"
	echo "----------------------------------------------------------"
	echo "Create AWS function:   make create/FUNCTION DESC='Func description'"
	echo "Package all functions: make dist"
	echo "Package a function:    make dist/FUNCTION"
	echo "Deploy all functions:  make deploy [ENV=prod] - Default ENV=dev"
	echo "Deploy a function:     make deploy/FUNCTION [ENV=prod]"
	echo "Setup environment:     make env [ENV=environment]"
	echo "Set function MEM size: make setmem/FUNCTION SIZE=[size]" 
	echo "----------------------------------------------------------"

all: dist

create/%: dist/%.zip _check-desc .env
	aws $(if ${PROFILE},--profile ${PROFILE},) s3 cp $< s3://${AWS_BUCKET_CODE}/lambda/$(<F)
	aws $(if ${PROFILE},--profile ${PROFILE},) lambda create-function \
		--function-name $* \
		--memory-size 128 \
		--runtime python2.7 \
		--role ${IAM_ROLE} \
		--handler index.handler \
		--code S3Bucket=${AWS_BUCKET_CODE},S3Key=lambda/$(<F) \
		--description '${DESC}' \
		--timeout 10
setmem/%: _check-size
	aws $(if ${PROFILE},--profile ${PROFILE},) lambda update-function-configuration \
		--function-name $* \
		--memory-size ${SIZE}
deploy: $(addprefix deploy/,$(call FILTER_OUT,__init__, $(notdir $(wildcard src/*)))) .env
deploy/%: dist/%.zip .env
	aws $(if ${PROFILE},--profile ${PROFILE},) s3 cp $< s3://${AWS_BUCKET_CODE}/lambda/$(<F)
	aws $(if ${PROFILE},--profile ${PROFILE},) lambda update-function-code \
		--function-name $* \
		--s3-bucket ${AWS_BUCKET_CODE} \
		--s3-key lambda/$(<F)
dist: $(addprefix dist/,$(addsuffix .zip,$(call FILTER_OUT,__init__, $(notdir $(wildcard src/*))))) .env
dist/%.zip: src/%/* build/setup.cfg $(wildcard lib/**/*) .env
	cd build && zip -r -q ../$@ *
	zip -r -q $@ lib
	cd $(<D) && zip -r -q ../../$@ *

build/setup.cfg: requirements.txt
	find build/ -mindepth 1 -not -name setup.cfg -delete
	pip install -r $^ -t $(@D)
	touch $@

clean:
	-$(RM) -rf dist/*
	-$(RM) -rf build/*
	-$(RM) -f .env

.env:
	aws $(if ${PROFILE},--profile ${PROFILE},) s3 cp s3://${AWS_BUCKET_CODE}/${ENV}_es_creds ./lib/env.py
	cp ./lib/env.py .env

_check-vers:
ifndef VERS
	@echo "You must provide a Version for your API to deploy!";
	@echo "e.g: make api VERS=0.6";
	@echo "We pick the proper file in ./swagger/api-$VERSION.yaml";
	@false;
endif

_check-desc:
ifndef DESC
	@echo "You must provide a description for your function!";
	@echo "e.g: make create/<function> DESC='Awesome function that does great things!'";
	@false;
endif

_check-size:
ifndef SIZE
	@echo "You must provide a size for your function! See lambda console and function configuration for list of memory.";
	@echo "e.g: make setmem/<function> SIZE=512";
	@false;
endif

